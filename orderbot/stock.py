"""G6: остатки — что скрывать, чего заканчивается и как списывать.

Правила простые:
- позиция скрыта у клиента, если её сняли с продажи (`/stop`) или остаток дошёл до нуля;
- «заканчивается» — остаток меньше порога (`stock_warn_threshold`, по умолчанию 5):
  такие позиции показывает `/lowstock` и утренняя сводка;
- списание (`/writeoff`) уменьшает остаток, если единица совпадает с единицей прайса.

Остаток берётся из базы (`stock_items`), а если там пусто — из колонки «Остаток» в прайсе.
Здесь только чистая логика, чтобы её покрывали тесты.
"""
from __future__ import annotations

from dataclasses import dataclass

from .catalog import Catalog
from .units import canon_unit
from .units import format_qty, plural

WARN_KEY = "stock_warn_threshold"
DEFAULT_WARN = 5.0


@dataclass(frozen=True)
class StockRow:
    catalog_id: int
    name: str
    category: str
    unit: str
    left: float | None
    available: bool

    @property
    def stopped(self) -> bool:
        return not self.available

    @property
    def empty(self) -> bool:
        return self.left is not None and self.left <= 0

    @property
    def hidden(self) -> bool:
        return self.stopped or self.empty


def resolve(catalog: Catalog, overrides: dict[int, dict]) -> dict[int, StockRow]:
    """Сводит остатки: база важнее колонки в прайсе, флаг «снято» важнее всего."""
    rows: dict[int, StockRow] = {}
    for item in catalog.items:
        info = overrides.get(item.id) or {}
        available = bool(info.get("available", True))
        left = info.get("stock_left")
        if left is None:
            left = item.stock
        rows[item.id] = StockRow(
            catalog_id=item.id,
            name=item.name,
            category=item.category or "",
            unit=item.unit,
            left=float(left) if left is not None else None,
            available=available,
        )
    return rows


def hidden_ids(rows: dict[int, StockRow]) -> set[int]:
    return {item_id for item_id, row in rows.items() if row.hidden}


def low_stock(rows: dict[int, StockRow], threshold: float) -> list[StockRow]:
    """Позиции на исходе: 0 < остаток ≤ порога и ещё продаются."""
    result = [
        row
        for row in rows.values()
        if row.available and row.left is not None and 0 < float(row.left) <= threshold
    ]
    result.sort(key=lambda row: (float(row.left or 0), row.name))
    return result


def low_stock_text(rows: list[StockRow], threshold: float, limit: int = 25) -> str:
    header = f"📦 <b>Заканчивается</b> (порог {threshold:g})"
    if not rows:
        return header + "\n\nНичего на исходе — остатки в порядке."
    lines = [header, ""]
    for row in rows[:limit]:
        left = float(row.left or 0)
        lines.append(f"• {row.name} — осталось {format_qty(left)} {plural(row.unit, left)}")
    if len(rows) > limit:
        lines.append(f"…и ещё {len(rows) - limit}")
    lines.append("")
    lines.append("Приход или правку остатка делать здесь же: /stop и /start_item,")
    lines.append("списание — /writeoff помидоры 3 кг усушка")
    return "\n".join(lines)


def summary_line(rows: list[StockRow]) -> str:
    if not rows:
        return ""
    names = ", ".join(row.name for row in rows[:3])
    tail = f" и ещё {len(rows) - 3}" if len(rows) > 3 else ""
    return f"📦 Заканчивается: {len(rows)} поз. ({names}{tail}) — /lowstock"


def writeoff_effect(row: StockRow | None, unit: str) -> str:
    """Что скажем менеджеру про остаток после списания."""
    if row is None:
        return "остаток неизвестен — позиции нет в прайсе"
    if (row.unit or "").strip().lower() != (unit or "").strip().lower():
        return (
            f"остаток не менял: списываем в «{unit}», а на складе «{row.unit}»"
        )
    if row.left is None:
        return "остаток не менял: в прайсе не заполнен"
    return ""


SALES_KEY = "stock_consume_sales"


def units_match(left, right) -> bool:
    """Совпадают ли единицы, чтобы менять остаток (кг/кг — да, уп/кг — нет)."""
    raw_left = str(left or "").strip().lower()
    raw_right = str(right or "").strip().lower()
    if not raw_left or not raw_right:
        return True  # единица не указана — считаем как в прайсе
    one = canon_unit(raw_left)
    two = canon_unit(raw_right)
    if one and two:
        return one == two
    return raw_left == raw_right


async def consume_order(db, catalog: Catalog, order: dict) -> tuple[int, list[str]]:
    """Списывает остатки по доставленному заказу (G6, продолжение).

    Возвращает (сколько позиций списано, заметки для журнала). Если позиция
    заказана в другой единице, остаток не трогаем — единицы не пересчитываем.
    """
    if (await db.get_setting(SALES_KEY, "1")) != "1":
        return 0, []
    rows = await current(catalog, db)
    consumed = 0
    notes: list[str] = []
    for item in order.get("items") or []:
        catalog_id = item.get("catalog_id")
        if not catalog_id:
            continue
        catalog_item = catalog.get(int(catalog_id))
        if catalog_item is None:
            continue
        try:
            qty = float(item.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        if not units_match(item.get("unit"), catalog_item.unit):
            notes.append(
                f"{catalog_item.name}: «{item.get('unit')}» ≠ «{catalog_item.unit}» — остаток не менял"
            )
            continue
        row = rows.get(int(catalog_id))
        before = float(row.left) if row and row.left is not None else None
        left = await db.adjust_stock(int(catalog_id), -qty, base=catalog_item.stock)
        consumed += 1
        was = f"{before:g} → " if before is not None else ""
        notes.append(f"{catalog_item.name}: {was}{left:g} {catalog_item.unit}")
    return consumed, notes


async def threshold(db) -> float:
    raw = (await db.get_setting(WARN_KEY, "") or "").strip()
    if not raw:
        return DEFAULT_WARN
    try:
        return max(0.0, round(float(raw.replace(",", ".")), 2))
    except ValueError:
        return DEFAULT_WARN


async def current(catalog: Catalog, db) -> dict[int, StockRow]:
    return resolve(catalog, await db.stock_overrides())
