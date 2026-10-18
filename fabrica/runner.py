"""Executa o Codex CLI instalado, usando a autenticação já configurada.

O ambiente do processo contém somente caminhos, localização e configuração de
proxy/certificados. Chaves de API do ambiente não são encaminhadas. Os arquivos
da execução são privados; respostas e erros brutos ficam somente nesses logs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
from typing import Any
import uuid


_ENVIRONMENT_NAMES = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "CODEX_HOME",
    "TMPDIR", "TEMP", "TMP", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    "XDG_STATE_HOME", "XDG_CACHE_HOME", "LANG", "LANGUAGE", "LC_ALL",
    "LC_CTYPE", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "HTTP_PROXY", "HTTPS_PROXY",
    "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy",
    "no_proxy", "SYSTEMROOT", "WINDIR",
}


def _environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key in _ENVIRONMENT_NAMES}


def _private_file(path: Path):
    """Não segue links nem reutiliza arquivos preexistentes."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def _failure_message(detail: str, default: str) -> str:
    """Classifica sem propagar mensagens que possam conter credenciais."""
    lowered = detail.lower()
    if any(word in lowered for word in (
        "not logged in", "login required", "unauthorized", "unauthenticated",
        "authentication", "invalid_api_key", "token expired", "please log in",
        "refresh token", "401 unauthorized", "please sign in", "logged out",
        "token could not be refreshed",
    )):
        return "Autenticação do Codex necessária; execute 'codex login' no terminal."
    if any(word in lowered for word in (
        "usage limit", "quota exceeded", "insufficient_quota", "usage_limit_reached",
    )):
        return "Limite de uso do Codex atingido; aguarde a renovação do seu limite."
    if "rate limit" in lowered or "rate_limit" in lowered:
        return "Limite temporário de solicitações do Codex atingido; tente mais tarde."
    return default


def _schema_matches(value: Any, schema: dict, root: dict | None = None) -> bool:
    """Valida um subconjunto de JSON Schema, suficiente para respostas estruturadas.

    Abrange tipos, required, properties, additionalProperties, items, enum,
    limites de strings/listas/números, anyOf/oneOf e referências locais. Não é um
    substituto de um validador completo de JSON Schema.
    """
    root = schema if root is None else root
    reference = schema.get("$ref")
    if reference is not None:
        if not isinstance(reference, str) or not reference.startswith("#/"):
            return False
        target: Any = root
        try:
            for piece in reference[2:].split("/"):
                target = target[piece.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError):
            return False
        if not isinstance(target, dict) or not _schema_matches(value, target, root):
            return False
    if "anyOf" in schema and not any(_schema_matches(value, branch, root) for branch in schema["anyOf"]):
        return False
    if "oneOf" in schema and sum(_schema_matches(value, branch, root) for branch in schema["oneOf"]) != 1:
        return False
    if "allOf" in schema and not all(_schema_matches(value, branch, root) for branch in schema["allOf"]):
        return False
    expected = schema.get("type")
    types = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if expected is not None:
        choices = expected if isinstance(expected, list) else [expected]
        if not any(types.get(choice, False) for choice in choices):
            return False
    if "enum" in schema and not any(type(value) is type(item) and value == item for item in schema["enum"]):
        return False
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        return False
    if isinstance(value, dict):
        if any(key not in value for key in schema.get("required", [])):
            return False
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                if not _schema_matches(item, properties[key], root):
                    return False
            elif additional is False:
                return False
            elif isinstance(additional, dict) and not _schema_matches(item, additional, root):
                return False
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            return False
        if isinstance(schema.get("items"), dict) and not all(_schema_matches(item, schema["items"], root) for item in value):
            return False
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            return False
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")):
            return False
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            return False
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            return False
    return True


def _events(path: Path) -> tuple[str | None, dict, str | None]:
    last_message = None
    usage: dict = {}
    failed = None
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "turn.failed":
                error = event.get("error")
                failed = str(error.get("message", "")) if isinstance(error, dict) else str(error or "")
            item = event.get("item")
            if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
                if isinstance(item.get("text"), str):
                    last_message = item["text"]
            if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                for key, value in event["usage"].items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        usage[key] = usage.get(key, 0) + value
    return last_message, usage, failed


