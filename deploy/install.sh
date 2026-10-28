#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/advogada-find
STATE_DIR=/var/lib/advogada-find
PRIVATE_DIR=/opt/advogada-find-private
ENV_FILE=/etc/advogada-find.env
SERVICE_NAME=advogada-find.service
SERVICE_SRC="$APP_DIR/deploy/advogada-find.service"
SERVICE_DST="/etc/systemd/system/$SERVICE_NAME"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Execute como root: sudo bash deploy/install.sh" >&2
  exit 1
fi

if [[ ! -d "$APP_DIR" ]]; then
  echo "$APP_DIR não existe. Clone o repositório antes." >&2
  exit 1
fi

command -v python3 >/dev/null || { echo "python3 não encontrado" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "Python 3.11+ necessário" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd não encontrado" >&2; exit 1; }

if ! id -u advfind >/dev/null 2>&1; then
  useradd --system --home "$STATE_DIR" --shell /usr/sbin/nologin advfind
fi

install -d -o advfind -g advfind -m 0750 "$STATE_DIR"
install -d -o advfind -g advfind -m 0700 "$PRIVATE_DIR"

cd "$APP_DIR"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e .

if [[ ! -f "$ENV_FILE" ]]; then
  cp .env.example "$ENV_FILE"
  chown root:advfind "$ENV_FILE"
  chmod 640 "$ENV_FILE"
  echo "Criado $ENV_FILE com DRY_RUN=true."
fi

install -m 0644 "$SERVICE_SRC" "$SERVICE_DST"
install -m 0644 "$APP_DIR/deploy/advogada-find-dashboard.service" /etc/systemd/system/advogada-find-dashboard.service
install -m 0644 "$APP_DIR/deploy/advogada-find-cnpj.service" /etc/systemd/system/advogada-find-cnpj.service
install -m 0644 "$APP_DIR/deploy/advogada-find-cnpj.timer" /etc/systemd/system/advogada-find-cnpj.timer
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl enable advogada-find-dashboard.service
# Receita/CNPJ (CNAE 6911-7/01) é opcional: baixa a publicação nacional inteira.
# Ativar só se pedido: systemctl enable --now advogada-find-cnpj.timer

echo
echo "Instalação concluída."
echo "1. Copie o CV para $PRIVATE_DIR/cv.pdf"
echo "2. Edite $ENV_FILE"
echo "3. Valide: sudo -u advfind $APP_DIR/.venv/bin/python -m advogada_find --env-file $ENV_FILE doctor --smtp"
echo "   O diagnóstico autentica sem enviar. DRY_RUN=true até configurar o envio real."
echo "4. Inicie com: systemctl start $SERVICE_NAME"
echo "5. Logs: journalctl -u $SERVICE_NAME -f"
