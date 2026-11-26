#!/usr/bin/env python3
"""Проверка «деньги не float» (I6).

Смотрит аннотации функций в модулях про деньги: если функция называется
amount/price/total/debt/paid/credited/... и возвращает или принимает `float`,
это попадает в отчёт. Новые случаи блокируют деплой, старые записаны в базовой
линии `tests/.money_baseline.json` — их чиним постепенно.

  venv/bin/python scripts/money_check.py             # показать нарушения
  venv/bin/python scripts/money_check.py --update    # перезаписать базовую линию
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
TARGETS = [
    "orderbot/ledger.py",
    "orderbot/statement.py",
    "orderbot/invoice.py",
    "orderbot/margins.py",
    "orderbot/receipts.py",
    "orderbot/pilot.py",
    "orderbot/forecast.py",
    "orderbot/writeoff.py",
]
MONEY_WORDS = (
    "amount",
    "price",
    "total",
    "debt",
    "paid",
    "credit",
    "sum",
    "revenue",
    "cost",
    "margin",
    "balance",
)
BASELINE = BASE_DIR / "tests" / ".money_baseline.json"


def _float_annotation(node: ast.AST | None) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Name):
        return node.id == "float"
    if isinstance(node, ast.Constant):
        return str(node.value) == "float"
    if isinstance(node, ast.BinOp):
        return _float_annotation(node.left) or _float_annotation(node.right)
    if isinstance(node, ast.Subscript):
        return _float_annotation(node.value)
    if isinstance(node, ast.Tuple):
        return any(_float_annotation(item) for item in node.elts)
    return False


def scan(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        return [f"{path}: не прочитан ({exc})"]
    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        name = node.name.lower()
        if not any(word in name for word in MONEY_WORDS):
            continue
        if _float_annotation(node.returns):
            problems.append(f"{path.name}:{node.lineno} {node.name}() -> float")
        for arg in list(node.args.args) + list(node.args.kwonlyargs):
            if _float_annotation(arg.annotation):
                problems.append(f"{path.name}:{node.lineno} {node.name}({arg.arg}: float)")
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Деньги не float (I6)")
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args(argv)
    problems: list[str] = []
    for relative in TARGETS:
        path = BASE_DIR / relative
        if not path.exists():
            continue
        problems.extend(scan(path))
    problems.sort()
    if args.update:
        BASELINE.write_text(
            json.dumps(problems, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"База обновлена: {len(problems)} записей")
        return 0
    known: list[str] = []
    if BASELINE.exists():
        try:
            known = json.loads(BASELINE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            known = []
    fresh = [item for item in problems if item not in set(known)]
    fixed = [item for item in known if item not in set(problems)]
    print(f"Деньги с float: {len(problems)} (в базе {len(known)})")
    for item in fresh:
        print(f"  ❌ новое: {item}")
    for item in fixed[:5]:
        print(f"  ✅ исправлено: {item}")
    if fresh:
        print("Новые деньги через float запрещены — используйте Decimal.")
        return 1
    print("Новых нарушений нет.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
