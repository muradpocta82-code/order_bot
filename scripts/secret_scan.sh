#!/usr/bin/env bash
# Проверка «в репозитории нет секретов» (A3).
#
# Смотрит только файлы под git (git ls-files) и ищет типовые секреты:
# токены Telegram-ботов, ключи Google, пароли в строке подключения, приватные ключи.
# Запускается в scripts/test_all.sh, поэтому утечка не уедет в репозиторий незамеченной.
#
# Запуск:  bash scripts/secret_scan.sh
set -uo pipefail
cd "$(dirname "$0")/.."

PATTERNS=(
  '[0-9]{8,10}:AA[0-9A-Za-z_-]{30,}'                 # токен Telegram-бота
  'AIza[0-9A-Za-z_-]{30,}'                           # ключ Google API
  'postgresql://[A-Za-z0-9_]+:[^@ ]+@'               # пароль в строке подключения
  'BEGIN [A-Z ]*PRIVATE KEY'                         # приватный ключ TLS/SSH
  'ghp_[0-9A-Za-z]{30,}'                             # токен GitHub
  'xox[baprs]-[0-9A-Za-z-]{10,}'                     # токен Slack
  'https://hc-ping\.com/[0-9a-f-]{30,}'              # ping-ссылка мониторинга
)

# Заглушки в тестах и примерах: 123456789:AAAA…, xxxx, example, <…>
PLACEHOLDER='A{16,}|x{6,}|X{6,}|example|заглушк|placeholder|\*{4,}|\.\.\.'

found=0
for pattern in "${PATTERNS[@]}"; do
  hits="$(git ls-files -z | xargs -0 grep -nIE "$pattern" 2>/dev/null || true)"
  [ -n "$hits" ] || continue
  hits="$(printf '%s\n' "$hits" | grep -vE '^(\.env\.example|docs/)' | grep -vE "$PLACEHOLDER" || true)"
  [ -n "$hits" ] || continue
  echo "❌ похоже на секрет (${pattern}):"
  printf '%s\n' "$hits" | sed 's/^/   /'
  found=1
done

if [ "$found" = "1" ]; then
  echo ""
  echo "Уберите секрет из файлов и из истории (docs/PRIVATE_REPO.md)."
  exit 1
fi

echo "✅ секретов в отслеживаемых файлах нет"
