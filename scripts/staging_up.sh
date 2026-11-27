#!/usr/bin/env bash
# A13: поднять тестового бота (staging) — отдельный токен и отдельная база.
#
# Что делает: проверяет .env.staging, готовит базу order_bot_rehearsal
# (свежий дамп с боевой + миграции), ставит и запускает службу.
# Запуск от root:  bash scripts/staging_up.sh
set -euo pipefail
cd "$(dirname "$0")/.."

UNIT=/etc/systemd/system/order_bot_staging.service
ENV_FILE=/opt/order_bot/.env.staging

if [ ! -f "$ENV_FILE" ]; then
  echo "❌ Нет $ENV_FILE"
  echo "Скопируйте .env.staging.example, впишите BOT_TOKEN тестового бота"
  echo "и DATABASE_URL на базу order_bot_rehearsal (docs/STAGING.md)."
  exit 1
fi

TOKEN=$(grep -E '^BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2- || true)
PROD_TOKEN=$(grep -E '^BOT_TOKEN=' .env | cut -d= -f2- || true)
if [ -z "$TOKEN" ]; then
  echo "❌ В $ENV_FILE не задан BOT_TOKEN"
  exit 1
fi
if [ "$TOKEN" = "$PROD_TOKEN" ]; then
  echo "❌ Токен staging совпадает с боевым — нужен отдельный бот из BotFather"
  exit 1
fi

echo "1/4 бэкап боевой базы"
bash scripts/rehearse.sh

echo "2/4 юнит"
install -m 644 deploy/order_bot_staging.service "$UNIT"
systemctl daemon-reload

echo "3/4 запуск службы"
systemctl enable --now order_bot_staging

echo "4/4 проверка"
sleep 8
systemctl is-active order_bot_staging
journalctl -u order_bot_staging -n 15 --no-pager
echo ""
echo "✅ Staging поднят. Остановить: systemctl stop order_bot_staging"
