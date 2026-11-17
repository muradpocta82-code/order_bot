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

status=0
for script in scripts/selftest.py scripts/integration_test.py scripts/delivery_flow_check.py; do
  echo "=== $script ==="
  ./venv/bin/python "$script" || status=1
done
exit $status
