#!/usr/bin/env python3
"""A11: проверка кнопок — каждой callback_data должен найтись обработчик.

Статический анализ (без Telegram): собираем все кнопки, которые создаёт код,
и все фильтры `F.data == ...` / `F.data.startswith(...)`, затем ищем кнопки
без обработчика. Такие кнопки в боте «мёртвые»: нажатие ничего не делает.

Запуск: venv/bin/python scripts/buttons_check.py
Код возврата 1 — есть мёртвые кнопки (test_all.sh считает это ошибкой).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SOURCES = [BASE_DIR / "orderbot", BASE_DIR / "main.py"]

BUTTON_RE = re.compile(r'callback_data\s*=\s*(f?)"([^"]+)"')
EXACT_RE = re.compile(r'F\.data\s*==\s*"([^"]+)"')
PREFIX_RE = re.compile(r'F\.data\.startswith\(\s*"([^"]+)"')
PREFIX_TUPLE_RE = re.compile(r'F\.data\.startswith\(\s*\(([^)]+)\)')
STRING_RE = re.compile(r'"([^"]+)"')


def files() -> list[Path]:
    result: list[Path] = []
    for source in SOURCES:
        if source.is_dir():
            result.extend(sorted(source.glob("*.py")))
        elif source.exists():
            result.append(source)
    return result


def key_of(raw: str, dynamic: bool) -> str:
    """Из «rday:yes:{user_id}» делаем «rday:yes:»."""
    if not dynamic:
        return raw
    cut = raw.find("{")
    return raw[:cut] if cut >= 0 else raw


def base(prefix: str) -> str:
    return prefix.split(":", 1)[0]


def collect() -> tuple[dict[str, list[str]], set[str], list[str]]:
    produced: dict[str, list[str]] = {}
    exact: set[str] = set()
    prefixes: list[str] = []
    for path in files():
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            for dynamic, raw in BUTTON_RE.findall(line):
                key = key_of(raw, dynamic == "f")
                if not key:
                    continue
                produced.setdefault(key, []).append(f"{path.name}:{number}")
            exact.update(EXACT_RE.findall(line))
            prefixes.extend(PREFIX_RE.findall(line))
            for group in PREFIX_TUPLE_RE.findall(line):
                prefixes.extend(STRING_RE.findall(group))
    return produced, exact, prefixes


def is_handled(key: str, exact: set[str], prefixes: list[str]) -> bool:
    if key in exact:
        return True
    for prefix in prefixes:
        if key.startswith(prefix):
            return True
    for handled in exact:
        if handled.startswith(key):
            return True
    return False


def main() -> int:
    produced, exact, prefixes = collect()
    dead = {
        key: places
        for key, places in sorted(produced.items())
        if not is_handled(key, exact, prefixes)
    }
    print(f"Кнопок в коде: {len(produced)}, обработчиков: {len(exact) + len(prefixes)}")
    if dead:
        print("Мёртвые кнопки (нет обработчика):")
        for key, places in dead.items():
            print(f"  ✗ {key} — {', '.join(places[:3])}")
        return 1
    print("Все кнопки имеют обработчики.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
