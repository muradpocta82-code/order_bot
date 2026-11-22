#!/usr/bin/env bash
# Репетиция миграций на копии боевой базы.
#
# Берёт свежий дамп (или указанный), восстанавливает его в order_bot_rehearsal
# от имени роли order_bot (поэтому права на таблицы сразу правильные)
# и применяет миграции. Боевая база не меняется.
#
# Запуск (от root):  bash scripts/rehearse.sh [путь-к-дампу]
set -euo pipefail
cd "$(dirname "$0")/.."

DUMP="${1:-$(ls -t /var/backups/order_bot/*.dump 2>/dev/null | head -1)}"
if [ -z "${DUMP:-}" ] || [ ! -f "$DUMP" ]; then
  echo "Не найден дамп для репетиции" >&2
  exit 2
fi

URL="$(venv/bin/python - <<'PY'
from pathlib import Path
print([l for l in Path(".env").read_text().splitlines() if l.startswith("DATABASE_URL=")][0].split("=", 1)[1].strip())
PY
)"
REHEARSAL="$(venv/bin/python - "$URL" <<'PY'
import sys
from urllib.parse import urlsplit, urlunsplit
print(urlunsplit(urlsplit(sys.argv[1])._replace(path="/order_bot_rehearsal")))
PY
)"

echo "Дамп:            $DUMP"
echo "Репетиционная:   order_bot_rehearsal"

su postgres -c "dropdb --if-exists order_bot_rehearsal && createdb -O order_bot order_bot_rehearsal"
# Расширение статистики создаёт суперпользователь: в дампе есть CREATE EXTENSION,
# а роль order_bot такого права не имеет.
su postgres -c "psql -q -d order_bot_rehearsal -c 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements'"
pg_restore --no-owner --no-privileges -d "$REHEARSAL" "$DUMP"
DATABASE_URL="$REHEARSAL" venv/bin/python scripts/migrate.py
echo "Репетиция пройдена. Если всё верно — можно применять на боевой."
