#!/usr/bin/env bash
# Все тесты на PostgreSQL (order_bot_test). SQLite больше не используется.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -z "${TEST_DATABASE_URL:-}" ] && [ -f .env ]; then
  TEST_DATABASE_URL="$(grep '^TEST_DATABASE_URL=' .env | cut -d= -f2-)"
  export TEST_DATABASE_URL
fi

if [ -z "${TEST_DATABASE_URL:-}" ]; then
  echo "TEST_DATABASE_URL не задан" >&2
  exit 2
fi

# Быстрая проверка неопределённых имён (та же болезнь, что инцидент 14.11 с USDT).
if [ -x ./venv/bin/ruff ]; then
  echo "=== ruff: неопределённые имена ==="
  ./venv/bin/ruff check --select F821,F822,F823 --output-format concise orderbot main.py webapp scripts \
    || { echo "НАЙДЕНЫ неопределённые имена — деплой запрещён" >&2; exit 1; }
fi

status=0
for script in scripts/selftest.py scripts/integration_test.py scripts/delivery_flow_check.py; do
  echo "=== $script ==="
  ./venv/bin/python "$script" || status=1
done
exit $status
