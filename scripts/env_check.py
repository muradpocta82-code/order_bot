#!/usr/bin/env python3
"""A8: гигиена секретов — печатает ИМЕНА переменных и длины, не значения.

Запуск: venv/bin/python scripts/env_check.py [--env .env] [--example .env.example]
Ничего не меняет, только смотрит:
- пустые значения;
- подозрительно короткие (токены ботов короче 30 символов и т.п.);
- пробелы и кавычки вокруг значения (частая причина «токен не принимается»);
- дубли имён (побеждает последнее — легко ошибиться);
- переменные из .env.example, которых нет в .env.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SHORT_LIMIT = 30
SECRET_HINTS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "DSN", "URL", "ID")


def parse_env(path: Path) -> tuple[dict[str, str], list[str], set[str]]:
    values: dict[str, str] = {}
    problems: list[str] = []
    duplicates: set[str] = set()
    if not path.exists():
        return values, [f"файл не найден: {path}"], duplicates
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        name, raw = text.split("=", 1)
        name = name.strip()
        value = raw.strip()
        if not name:
            problems.append(f"строка {number}: имя пустое")
            continue
        if name in values:
            duplicates.add(name)
        values[name] = value
        if not value:
            problems.append(f"{name}: пустое значение")
        elif value != raw.strip().strip('"').strip("'"):
            problems.append(f"{name}: кавычки, снимите их")
        elif value.strip() != value:
            problems.append(f"{name}: пробелы по краям")
        if any(hint in name.upper() for hint in SECRET_HINTS) and 0 < len(value) < SHORT_LIMIT:
            problems.append(f"{name}: короткое значение ({len(value)} символов) — похоже на обрезку")
    for name in sorted(duplicates):
        problems.append(f"{name}: задан дважды (действует последний)")
    return values, problems, duplicates


def read_example(path: Path) -> list[str]:
    if not path.exists():
        return []
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        names.append(text.split("=", 1)[0].strip())
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка .env без вывода значений")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--example", default=".env.example")
    args = parser.parse_args()
    values, problems, _dupes = parse_env(Path(args.env))
    example = read_example(Path(args.example))
    print(f"Переменных в {args.env}: {len(values)}")
    if example:
        missing = [name for name in example if name not in values]
        print(f"В .env.example: {len(example)}, нет в .env: {len(missing)}")
        for name in missing:
            print(f"  · {name} — не задана")
    print("")
    for name in sorted(values):
        value = values[name]
        print(f"  {name:<32} длина {len(value)}")
    print("")
    if problems:
        print("Замечания:")
        for problem in problems:
            print(f"  ⚠️ {problem}")
        return 1
    print("Замечаний нет.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
