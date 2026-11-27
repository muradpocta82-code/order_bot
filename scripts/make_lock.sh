#!/usr/bin/env bash
# A3: фиксируем точные версии пакетов (requirements.lock) и проверяем, что
# requirements.txt согласован с окружением.
#
# Запуск на сервере от root:  bash scripts/make_lock.sh
set -euo pipefail
cd "$(dirname "$0")/.."

venv/bin/python -m pip freeze --exclude-editable > requirements.lock
COUNT=$(grep -c . requirements.lock)
echo "✅ requirements.lock обновлён: $COUNT пакетов"

MISSING=$(venv/bin/python - <<'PY'
import sys
from pathlib import Path

pins = {}
for line in Path("requirements.txt").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "==" not in line:
        continue
    name, version = line.split("==", 1)
    name = name.split("[", 1)[0]  # qrcode[pil] — extras в имени не нужны
    pins[name.lower().replace("_", "-")] = version.strip()

lock = {}
for line in Path("requirements.lock").read_text().splitlines():
    if "==" not in line:
        continue
    name, version = line.split("==", 1)
    name = name.split("[", 1)[0]
    lock[name.lower().replace("_", "-")] = version.strip()

bad = [f"{n}=={v} (в окружении {lock.get(n, '—')})" for n, v in pins.items() if lock.get(n) != v]
print("\n".join(bad))
PY
)
if [ -n "$MISSING" ]; then
  echo "⚠️ requirements.txt расходится с окружением:"
  echo "$MISSING"
  exit 1
fi
echo "✅ requirements.txt совпадает с окружением"
