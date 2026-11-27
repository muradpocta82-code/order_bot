#!/usr/bin/env bash
# H3 (вариант «копия каждому»): подготовить установку бота для нового клиента.
#
# Запускать от root на ЧИСТОМ сервере, после того как код скопирован в /opt/order_bot:
#   bash scripts/client_setup.sh /root/clients/barbek.env
#
# Файл профиля клиента (права 600) — обычный .env, который вы заполняете один раз:
#   CLIENT_NAME=Бар «Бек»
#   BOT_TOKEN=123456789:AA...
#   MANAGER_CHAT_ID=123456789
#   COURIER_CHAT_ID=987654321
#   WEBAPP_PORT=8443
#   CATALOG_WEBAPP_URL=https://bot.barbek.uz:8443/
#
# Скрипт ничего не удаляет и не перезаписывает: если .env уже есть — остановится.
set -euo pipefail
cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"
PROFILE="${1:-}"

step() { echo; echo "=== $* ==="; }
die() { echo "❌ $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Нужен root"
[ -n "$PROFILE" ] || die "Укажите файл профиля клиента: bash scripts/client_setup.sh /root/clients/имя.env"
[ -f "$PROFILE" ] || die "Профиль не найден: $PROFILE"

read_value() {
  local key="$1" default="${2:-}"
  local value
  value="$(grep -E "^${key}=" "$PROFILE" | head -1 | cut -d= -f2- || true)"
  echo "${value:-$default}"
}

CLIENT_NAME="$(read_value CLIENT_NAME)"
BOT_TOKEN="$(read_value BOT_TOKEN)"
MANAGER_CHAT_ID="$(read_value MANAGER_CHAT_ID)"
COURIER_CHAT_ID="$(read_value COURIER_CHAT_ID)"
WEBAPP_PORT="$(read_value WEBAPP_PORT 8443)"
WEBAPP_URL="$(read_value CATALOG_WEBAPP_URL)"

[ -n "$CLIENT_NAME" ] || die "В профиле нет CLIENT_NAME"
[ -n "$BOT_TOKEN" ] || die "В профиле нет BOT_TOKEN (бот в BotFather)"
[ -n "$MANAGER_CHAT_ID" ] || die "В профиле нет MANAGER_CHAT_ID (id чата управляющего)"

if [ -f "$APP_DIR/.env" ]; then
  die ".env уже существует — это не чистая установка. Ничего не меняю."
fi

step "1/6 Проверка профиля"
echo "клиент:   $CLIENT_NAME"
echo "порт:     $WEBAPP_PORT"
echo "менеджер: $MANAGER_CHAT_ID · курьер: ${COURIER_CHAT_ID:-не задан}"

step "2/6 Базовая установка сервера (scripts/bootstrap.sh)"
bash scripts/bootstrap.sh

step "3/6 Окружение клиента"
umask 077
{
  echo "# Установка клиента: $CLIENT_NAME · $(date '+%d.%m.%Y')"
  echo "BOT_TOKEN=$BOT_TOKEN"
  echo "MANAGER_CHAT_ID=$MANAGER_CHAT_ID"
  [ -n "$COURIER_CHAT_ID" ] && echo "COURIER_CHAT_ID=$COURIER_CHAT_ID"
  echo "WEBAPP_PORT=$WEBAPP_PORT"
  [ -n "$WEBAPP_URL" ] && echo "CATALOG_WEBAPP_URL=$WEBAPP_URL"
} > .env.client
grep -vE '^(BOT_TOKEN|MANAGER_CHAT_ID|COURIER_CHAT_ID|WEBAPP_PORT|CATALOG_WEBAPP_URL|# )' \
  .env.example >> .env.client
mv .env.client .env
chown orderbot:orderbot .env
chmod 600 .env
umask 022

DB_PASSWORD="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)"
su postgres -c "psql -q -c \"ALTER ROLE order_bot WITH PASSWORD '$DB_PASSWORD'\""
python3 - "$DB_PASSWORD" <<'PY'
import sys
from pathlib import Path

password = sys.argv[1]
path = Path("/opt/order_bot/.env")
lines = path.read_text(encoding="utf-8").splitlines()
out = []
for line in lines:
    if line.startswith("DATABASE_URL="):
        out.append(f"DATABASE_URL=postgresql://order_bot:{password}@127.0.0.1:5432/order_bot")
    elif line.startswith("TEST_DATABASE_URL="):
        out.append(f"TEST_DATABASE_URL=postgresql://order_bot:{password}@127.0.0.1:5432/order_bot_test")
    else:
        out.append(line)
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
chown orderbot:orderbot .env
echo "пароль базы сгенерирован, .env права 600"

step "4/6 Миграции и прайс"
su orderbot -c "cd $APP_DIR && ./venv/bin/python scripts/migrate.py" || die "Миграции не прошли"
if [ -f catalog.xlsx ]; then
  chown orderbot:orderbot catalog.xlsx
  echo "прайс на месте"
else
  echo "⚠️ положите прайс клиента в $APP_DIR/catalog.xlsx — без него бот не поймёт заказ"
fi

step "5/6 Название и приветствие"
su orderbot -c "cd $APP_DIR && ./venv/bin/python - <<'PY'
import asyncio

from orderbot.config import load_settings
from orderbot.db import Database


async def main() -> None:
    settings = load_settings()
    db = Database(settings.db_path, settings.database_url)
    await db.connect()
    await db.set_setting('client_name', '$CLIENT_NAME')
    await db.close()


asyncio.run(main())
PY
" || echo "название клиента запишем позже (не критично)"

step "6/6 Запуск и проверка"
systemctl restart order_bot order_bot_web || die "Службы не поднялись"
sleep 10
systemctl is-active order_bot order_bot_web
./venv/bin/python scripts/doctor.py || true

cat <<TXT

Готово. Что осталось сделать руками:
1) Открыть бота в Telegram, нажать /start, пройти онбординг заведения.
2) Проверить прайс: команда /price, затем тестовый заказ.
3) Управляющему: /quality (качество разбора), /health (здоровье).
4) Тексты под клиента: /text (приветствие), /tune (пороги), /slots (интервалы).
5) Бэкапы: systemctl status order_bot_backup.timer
6) Мониторинг: bash scripts/set_external.sh HEARTBEAT_URL <ссылка healthchecks>
7) При первом чеке обновить: bash scripts/make_lock.sh → git commit

Клиент: $CLIENT_NAME · порт мини-аппа: $WEBAPP_PORT
TXT
