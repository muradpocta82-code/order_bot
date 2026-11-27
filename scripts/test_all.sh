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

# I6: деньги не через float — новые случаи блокируют деплой.
echo "=== scripts/secret_scan.sh ==="
./scripts/secret_scan.sh \
  || { echo "СЕКРЕТЫ В РЕПОЗИТОРИИ"; exit 1; }

echo "=== scripts/money_check.py ==="
./venv/bin/python scripts/money_check.py \
  || { echo "НОВЫЕ ДЕНЬГИ ЧЕРЕЗ float — деплой запрещён" >&2; status=1; }

# A11: у каждой кнопки должен быть обработчик.
echo "=== scripts/buttons_check.py ==="
./venv/bin/python scripts/buttons_check.py \
  || { echo "ЕСТЬ МЁРТВЫЕ КНОПКИ — деплой запрещён" >&2; status=1; }

# E2: точность разбора не должна падать больше чем на 2 пункта от базовой.
echo "=== scripts/eval_parse.py --check ==="
./venv/bin/python scripts/eval_parse.py --local-only --check --show-errors 3 \
  || { echo "ТОЧНОСТЬ РАЗБОРА УПАЛА — деплой запрещён" >&2; status=1; }

for script in scripts/selftest.py scripts/integration_test.py scripts/delivery_flow_check.py; do
  echo "=== $script ==="
  ./venv/bin/python "$script" || status=1
done
exit $status
