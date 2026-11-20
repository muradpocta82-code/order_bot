"""Добавление новой позиции в прайс-файл (B11, кнопка «Добавить в каталог»).

Пишет строку в конец `catalog.xlsx`: id = максимальный + 1, название, цена,
единица, фасовка, категория, остаток. Колонки ищутся по заголовкам, поэтому
порядок в файле не важен. Перед правкой вызывающий код делает архивную копию.
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook

from .catalog import Catalog, normalize
from .units import canon_unit


def _column(lowered: list[str], *names: str) -> int | None:
    for index, title in enumerate(lowered):
        for name in names:
            if title == name or title.startswith(name):
                return index + 1
    return None


def append_item(
    path: str | Path,
    name: str,
    price: float,
    unit: str = "шт",
    pack: str = "",
    category: str = "",
    stock: float | None = None,
) -> int:
    """Дописывает позицию в прайс и возвращает её id."""
    path = Path(path)
    workbook = load_workbook(path)
    worksheet = workbook.worksheets[0]
    header = [str(cell.value or "").strip() for cell in worksheet[1]]
    lowered = [normalize(title) for title in header]
    name_col = _column(lowered, "назван", "наименование", "name", "товар")
    price_col = _column(lowered, "цена", "price", "стоимость")
    if not name_col or not price_col:
        workbook.close()
        raise ValueError("В прайсе нет колонок «Название» и «Цена»")
    id_col = _column(lowered, "id", "код", "артикул")
    unit_col = _column(lowered, "единиц", "ед", "unit")
    pack_col = _column(lowered, "фасов", "pack", "упаковка")
    category_col = _column(lowered, "категор")
    stock_col = _column(lowered, "остат", "stock", "кол-во")

    ids: list[int] = []
    if id_col:
        for row in range(2, worksheet.max_row + 1):
            raw = worksheet.cell(row=row, column=id_col).value
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                continue
    new_id = (max(ids) if ids else 1000) + 1

    row = worksheet.max_row + 1
    while row > 2 and all(
        worksheet.cell(row=row - 1, column=column).value in (None, "")
        for column in range(1, worksheet.max_column + 1)
    ):
        row -= 1

    def put(column: int | None, value) -> None:
        if column:
            worksheet.cell(row=row, column=column).value = value

    if id_col:
        put(id_col, new_id)
    put(name_col, str(name).strip())
    put(price_col, round(float(price), 2))
    price_cell = worksheet.cell(row=row, column=price_col)
    price_cell.number_format = "$#,##0.00"
    put(unit_col, canon_unit(unit) or (unit or "шт"))
    if pack:
        put(pack_col, pack)
    if category:
        put(category_col, category)
    if stock is not None:
        put(stock_col, float(stock))
    workbook.save(path)
    workbook.close()
    return new_id


def reload_into(catalog: Catalog, path: str | Path) -> Catalog:
    """Перечитывает прайс и переносит данные в уже работающий объект."""
    fresh = Catalog.load(path)
    catalog.items = fresh.items
    catalog.by_id = fresh.by_id
    catalog._phrases = fresh._phrases
    return catalog
