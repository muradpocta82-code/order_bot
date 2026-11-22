#!/usr/bin/env bash
# A14: восстановление сервера с нуля — чистый Ubuntu → рабочий бот.
#
# Запускать от root:  bash scripts/bootstrap.sh [--with-db-dump /path/dump]
#
# Скрипт ничего не удаляет: только ставит пакеты, создаёт пользователя и базу,
# окружение, службы и права. Секреты вписываются в .env вручную после прогона
# (скрипт подскажет, какие именно переменные нужны).
#
# Проверка «восстановление с нуля» раз в квартал: одноразовый VPS, этот скрипт,
# затем restore + doctor.py. Цель — рабочий бот не дольше чем за 2 часа.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/order_bot}"
DB_NAME="${DB_NAME:-order_bot}"
DB_USER="${DB_USER:-order_bot}"
BOT_USER="${BOT_USER:-orderbot}"
DUMP=""
if [ "${1:-}" = "--with-db-dump" ]; then DUMP="${2:-}"; fi

step() { echo; echo "=== $* ==="; }
need() { command -v "$1" >/dev/null 2>&1; }

[ "$(id -u)" -eq 0 ] || { echo "Нужен root" >&2; exit 1; }

step "1/9 Пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip postgresql postgresql-contrib \
  ffmpeg rclone curl ca-certificates rsync >/dev/null

step "2/9 Пользователь $BOT_USER"
id "$BOT_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "$BOT_USER"

step "3/9 Код в $APP_DIR"
mkdir -p "$APP_DIR"
if [ ! -f "$APP_DIR/main.py" ]; then
  echo "Код не найден: скопируйте проект в $APP_DIR и запустите скрипт снова." >&2
  echo "Например: rsync -a ./ $APP_DIR/" >&2
  exit 2
fi
chown -R "$BOT_USER":"$BOT_USER" "$APP_DIR"

step "4/9 Виртуальное окружение"
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
  su "$BOT_USER" -c "cd $APP_DIR && python3 -m venv venv"
fi
su "$BOT_USER" -c "cd $APP_DIR && ./venv/bin/pip install -q --upgrade pip && ./venv/bin/pip install -q -r requirements.txt"
echo "Зависимости установлены"

step "5/9 База данных"
systemctl enable --now postgresql >/dev/null 2>&1 || true
su postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'\"" | grep -q 1 \
  || su postgres -c "createuser --createdb $DB_USER"
su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='$DB_NAME'\"" | grep -q 1 \
  || su postgres -c "createdb -O $DB_USER $DB_NAME"
for extra in "${DB_NAME}_test"; do
  su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='$extra'\"" | grep -q 1 \
    || su postgres -c "createdb -O $DB_USER $extra"
done

if [ -n "$DUMP" ]; then
  step "5б Восстановление из дампа"
  [ -f "$DUMP" ] || { echo "Дамп не найден: $DUMP" >&2; exit 3; }
  su postgres -c "pg_restore --clean --if-exists --no-owner --no-privileges -d $DB_NAME '$DUMP'"
  echo "Дамп восстановлен"
fi

step "6/9 Миграции"
cd "$APP_DIR"
if [ -f .env ]; then
  ./venv/bin/python scripts/migrate.py || echo "Миграции не применились — проверьте .env"
else
  echo "Нет .env — миграции пропущены (сначала настройте .env)"
fi

step "7/9 Прайс"
if [ ! -f "$APP_DIR/catalog.xlsx" ]; then
  echo "⚠️ catalog.xlsx отсутствует — положите прайс в $APP_DIR"
else
  chown "$BOT_USER":"$BOT_USER" "$APP_DIR/catalog.xlsx"
fi

step "8/9 Службы"
for unit in order_bot.service order_bot_web.service order_bot_backup.service \
            order_bot_backup.timer order_bot_verify.service order_bot_verify.timer; do
  if [ -f "deploy/$unit" ]; then
    cp "deploy/$unit" /etc/systemd/system/
  fi
done
systemctl daemon-reload
systemctl enable order_bot.service order_bot_web.service >/dev/null 2>&1 || true
systemctl enable --now order_bot_backup.timer >/dev/null 2>&1 || true
systemctl enable --now order_bot_verify.timer >/dev/null 2>&1 || true

step "9/9 Проверка"
if [ -f .env ]; then
  systemctl restart order_bot.service order_bot_web.service || true
  sleep 5
  ./venv/bin/python scripts/doctor.py || true
else
  echo "Заполните .env. Нужны как минимум (имена — в .env.example):"
  sed -n '1,40p' .env.example 2>/dev/null || echo "  .env.example не найден"
fi

cat <<'TXT'

Дальше вручную:
1) заполнить /opt/order_bot/.env секретами (chmod 600, владелец orderbot);
2) systemctl restart order_bot order_bot_web;
3) venv/bin/python scripts/doctor.py — не должно быть ❌;
4) python scripts/env_check.py — имена и длины переменных без значений;
5) проверить бота в Telegram: /start, тестовый заказ, /health.
TXT
