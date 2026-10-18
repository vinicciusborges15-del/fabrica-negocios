#!/usr/bin/env python3
"""Prepara uma revisão fixa; --verificar não instala nem exige root."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request


SOURCE_COMMIT = "7a1c22974d8e63cb9c4e1c6c565b50eb25154f99"
SOURCE_SHA256 = "386b36ed40fdc8ada6c358b6e7b2646ce5e822f60f569b41d8b15614c650f1a4"
SOURCE_URL = (
    "https://codeload.github.com/vinicciusborges15-del/fabrica-negocios/tar.gz/"
    + SOURCE_COMMIT
)
APP_DIR = Path("/opt/fabrica-negocios")
MAX_ARCHIVE_BYTES = 5_000_000
MAX_EXTRACTED_BYTES = 20_000_000


def preflight(*, verify_only: bool) -> None:
    if sys.version_info < (3, 12):
        raise SystemExit("Use Python 3.12 ou posterior, como o fornecido no Ubuntu 24.04.")
    if verify_only:
        return
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise SystemExit("Execute a instalação como root; --verificar dispensa esse acesso.")
    if APP_DIR.exists() or APP_DIR.is_symlink():
        raise SystemExit("O destino já existe. Instalação interrompida sem alterar código ou dados.")
    release = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            release[key] = value.strip('"')
    if release.get("ID") != "ubuntu" or release.get("VERSION_ID") != "24.04":
        raise SystemExit("A instalação nativa exige Ubuntu 24.04.")


def verify_manifest(root: Path) -> list[str]:
    manifest_path = root / "MANIFEST.sha256"
    recorded = set()
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        expected, separator, relative = line.partition("  ")
        parts = PurePosixPath(relative)
        if not separator or parts.is_absolute() or ".." in parts.parts or relative in recorded:
            raise SystemExit("Manifesto inválido; nenhum instalador foi iniciado.")
        target = root / relative
        if not target.is_file() or target.is_symlink():
            raise SystemExit("Arquivo do manifesto ausente ou inválido.")
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise SystemExit("Hash do manifesto divergente; nenhum instalador foi iniciado.")
        recorded.add(relative)
    actual = {file.relative_to(root).as_posix() for file in root.rglob("*") if file.is_file()}
    if actual != recorded | {"MANIFEST.sha256"}:
        raise SystemExit("O pacote contém arquivos fora do manifesto.")
    return sorted(actual)


def download_and_extract(destination: Path) -> tuple[Path, list[str]]:
    # urlopen preserva o proxy e os certificados configurados no ambiente.
    # Nunca desabilitamos verificação TLS ou usamos autenticação neste GET.
    with urllib.request.urlopen(SOURCE_URL, timeout=60) as response:
        origin = urllib.parse.urlsplit(response.geturl())
        if origin.scheme != "https" or origin.hostname != "codeload.github.com":
            raise SystemExit("Origem inesperada; nenhum instalador foi iniciado.")
        data = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise SystemExit("O pacote excede o tamanho permitido.")
    if hashlib.sha256(data).hexdigest() != SOURCE_SHA256:
        raise SystemExit("O arquivo não confere. Instalação interrompida.")
    archive_path = destination / "codigo.tar.gz"
    archive_path.write_bytes(data)
    expected_root = "fabrica-negocios-" + SOURCE_COMMIT
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        if len(members) > 1000 or sum(member.size for member in members) > MAX_EXTRACTED_BYTES:
            raise SystemExit("Conteúdo extraído excede o limite permitido.")
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != expected_root:
                raise SystemExit("Caminho inválido no pacote.")
            if not (member.isdir() or member.isfile()):
                raise SystemExit("Links ou arquivos especiais não são aceitos no pacote.")
        archive.extractall(destination, filter="data")
    root = destination / expected_root
    return root, verify_manifest(root)


def cloud_init_report(completed) -> dict:
    """Resumo sem mensagens de erro, user-data ou detalhes do provedor."""
    try:
        payload = json.loads(completed.stdout)
    except (ValueError, TypeError):
        payload = None
    valid = isinstance(payload, dict)
    payload = payload if valid else {}
    errors = payload.get("errors")
    recoverable = payload.get("recoverable_errors")
    valid = valid and isinstance(errors, list) and all(isinstance(item, str) for item in errors)
    valid = valid and isinstance(recoverable, dict) and all(
        isinstance(items, list) and all(isinstance(item, str) for item in items)
        for items in recoverable.values()
    )
    fatal_count = len(errors) if isinstance(errors, list) else None
    warning_count = sum(len(items) for items in recoverable.values()) if valid else None
    # Cloud-init pode chamar ERROR/CRITICAL de recuperáveis; não os tratamos
    # como simples avisos. Níveis desconhecidos também exigem investigação.
    warning_levels_only = isinstance(recoverable, dict) and all(
        level in {"WARNING", "DEPRECATED"} or not items
        for level, items in recoverable.items()
    )
    status = payload.get("status")
    extended = payload.get("extended_status")
    clean = completed.returncode == 0 and status == "done" and extended == "done" and warning_count == 0
    degraded = completed.returncode == 2 and status == "done" and extended == "degraded done" and warning_count is not None and warning_count > 0
    allowed_states = {"done", "running", "not started", "disabled", "error"}
    allowed_extended = allowed_states | {"degraded done", "degraded running", "error - done", "error - running", "error - not started", "error - disabled"}
    return {
        "component": "cloud-init",
        "returncode": completed.returncode,
        "status": status if isinstance(status, str) and status in allowed_states else "unknown",
        "extended_status": extended if isinstance(extended, str) and extended in allowed_extended else "unknown",
        "fatal_error_count": fatal_count,
        "recoverable_error_count": warning_count,
        "json_schema_valid": bool(valid),
        "stderr_present": bool(completed.stderr),
        "approved": bool(valid and fatal_count == 0 and warning_levels_only and (clean or degraded)),
    }


def wait_for_cloud_init() -> None:
    try:
        completed = subprocess.run(
            ["cloud-init", "status", "--wait", "--format=json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, timeout=120,
        )
    except subprocess.TimeoutExpired:
        print(json.dumps({"component": "cloud-init", "returncode": None, "status": "unknown", "extended_status": "unknown", "error": "timeout", "timeout_seconds": 120, "approved": False}), flush=True)
        raise SystemExit("Cloud-init não concluiu no prazo; nenhum instalador foi iniciado.")
    except OSError:
        print(json.dumps({"component": "cloud-init", "returncode": None, "status": "unknown", "extended_status": "unknown", "error": "command_unavailable", "approved": False}), flush=True)
        raise SystemExit("Não foi possível consultar cloud-init; nenhum instalador foi iniciado.")
    report = cloud_init_report(completed)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if not report["approved"]:
        raise SystemExit("Cloud-init não confirmou conclusão segura; nenhum instalador foi iniciado.")
    if report["returncode"] == 2:
        print("Cloud-init concluiu com avisos recuperáveis. A próxima etapa instala somente o monitor.", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verificar", action="store_true", help="Baixar, verificar e extrair sem instalar; não exige root")
    args = parser.parse_args(argv)
    preflight(verify_only=args.verificar)
    temporary = Path(tempfile.mkdtemp(prefix="fabrica-instalacao-"))
    try:
        root, files = download_and_extract(temporary)
        print(json.dumps({
            "commit": SOURCE_COMMIT,
            "sha256_tar_gz": SOURCE_SHA256,
            "manifest_sha256": hashlib.sha256((root / "MANIFEST.sha256").read_bytes()).hexdigest(),
            "arquivos": files,
            "total_arquivos": len(files),
            "somente_verificacao": args.verificar,
        }, ensure_ascii=False, indent=2), flush=True)
        if args.verificar:
            print("Verificação concluída; nenhum instalador, cloud-init ou serviço foi executado.")
            return 0
        wait_for_cloud_init()
        script = root / "deploy" / "instalar-ubuntu.sh"
        print("Pacote conferido. Instalando a base; aguarde...", flush=True)
        result = subprocess.run([
            "systemd-run", "--unit=fabrica-instalacao", "--wait",
            "--property=Type=exec", "/bin/sh", str(script),
        ])
        subprocess.run(["journalctl", "-u", "fabrica-instalacao", "-n", "35", "--no-pager"])
        return result.returncode
    finally:
        # A instalação pode continuar no systemd após a sessão SSH cair.
        # Nesse modo, mantemos o código temporário para não interrompê-la.
        if args.verificar:
            shutil.rmtree(temporary)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, tarfile.TarError, urllib.error.URLError, subprocess.SubprocessError) as error:
        print("Não foi possível concluir a preparação (" + type(error).__name__ + ").", file=sys.stderr)
        raise SystemExit(1)
