#!/usr/bin/env bash
# Копия бэкапа базы вне сервера (A2). Готово к работе, но требует настроенного rclone.
#
# Что делает:
#   1. берёт самый свежий дамп из /var/backups/order_bot;
#   2. отправляет его в удалённое хранилище (rclone, лучше crypt-remote с шифрованием);
#   3. проверяет, что файл доехал (размер совпадает);
#   4. если задан HEARTBEAT_URL — пингует Healthchecks, при ошибке шлёт /fail.
#
# Запускается таймером order_bot_offsite.timer раз в сутки (см. deploy/),
# но включать его нужно только после настройки rclone — см. docs/OFFSITE_BACKUP.md.
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/order_bot}"
REMOTE="${OFFSITE_REMOTE:-}"          # например: b2crypt:orderbot-backups
KEEP_DAYS="${OFFSITE_KEEP_DAYS:-30}"
ENV_FILE="$BASE_DIR/.env"
LOG_PREFIX="offsite-backup"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $LOG_PREFIX: $*"; }
fail() {
  log "ОШИБКА: $*"
  if [ -n "${HEARTBEAT_URL:-}" ]; then
    curl -fsS -m 10 --retry 2 "${HEARTBEAT_URL%/}/fail" >/dev/null 2>&1 || true
  fi
  exit 1
}

# HEARTBEAT_URL берём из окружения или из .env (только имя переменной, не печатаем значение)
if [ -z "${HEARTBEAT_URL:-}" ] && [ -f "$ENV_FILE" ]; then
  HEARTBEAT_URL="$(grep -E '^HEARTBEAT_URL=' "$ENV_FILE" | cut -d= -f2- || true)"
fi
if [ -z "${REMOTE}" ] && [ -f "$ENV_FILE" ]; then
  REMOTE="$(grep -E '^OFFSITE_REMOTE=' "$ENV_FILE" | cut -d= -f2- || true)"
fi

if [ -z "${REMOTE}" ]; then
  log "OFFSITE_REMOTE не задан — копия вне сервера не настроена."
  log "Как настроить: docs/OFFSITE_BACKUP.md (нужен аккаунт облака и rclone config)."
  exit 0
fi

command -v rclone >/dev/null 2>&1 || fail "rclone не установлен (apt install rclone)"

LATEST="$(ls -1t "$BACKUP_DIR"/orderbot-pg-*.dump 2>/dev/null | head -1 || true)"
[ -n "$LATEST" ] || fail "нет дампов в $BACKUP_DIR"
log "отправляю $(basename "$LATEST") ($(du -h "$LATEST" | cut -f1)) → $REMOTE"

if ! rclone copy "$LATEST" "$REMOTE" --no-traverse --stats-one-line >/dev/null 2>&1; then
  fail "rclone не смог отправить файл"
fi

REMOTE_SIZE="$(rclone size "$REMOTE/$(basename "$LATEST")" --json 2>/dev/null \
  | sed -n 's/.*"bytes":\([0-9]*\).*/\1/p' || true)"
LOCAL_SIZE="$(stat -c%s "$LATEST")"
if [ -n "$REMOTE_SIZE" ] && [ "$REMOTE_SIZE" != "$LOCAL_SIZE" ]; then
  fail "размер в облаке ($REMOTE_SIZE) не совпал с локальным ($LOCAL_SIZE)"
fi

# Чистим старое в облаке
rclone delete "$REMOTE" --min-age "${KEEP_DAYS}d" >/dev/null 2>&1 || true

if [ -n "${HEARTBEAT_URL:-}" ]; then
  curl -fsS -m 10 --retry 2 "$HEARTBEAT_URL" >/dev/null 2>&1 \
    || log "предупреждение: пинг Healthchecks не прошёл"
fi

log "готово: копия вне сервера обновлена"
