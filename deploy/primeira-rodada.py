#!/usr/bin/env python3
"""Uma primeira rodada de pesquisa; --verificar apenas mostra a proposta."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


APP_DIR = "/opt/fabrica-negocios"
STATE_DIR = "/var/lib/fabrica"
CODEX_PATH = "/opt/fabrica-runtime/bin/codex"
PYTHON_PATH = "/usr/bin/python3"
SERVICE_USER = "fabrica"
UNIT = "fabrica-primeira-rodada.service"
SERVICE_PATH = "/opt/fabrica-runtime/bin:/usr/bin:/bin"

SPEC = {
    "name": "Orçamento Fácil para prestadores",
    "hypothesis": "Prestadores que hoje fazem orçamentos no WhatsApp podem pagar por uma ferramenta simples para montar e imprimir um orçamento profissional. Hipótese ainda sem compradores ou demanda confirmada.",
    "product": "Uma página HTML em português, sem dependências externas: dados do prestador e cliente, itens com quantidade e valor, desconto, validade, observações, total em reais, salvar rascunho no navegador e imprimir/PDF. Funcionar offline. Incluir exemplos fictícios identificados, botão limpar e instruções. Não coletar dados em servidores. Não inventar depoimentos nem dizer que houve vendas.",
    "offer": "Testar uma licença de R$ 29 por pagamento único somente depois de conectar recebimento e apresentar o produto a usuários reais.",
    "distribution": "Preparar uma demonstração e textos curtos para revisão do dono. Nenhuma mensagem deve ser enviada automaticamente.",
    "evidence": [],
    "budget_cents": 3000,
    "duration_days": 7,
}


# Este código é enviado ao gerenciador do sistema como argumento de ExecStart.
# Não é salvo em /tmp nem depende do arquivo usado pelo comando de bootstrap.
# Todo acesso operacional ocorre como o usuário fabrica, definido na unidade.
WORKER = r'''
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from fabrica.core import Factory
from fabrica.runner import doctor


class FirstRoundBlocked(Exception):
    pass


class ResearchFactory(Factory):
    """Reentrância local; o lock real fica retido durante toda a rodada."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._first_round_lock_depth = 0

    @contextmanager
    def locked(self):
        if self._first_round_lock_depth:
            self._first_round_lock_depth += 1
            try:
                yield
            finally:
                self._first_round_lock_depth -= 1
            return
        with super().locked():
            self._first_round_lock_depth = 1
            try:
                yield
            finally:
                self._first_round_lock_depth = 0


def emit(phase, **fields):
    print(json.dumps({"phase": phase, **fields}, ensure_ascii=False), flush=True)


def initial_state(factory):
    status = factory.status()
    rows = status["businesses"]
    if status["runs"] != 0:
        raise FirstRoundBlocked("Já existe execução registrada; inspecione o resultado antes de continuar.")
    if not rows:
        return None
    if len(rows) != 1 or rows[0]["status"] != "queued":
        raise FirstRoundBlocked("Estado inicial incompatível; nenhum novo experimento será criado.")
    identifier = rows[0]["id"]
    with factory.connect() as connection:
        business = factory.business(connection, identifier)
    if json.loads(business["specification"]) != SPEC:
        raise FirstRoundBlocked("Já existe uma hipótese diferente; nenhum cadastro será alterado.")
    workspace = Path(STATE_DIR) / "workspaces" / identifier
    specification = workspace / "experiment.json"
    if workspace.is_symlink() or not workspace.is_dir() or specification.is_symlink() or not specification.is_file():
        raise FirstRoundBlocked("Cadastro parcial ou workspace inválido; revise sem recriar os dados.")
    if json.loads(specification.read_text(encoding="utf-8")) != SPEC:
        raise FirstRoundBlocked("Os dados do workspace divergem do cadastro; revise antes de continuar.")
    return identifier


def sandbox_preflight():
    with tempfile.TemporaryDirectory(prefix="primeira-rodada-", dir=Path(STATE_DIR) / "workspaces") as directory:
        command = [CODEX_PATH, "sandbox", "-P", ":workspace", "-C", directory, "--", PYTHON_PATH, "-c",
                   'from pathlib import Path; Path("sandbox-ok.txt").write_text("ok"); print("sandbox_local_ok")']
        result = subprocess.run(command, cwd=directory, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=45, check=False)
        if result.returncode != 0 or "sandbox_local_ok" not in result.stdout.splitlines():
            emit("sandbox", ok=False, returncode=result.returncode)
            raise FirstRoundBlocked("Sandbox não confirmou execução local; a rodada foi bloqueada.")
    emit("sandbox", ok=True)


