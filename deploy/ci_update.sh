#!/usr/bin/env bash
# Executado somente por um usuário SSH autorizado com sudo -n na VM.
set -Eeuo pipefail
if [[ "$(id -u)" != 0 ]]; then echo "root necessário" >&2; exit 1; fi
archive="${1:-}"
revision="${2:-}"
if [[ ! "$archive" =~ ^/tmp/advogada-find-ci-[a-f0-9]{40}\.tgz$ ]] || [[ ! "$revision" =~ ^[a-f0-9]{40}$ ]]; then
  echo "Artefato/revisão inválidos" >&2; exit 1
fi
if [[ "$archive" != "/tmp/advogada-find-ci-$revision.tgz" ]]; then exit 1; fi
app=/opt/advogada-find
backup_dir=/var/backups/advogada-find
test -f "$archive" && test -x "$app/.venv/bin/python" && test -f /etc/advogada-find.env
mkdir -p "$backup_dir"
chmod 0700 "$backup_dir"
stage="$(mktemp -d /tmp/advfind-stage.XXXXXXXX)"
backup="$backup_dir/ci-$revision-$(date -u +%Y%m%dT%H%M%SZ).tgz"
applied=0
success=0
cleanup() {
  code=$?
  if [[ "$success" != 1 && "$applied" == 1 ]]; then
    echo "Deploy falhou; restaurando backup..." >&2
    tar -xzf "$backup" -C "$app" || true
    "$app/.venv/bin/python" -m pip install -q -e "$app" || true
    systemctl daemon-reload || true
    systemctl restart advogada-find-dashboard.service || true
    systemctl restart advogada-find.service || true
  fi
  rm -rf "$stage"
  rm -f "$archive"
  exit "$code"
}
trap cleanup EXIT
tar -xzf "$archive" -C "$stage"
test -f "$stage/pyproject.toml"
test -f "$stage/src/advogada_find/prospecting.py"
test -f "$stage/deploy/ci_update.sh"
tar -C "$app" --exclude='./.venv' --exclude='./data' --exclude='./.env' --exclude='./.git' -czf "$backup" .
systemctl stop advogada-find.service advogada-find-dashboard.service
applied=1
# Mantém banco, currículo, configuração SMTP e virtualenv onde já estavam.
cp -a "$stage/." "$app/"
"$app/.venv/bin/python" -m pip install -q -e "$app"
"$app/.venv/bin/python" -m compileall -q "$app/src"
runuser -u advfind -- "$app/.venv/bin/python" -m advogada_find --env-file /etc/advogada-find.env doctor
systemctl daemon-reload
systemctl restart advogada-find-dashboard.service
systemctl restart advogada-find.service
for i in $(seq 1 20); do
  if curl -fsS --max-time 2 http://127.0.0.1:8797/health >/dev/null; then
    success=1
    echo "DEPLOY_OK: $revision"
    break
  fi
  sleep 1
done
if [[ "$success" != 1 ]]; then echo "Falha no health check" >&2; exit 1; fi