class CodexRunner:
    def __init__(self, command: str = "codex", timeout_seconds: int = 900):
        if not isinstance(command, str) or not command or "\x00" in command:
            raise ValueError("Informe um executável do Codex válido.")
        if timeout_seconds <= 0:
            raise ValueError("O tempo limite precisa ser positivo.")
        self.command = command
        self.timeout_seconds = timeout_seconds

    def run(self, workspace: Path, prompt: str, schema: dict | None = None) -> dict:
        workspace = Path(workspace).absolute()
        run_dir = workspace / ".runs" / uuid.uuid4().hex
        answer: dict = {"ok": False, "result": None, "error": None, "run_dir": str(run_dir), "usage": {}}
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            runs_parent = workspace / ".runs"
            if runs_parent.is_symlink():
                answer["error"] = "O diretório .runs não pode ser um link simbólico."
                return answer
            runs_parent.mkdir(mode=0o700, exist_ok=True)
            run_dir.mkdir(mode=0o700)
            with _private_file(run_dir / "prompt.txt") as stream:
                stream.write(prompt)
            if schema is not None:
                if not isinstance(schema, dict):
                    answer["error"] = "O esquema de resposta precisa ser um objeto JSON."
                    return answer
                with _private_file(run_dir / "schema.json") as stream:
                    json.dump(schema, stream, ensure_ascii=False, allow_nan=False)
            final_path = run_dir / "last-message.txt"
            with _private_file(final_path):
                pass
            command = [
                self.command, "exec", "--skip-git-repo-check", "--ephemeral",
                "--sandbox", "workspace-write", "-c", "sandbox_workspace_write.network_access=false",
                "--color", "never", "--json", "-C", str(workspace), "-o", str(final_path),
            ]
            if schema is not None:
                command.extend(["--output-schema", str(run_dir / "schema.json")])
            command.append("-")
            timed_out = False
            with _private_file(run_dir / "stdout.jsonl") as stdout, _private_file(run_dir / "stderr.txt") as stderr:
                # Barreira central para tick, run e worker. O status é obtido
                # somente pelo CLI; não lemos auth.json nem valores de tokens.
                # Não aceitamos um método desconhecido ou uma API paga como
                # alternativa à assinatura ChatGPT configurada pelo dono.
                auth = doctor(self.command)
                if auth.get("authenticated") is not True or auth.get("auth_method") != "chatgpt":
                    answer["error"] = (
                        "Execução bloqueada: entre no Codex com ChatGPT usando 'codex login'. "
                        "Login por chave de API ou método desconhecido não é permitido."
                    )
                    return answer
                options: dict = {"start_new_session": True, "umask": 0o077} if os.name == "posix" else {}
                process = subprocess.Popen(
                    command, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                    cwd=workspace, env=_environment(), text=True, encoding="utf-8", errors="replace", **options,
                )
                try:
                    process.communicate(input=prompt, timeout=self.timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    try:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                    except ProcessLookupError:
                        pass
                    except OSError:
                        process.kill()
                    process.communicate()
            event_message, answer["usage"], event_failure = _events(run_dir / "stdout.jsonl")
            if timed_out:
                answer["error"] = "O Codex excedeu o tempo limite; seu grupo de processos foi encerrado."
                return answer
            if process.returncode != 0 or event_failure is not None:
                detail = (run_dir / "stderr.txt").read_text(encoding="utf-8", errors="replace")
                default = "O Codex informou uma falha. Consulte os logs privados desta execução."
                answer["error"] = _failure_message((event_failure or "") + "\n" + detail, default)
                return answer
            message = final_path.read_text(encoding="utf-8", errors="replace").strip() or event_message
            if not message:
                answer["error"] = "O Codex terminou sem uma resposta final. Consulte os logs privados desta execução."
                return answer
            try:
                parsed = json.loads(message)
            except (ValueError, RecursionError):
                if schema is not None:
                    answer["error"] = "O Codex devolveu uma resposta fora do formato JSON exigido."
                    return answer
                parsed = message
            if schema is not None:
                try:
                    valid = _schema_matches(parsed, schema)
                except (TypeError, ValueError, KeyError, RecursionError, re.error):
                    valid = False
                if not valid:
                    answer["error"] = "A resposta do Codex não corresponde ao esquema solicitado."
                    return answer
            answer["result"] = parsed if isinstance(parsed, (dict, str)) else message
            if isinstance(parsed, dict):
                with _private_file(run_dir / "result.json") as stream:
                    json.dump(parsed, stream, ensure_ascii=False, indent=2, allow_nan=False)
            answer["ok"] = True
            return answer
        except FileNotFoundError:
            answer["error"] = "O executável do Codex ou um diretório da execução não foi encontrado."
        except PermissionError:
            answer["error"] = "Sem permissão para executar o Codex ou gravar seus arquivos."
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            answer["error"] = "Não foi possível concluir a execução do Codex. Consulte os logs privados, se disponíveis."
        return answer


def doctor(command: str = "codex") -> dict:
    """Inspeciona presença, versão e login, sem ler ou devolver credenciais.

    O status local não verifica a sessão remota. Um login armazenado pode
    estar expirado ou revogado; somente uma execução poderá detectar isso.
    """
    found = shutil.which(command)
    result: dict = {
        "installed": found is not None, "version": None, "authenticated": None,
        "auth_status": "unknown", "auth_method": None,
        "auth_remotely_verified": False, "error": None,
    }
    if found is None:
        result["error"] = "Codex CLI não encontrado; instale-o e execute 'codex login'."
        return result
    try:
        version = subprocess.run([found, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, env=_environment())
        match = re.search(r"(?m)^codex(?:-cli)?\s+v?([0-9]+\.[0-9]+\.[0-9]+[a-zA-Z0-9.+-]*)\s*$", version.stdout)
        if version.returncode == 0 and match:
            result["version"] = match.group(1)
        status = subprocess.run([found, "login", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, env=_environment())
        detail = (status.stdout + "\n" + status.stderr).lower()
        if status.returncode == 0 and "logged in" in detail and "not logged in" not in detail:
            result.update(authenticated=True, auth_status="authenticated")
            if "chatgpt" in detail:
                result["auth_method"] = "chatgpt"
            elif "api key" in detail or "api_key" in detail:
                result["auth_method"] = "api_key"
        elif status.returncode != 0 or "not logged in" in detail:
            result.update(authenticated=False, auth_status="login_required")
            result["error"] = "Autenticação do Codex necessária; execute 'codex login' no terminal."
        else:
            result["error"] = "Não foi possível confirmar a autenticação do Codex."
    except subprocess.TimeoutExpired:
        result["error"] = "O diagnóstico do Codex excedeu o tempo limite."
    except (OSError, ValueError, subprocess.SubprocessError):
        result["error"] = "Não foi possível consultar o Codex CLI."
    return result
