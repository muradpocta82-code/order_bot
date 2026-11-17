#!/usr/bin/env bash
# Замена токена Telegram и перезапуск бота.
# Использование: ./scripts/set_token.sh '9174143843:AA...'
set -euo pipefail

ENV_FILE="/opt/order_bot/.env"

if [ $# -ne 1 ] || [ -z "$1" ]; then
    echo "Использование: $0 '<BOT_TOKEN>'"
    exit 1
fi

TOKEN="$1"
if [[ ! "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; then
    echo "⚠️  Формат токена похож на неверный: ожидается 123456789:AA..."
    echo "    Всё равно записываю — проверка будет при запуске."
fi

if [ ! -f "$ENV_FILE" ]; then
    echo "Не найден $ENV_FILE"
    exit 1
fi

python3 - "$ENV_FILE" "$TOKEN" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
token = sys.argv[2]
lines = path.read_text(encoding="utf-8").splitlines()
found = False
for i, line in enumerate(lines):
    if line.startswith("BOT_TOKEN="):
        lines[i] = f"BOT_TOKEN={token}"
        found = True
        break
if not found:
    lines.append(f"BOT_TOKEN={token}")
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

chmod 600 "$ENV_FILE"
systemctl restart order_bot
sleep 4
systemctl --no-pager --lines=0 status order_bot || true
echo "---"
journalctl -u order_bot -n 25 --no-pager || true
