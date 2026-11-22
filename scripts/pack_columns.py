"""Колонки фасовки в прайсе (E6).

Размер и единицу фасовки можно править прямо в Excel: бот берёт их из
колонок «Размер фасовки» и «Фасовка, ед» и не требует деплоя.
Скрипт один раз заполняет эти колонки из текста «Фасовка», чтобы менеджеру
было что править.

  venv/bin/python scripts/pack_columns.py            # только показать
  venv/bin/python scripts/pack_columns.py --fill     # записать в прайс
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from openpyxl import load_workbook

BASE_DIR = Path(__file__).resolve().parent.parent
CATALOG = BASE_DIR / "catalog.xlsx"

SIZE_HEADER = "Размер фасовки"
UNIT_HEADER = "Фасовка, ед"
PACK_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([а-яё]+)")


def _header_map(ws) -> dict[str, int]:
    """Название колонки (нормализованное) → номер колонки (1…)."""
    result: dict[str, int] = {}
    for index, cell in enumerate(
        next(ws.iter_rows(min_row=1, max_row=1, values_only=True)), start=1
    ):
        title = str(cell or "").strip().lower().replace("ё", "е")
        if title:
            result.setdefault(title, index)
    return result


def _find(header: dict[str, int], predicate) -> int | None:
    for title, index in header.items():
        if predicate(title):
            return index
    return None


def main(argv: list[str]) -> int:
    fill = "--fill" in argv
    path = Path(argv[argv.index("--file") + 1]) if "--file" in argv else CATALOG
    if not path.exists():
        print(f"Прайс не найден: {path}")
        return 2
    wb = load_workbook(path)
    ws = wb.active
    header = _header_map(ws)
    pack_col = _find(
        header,
        lambda t: t == "фасовка" or (t.startswith("фасов") and "ед" not in t and "размер" not in t),
    )
    unit_col = _find(header, lambda t: t.startswith("единиц") or t == "ед")
    size_col = _find(header, lambda t: t.startswith("размер"))
    pack_unit_col = _find(
        header, lambda t: t.startswith("фасов") and ("ед" in t or "единиц" in t)
    )
    if pack_col is None:
        print("В прайсе нет колонки «Фасовка»")
        return 2
    last = ws.max_column
    if size_col is None:
        last += 1
        size_col = last
        ws.cell(row=1, column=size_col, value=SIZE_HEADER)
    if pack_unit_col is None or pack_unit_col == pack_col:
        last += 1
        pack_unit_col = last
        ws.cell(row=1, column=pack_unit_col, value=UNIT_HEADER)

    filled = 0
    skipped = 0
    for row in range(2, ws.max_row + 1):
        if ws.cell(row=row, column=size_col).value not in (None, ""):
            skipped += 1
            continue
        text = str(ws.cell(row=row, column=pack_col).value or "").strip()
        match = PACK_RE.search(text.lower().replace("ё", "е"))
        if not match:
            skipped += 1
            continue
        try:
            size = float(match.group(1).replace(",", "."))
        except ValueError:
            skipped += 1
            continue
        if size <= 0:
            skipped += 1
            continue
        if fill:
            ws.cell(row=row, column=size_col, value=size)
            ws.cell(row=row, column=pack_unit_col, value=match.group(2))
        filled += 1

    if fill:
        wb.save(path)
    wb.close()
    print(f"{'Заполнено' if fill else 'Можно заполнить'}: {filled}, без разбора: {skipped}")
    print(f"Файл: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
