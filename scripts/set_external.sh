#!/usr/bin/env bash
# Внешние сервисы без правки .env руками (A2, A4).
#
# Примеры:
#   bash scripts/set_external.sh HEARTBEAT_URL https://hc-ping.com/xxxxxxxx-xxxx
#   bash scripts/set_external.sh OFFSITE_REMOTE b2crypt:orderbot-backups
#   bash scripts/set_external.sh OWNER_WEB_TOKEN            # сгенерировать новый
#   bash scripts/set_external.sh --show                     # что уже задано
#
# Что делает: правит одну строку в /opt/order_bot/.env (с бэкапом и правами 600),
# перезапускает бота, а для OFFSITE_REMOTE — ставит и включает таймер копии.
set -euo pipefail
cd "$(dirname "$0")/.."
BASE_DIR="$(pwd)"
ENV_FILE="$BASE_DIR/.env"
ALLOWED="HEARTBEAT_URL OFFSITE_REMOTE OFFSITE_KEEP_DAYS OWNER_WEB_TOKEN SHEETS_ID GOOGLE_SA_FILE"

mask() {
  case "$1" in
    *TOKEN*|*KEY*|*SECRET*) echo "задан (скрыт)" ;;
    *) echo "задан" ;;
  esac
}

if [ ! -f "$ENV_FILE" ]; then
  echo "❌ Не найден $ENV_FILE"
  exit 1
fi

if [ "${1:-}" = "--show" ]; then
  for key in $ALLOWED; do
    value="$(grep -E "^${key}=" "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
    if [ -n "$value" ]; then
      echo "✅ $key — $(mask "$key")"
    else
      echo "— $key не задан"
    fi
  done
  exit 0
fi

KEY="${1:-}"
if [ -z "$KEY" ]; then
  echo "Использование: bash scripts/set_external.sh КЛЮЧ [ЗНАЧЕНИЕ]"
  echo "Ключи: $ALLOWED"
  exit 1
fi

case " $ALLOWED " in
  *" $KEY "*) ;;
  *)
    echo "❌ Ключ $KEY не из списка: $ALLOWED"
    exit 1
    ;;
esac

if [ "$KEY" = "OWNER_WEB_TOKEN" ] && [ -z "${2:-}" ]; then
  VALUE="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)"
else
  VALUE="${2:-}"
fi

if [ -z "$VALUE" ]; then
  echo "❌ Пустое значение для $KEY"
  exit 1
fi

BACKUP="/tmp/env.backup.$(date +%Y%m%d-%H%M%S)"
cp "$ENV_FILE" "$BACKUP"
chmod 600 "$BACKUP"
echo "бэкап .env: $BACKUP"

if grep -qE "^${KEY}=" "$ENV_FILE"; then
  python3 - "$ENV_FILE" "$KEY" "$VALUE" <<'PY'
import sys
from pathlib import Path

path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
lines = Path(path).read_text(encoding="utf-8").splitlines()
out = []
for line in lines:
    if line.startswith(f"{key}="):
        out.append(f"{key}={value}")
    else:
        out.append(line)
Path(path).write_text("\n".join(out) + "\n", encoding="utf-8")
PY
  echo "обновлено: $KEY"
else
  printf "\n%s=%s\n" "$KEY" "$VALUE" >> "$ENV_FILE"
  echo "добавлено: $KEY"
fi

chown orderbot:orderbot "$ENV_FILE"
chmod 600 "$ENV_FILE"

case "$KEY" in
  HEARTBEAT_URL|OWNER_WEB_TOKEN|SHEETS_ID|GOOGLE_SA_FILE)
    systemctl restart order_bot
    echo "✅ order_bot перезапущен"
    ;;
  OFFSITE_REMOTE|OFFSITE_KEEP_DAYS)
    if ! command -v rclone >/dev/null 2>&1; then
      echo "⚠️ rclone не установлен: apt install -y rclone, затем rclone config"
    fi
    install -m 644 deploy/order_bot_offsite.service /etc/systemd/system/
    install -m 644 deploy/order_bot_offsite.timer /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now order_bot_offsite.timer
    echo "✅ таймер order_bot_offsite.timer включён (раз в сутки, 04:30)"
    echo "   проверить сейчас: systemctl start order_bot_offsite.service && journalctl -u order_bot_offsite -n 20 --no-pager"
    ;;
esac

echo ""
echo "Проверка: bash scripts/set_external.sh --show"
