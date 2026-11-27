#!/usr/bin/env bash
# Проверка «в репозитории нет секретов» (A3).
#
# Смотрит только файлы под git (git ls-files):
#   1) ищет типовые секреты в содержимом — токены Telegram-ботов, ключи Google,
#      пароли в строке подключения, токены GitHub/Slack, ping-ссылки мониторинга;
#   2) проверяет, что в репозиторий не попали файлы, которых там быть не должно:
#      ключи TLS, приватные ключи SSH, ключи сервисных аккаунтов, .env.
#
# Заглушки в тестах и примерах (123456789:AAAA…, {password}, $VAR, example) не
# считаются утечкой. Запускается из scripts/test_all.sh перед деплоем.
#
# Запуск:  bash scripts/secret_scan.sh
set -uo pipefail
cd "$(dirname "$0")/.."

PATTERNS=(
  '[0-9]{8,10}:AA[0-9A-Za-z_-]{30,}'     # токен Telegram-бота
  'AIza[0-9A-Za-z_-]{30,}'               # ключ Google API
  'postgresql://[A-Za-z0-9_]+:[^@$/ ]+@' # пароль в строке подключения
  'ghp_[0-9A-Za-z]{30,}'                 # токен GitHub
  'xox[baprs]-[0-9A-Za-z-]{10,}'         # токен Slack
  'https://hc-ping\.com/[0-9a-f-]{30,}'  # ping-ссылка мониторинга
)

# Заглушки: всё, что заведомо не секрет
PLACEHOLDER='A{16,}|x{6,}|X{6,}|example|заглушк|placeholder|\*{4,}|\.\.\.|\{[a-zA-Z_]+\}|\$[A-Za-z_]+|password|PASSWORD'

found=0

echo "=== содержимое файлов ==="
for pattern in "${PATTERNS[@]}"; do
  hits="$(git ls-files -z | xargs -0 grep -nIE "$pattern" 2>/dev/null || true)"
  [ -n "$hits" ] || continue
  hits="$(printf '%s\n' "$hits" | grep -vE '^(\.env\.example|docs/)' | grep -vE "$PLACEHOLDER" || true)"
  [ -n "$hits" ] || continue
  echo "❌ похоже на секрет (${pattern}):"
  printf '%s\n' "$hits" | sed 's/^/   /'
  found=1
done

echo "=== файлы, которых не должно быть в git ==="
BAD_FILES="$(
  git ls-files \
    | grep -Ei '\.(pem|key|p12|pfx|crt)$|(^|/)(id_rsa|id_ed25519)(\.pub)?$|google[-_]?sa.*\.json$|rclone\.conf$|(^|/)\.env$|(^|/)\.env\.[a-z]+$' \
    | grep -v '\.env\.example$' || true
)"
if [ -n "$BAD_FILES" ]; then
  echo "❌ в репозитории файлы с секретами:"
  printf '%s\n' "$BAD_FILES" | sed 's/^/   /'
  found=1
fi

if [ "$found" = "1" ]; then
  echo ""
  echo "Уберите секрет из файлов и из истории (docs/PRIVATE_REPO.md)."
  exit 1
fi

echo "✅ секретов в отслеживаемых файлах нет"
