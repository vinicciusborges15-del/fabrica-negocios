#!/bin/sh
# Executar apenas na VPS Ubuntu 24.04 escolhida, a partir do pacote revisado.
# Instala monitor público. Não autentica Codex, não ativa IA e não compra nada.
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Execute este instalador como administrador da VPS (sudo)." >&2
    exit 1
fi

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
app_dir=/opt/fabrica-negocios
state_dir=/var/lib/fabrica
runtime_dir=/opt/fabrica-runtime/bin

if ! command -v python3 >/dev/null 2>&1; then
    echo "Este pacote requer a imagem padrão Ubuntu 24.04 com Python 3." >&2
    exit 1
fi
python3 - <<'PY'
from pathlib import Path
import sys
values = {}
for line in Path('/etc/os-release').read_text().splitlines():
    if '=' in line:
        key, value = line.split('=', 1)
        values[key] = value.strip('"')
if values.get('ID') != 'ubuntu' or values.get('VERSION_ID') != '24.04':
    raise SystemExit('Instalador validado para Ubuntu 24.04. Não executado neste sistema.')
if sys.version_info < (3, 11):
    raise SystemExit('É necessário Python 3.11 ou posterior.')
PY

if [ ! -f "$project_dir/fabrica/worker.py" ] || [ ! -f "$project_dir/deploy/fabrica-monitor.service" ]; then
    echo "Extraia o pacote completo antes de executar." >&2
    exit 1
fi
if [ "$(readlink -f "$project_dir")" = "$app_dir" ]; then
    echo "Execute a partir do pacote extraído, fora de /opt/fabrica-negocios." >&2
    exit 1
fi
if [ -e "$app_dir" ] || [ -L "$app_dir" ]; then
    echo "Já existe um destino em $app_dir. Este instalador aceita somente primeira instalação." >&2
    echo "Nenhum código ou dado existente foi alterado. Revise a instalação antes de continuar." >&2
    exit 1
fi

case "$(uname -m)" in
    x86_64)
        codex_target=x86_64-unknown-linux-musl
        codex_digest=26586b0d246d41a799b0ef8ee1add370f0fb0721b3709340f28db612381616ea
        ;;
    aarch64|arm64)
        codex_target=aarch64-unknown-linux-musl
        codex_digest=472ee4d49464a4f8792bbf0162bd9ac18d7036dbc00900a03e95f715887e930f
        ;;
    *) echo "Arquitetura de servidor não suportada por este instalador." >&2; exit 1 ;;
esac

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl python3 git bubblewrap
task_tmp=$(mktemp -d)
trap 'rm -rf -- "$task_tmp"' EXIT HUP INT TERM
archive="$task_tmp/codex.tar.gz"
curl --fail --location --proto '=https' --tlsv1.2 \
    "https://github.com/openai/codex/releases/download/rust-v0.159.2/codex-$codex_target.tar.gz" \
    --output "$archive"
printf '%s  %s\n' "$codex_digest" "$archive" | sha256sum --check --status
tar -xzf "$archive" --no-same-owner -C "$task_tmp"

if ! id fabrica >/dev/null 2>&1; then
    useradd --system --user-group --create-home --home-dir "$state_dir" --shell /bin/bash fabrica
fi
actual_user_home=$(getent passwd fabrica | cut -d: -f6)
if [ "$actual_user_home" != "$state_dir" ]; then
    echo "O usuário fabrica já existe com outro diretório. Revise antes de instalar." >&2
    exit 1
fi
install -d -m 0755 "$app_dir" "$runtime_dir"
install -d -o fabrica -g fabrica -m 0700 "$state_dir"
install -m 0755 "$task_tmp/codex-$codex_target" "$runtime_dir/codex"
if systemctl is-active --quiet fabrica-monitor; then
    systemctl stop fabrica-monitor
fi
cp -R "$project_dir/fabrica" "$app_dir/"
cp -R "$project_dir/examples" "$app_dir/"
# O pacote pode preservar diretórios 0700 e arquivos 0600. O usuário do
# serviço precisa ler o código, sem poder alterá-lo. X mantém executáveis
# existentes e acesso a diretórios; dados e sessão ficam fora destes alvos.
chown -R root:root "$app_dir/fabrica" "$app_dir/examples"
chmod -R u=rwX,go=rX "$app_dir/fabrica" "$app_dir/examples"
install -m 0644 "$project_dir/deploy/fabrica-monitor.service" /etc/systemd/system/fabrica-monitor.service
cd "$app_dir"
runuser -u fabrica -- /usr/bin/python3 -m fabrica --home "$state_dir" init

# Testa o sandbox real sem login nem chamada a modelos. Nunca o desativa.
install -d -o fabrica -g fabrica -m 0700 "$state_dir/workspaces/preflight"
sandbox_ok=false
if runuser -u fabrica -- "$runtime_dir/codex" sandbox -P :workspace \
    -C "$state_dir/workspaces/preflight" -- /usr/bin/python3 -c \
    'from pathlib import Path; Path("sandbox-ok.txt").write_text("sandbox local ok"); print("sandbox_local_ok")'; then
    sandbox_ok=true
fi

systemctl daemon-reload
systemctl enable --now fabrica-monitor
echo "Monitor instalado. Ele permanece sem execução de IA."
if [ "$sandbox_ok" = true ]; then
    echo "Sandbox local passou. Falta o login do Codex e uma rodada real antes de ativar tarefas."
else
    echo "Sandbox local indisponível. Somente monitor ativo; investigar VPS/kernel antes de ativar tarefas." >&2
fi
echo "Consultar: sudo journalctl -u fabrica-monitor -n 20 --no-pager"
