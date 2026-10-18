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


SOURCE_COMMIT = "7bfad7131efd9a5b88a14983ab593367109f5f23"
SOURCE_SHA256 = "5fd09a89a47861c07681a1120130bbfa1cf74e058c7e5af4c497101586c7c69c"
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
        subprocess.run(["cloud-init", "status", "--wait"], check=True, timeout=180)
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