def check_plan(factory, identifier):
    plan = factory.tick(dry_run=True, max_jobs=1)
    jobs = plan.get("jobs", [])
    if len(jobs) != 1 or jobs[0].get("id") != identifier or jobs[0].get("stage") != "research":
        raise FirstRoundBlocked("Dry-run não selecionou exatamente a pesquisa esperada; nenhuma IA será iniciada.")
    emit("dry_run", id=identifier, stage="research", max_jobs=1)


def worker_main():
    if os.geteuid() == 0:
        emit("blocked", message="A rotina operacional deve executar como fabrica, nunca como root.")
        return 1
    lock_path = Path(STATE_DIR) / "state" / "first-round.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise FirstRoundBlocked("Lock operacional inválido.")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            emit("blocked", message="Outra primeira rodada já está em andamento.")
            return 1
        factory = ResearchFactory(Path(STATE_DIR))
        with factory.locked():
            if factory.config["runner_timeout_seconds"] > 900:
                raise FirstRoundBlocked("Tempo limite configurado excede 900 segundos; revise antes de continuar.")
            identifier = initial_state(factory)
            emit("initial_state", runs=0, existing=identifier)
            sandbox_preflight()
            auth = doctor(command=CODEX_PATH)
            emit("doctor", installed=auth.get("installed"), version=auth.get("version"),
                 authenticated=auth.get("authenticated"), auth_method=auth.get("auth_method"),
                 auth_remotely_verified=False)
            if auth.get("authenticated") is not True or auth.get("auth_method") != "chatgpt":
                raise FirstRoundBlocked("Login local ChatGPT não confirmado; nenhuma IA será iniciada.")
            # Reconfere depois dos preflights, antes de qualquer cadastro ou IA.
            identifier = initial_state(factory)
            if identifier is None:
                identifier = factory.add_experiment(SPEC)["id"]
                emit("registered", id=identifier, created=True)
            else:
                emit("registered", id=identifier, created=False)
            check_plan(factory, identifier)
            if initial_state(factory) != identifier:
                raise FirstRoundBlocked("O estado mudou antes da execução; a rodada foi bloqueada.")
            result = factory.tick(max_jobs=1)
            jobs = result.get("jobs", [])
            success = (len(jobs) == 1 and jobs[0].get("id") == identifier
                       and jobs[0].get("stage") == "research" and jobs[0].get("ok") is True
                       and jobs[0].get("status") == "build")
            emit("finished", ok=success, id=identifier,
                 jobs=[{key: job.get(key) for key in ("id", "stage", "status", "ok")} for job in jobs],
                 message="Plano local concluído; construção e vendas ainda não ocorreram." if success
                 else "A pesquisa não foi confirmada; consulte o estado e os logs privados antes de retomar.")
            return 0 if success else 1


try:
    raise SystemExit(worker_main())
except FirstRoundBlocked as error:
    emit("blocked", error_type="FirstRoundBlocked", message=str(error))
    raise SystemExit(1)
except Exception as error:
    # Não propagamos stderr, conteúdo do banco, credenciais ou mensagens
    # arbitrárias de dependências. Os detalhes ficam fora do diário público.
    emit("blocked", error_type=type(error).__name__, message="Falha local; consulte o estado antes de retomar.")
    raise SystemExit(1)
'''


def worker_code() -> str:
    return (
        "SPEC = " + repr(SPEC) + "\nSTATE_DIR = " + repr(STATE_DIR)
        + "\nCODEX_PATH = " + repr(CODEX_PATH) + "\nPYTHON_PATH = " + repr(PYTHON_PATH)
        + "\n" + WORKER
    )


def launch_command() -> list[str]:
    return [
        "/usr/bin/systemd-run", "--unit=" + UNIT,
        "--property=Type=exec", "--property=RemainAfterExit=yes",
        "--property=User=" + SERVICE_USER, "--property=Group=" + SERVICE_USER,
        "--working-directory=" + APP_DIR,
        "--setenv=HOME=" + STATE_DIR, "--setenv=PATH=" + SERVICE_PATH,
        "--setenv=PYTHONUNBUFFERED=1", PYTHON_PATH, "-c", worker_code(),
    ]


def review_plan() -> dict:
    return {
        "mode": "verificar", "effects": "nenhum; somente proposta estática",
        "specification": SPEC,
        "sequence": [
            "Validar Ubuntu e diretórios; iniciar unidade fixa como fabrica.",
            "Adquirir lock separado; exigir zero execuções e fila vazia ou hipótese idêntica queued.",
            "Verificar sandbox local e login ChatGPT por metadados do CLI.",
            "Cadastrar somente se não houver negócio; conferir dry-run de uma pesquisa.",
            "Revalidar estado; executar tick(max_jobs=1) uma única vez e terminar.",
        ],
        "launch_command": launch_command(),
        "unit_status_command": ["/usr/bin/systemctl", "show", UNIT, "-p", "LoadState", "-p", "ActiveState", "-p", "SubState", "-p", "Result", "-p", "ExecMainStatus"],
        "journal_command": ["/usr/bin/journalctl", "-u", UNIT, "-n", "30", "--no-pager"],
        "codex_timeout_max_seconds": 900,
        "continuous_worker_enabled": False,
    }


def validate_host() -> None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise ValueError("Execute o disparo como root; --verificar dispensa esse acesso.")
    if sys.version_info < (3, 11):
        raise ValueError("É necessário Python 3.11 ou posterior.")
    release = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            release[key] = value.strip('"')
    if release.get("ID") != "ubuntu" or release.get("VERSION_ID") != "24.04":
        raise ValueError("O disparo exige Ubuntu 24.04.")
    import grp
    import pwd
    account = pwd.getpwnam(SERVICE_USER)
    if account.pw_dir != STATE_DIR or account.pw_gid != grp.getgrnam(SERVICE_USER).gr_gid:
        raise ValueError("A conta de serviço não corresponde à instalação esperada.")
    code_paths = [Path(APP_DIR), Path(APP_DIR) / "fabrica",
                  Path("/opt/fabrica-runtime"), Path("/opt/fabrica-runtime/bin"), Path(CODEX_PATH)]
    code_paths.extend((Path(APP_DIR) / "fabrica" / name) for name in (
        "__init__.py", "core.py", "finance.py", "runner.py",
    ))
    for path in code_paths:
        info = path.lstat()
        if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Código instalado sem a propriedade ou proteção esperada.")
    for path in (Path(STATE_DIR), Path(STATE_DIR) / "state", Path(STATE_DIR) / "workspaces"):
        info = path.lstat()
        if path.is_symlink() or not path.is_dir() or info.st_uid != account.pw_uid or info.st_mode & 0o022:
            raise ValueError("Diretório operacional não corresponde ao usuário de serviço.")
    database = Path(STATE_DIR) / "state" / "fabrica.sqlite3"
    if database.is_symlink() or not database.is_file() or database.stat().st_uid != account.pw_uid:
        raise ValueError("Banco inicial ausente ou inválido; nenhuma hipótese será recriada.")
    if not Path(PYTHON_PATH).is_file() or not os.access(CODEX_PATH, os.X_OK):
        raise ValueError("Executáveis necessários indisponíveis.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verificar", action="store_true", help="Mostrar especificação e comandos sem consultar conta, autenticação ou sistema")
    args = parser.parse_args(argv)
    if args.verificar:
        print(json.dumps(review_plan(), ensure_ascii=False, indent=2))
        return 0
    validate_host()
    result = subprocess.run(launch_command(), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=False, timeout=30)
    print(json.dumps({
        "unit": UNIT, "dispatch_requested": result.returncode == 0,
        "returncode": result.returncode, "completion_verified": False,
        "message": "Consulte estado e diário da unidade; a tarefa continua independente do SSH."
        if result.returncode == 0 else "Unidade já existente ou falha no disparo; consulte o estado antes de repetir.",
    }, ensure_ascii=False), flush=True)
    return 0 if result.returncode == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        message = str(error) if type(error) is ValueError else "Não foi possível preparar o disparo local."
        print(json.dumps({"error_type": type(error).__name__, "message": message}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
