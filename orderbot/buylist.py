"""Закупочный лист на день: сколько чего везти, с учётом остатков.

Считается из заказов на дату доставки (всё, что ещё не отменено и не закрыто),
минус остаток со склада. Выводится текстом и файлом Excel — чтобы можно было
переслать поставщику или распечатать.
"""
from __future__ import annotations

from io import BytesIO
from typing import Any

from .catalog import Catalog
from .db import Database
from .formatting import money

ACTIVE_STATUSES = ("new", "accepted", "ready")


def _fmt_qty(value: float) -> str:
    if abs(value - round(value)) < 0.001:
        return f"{int(round(value))}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


async def collect(
    db: Database, catalog: Catalog, day: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(строки закупки, заказы, которые ещё не приняты)."""
    orders = await db.orders_for_date(day)
    active = [order for order in orders if str(order.get("status")) in ACTIVE_STATUSES]
    pending = [order for order in active if str(order.get("status")) == "new"]
    stock = await db.stock_overrides()

    totals: dict[Any, dict[str, Any]] = {}
    for order in active:
        for item in order.get("items") or []:
            key = item.get("catalog_id") or f"name:{item.get('name')}"
            entry = totals.setdefault(
                key,
                {
                    "catalog_id": item.get("catalog_id"),
                    "name": item.get("name") or "—",
                    "unit": item.get("unit") or "",
                    "qty": 0.0,
                    "orders": 0,
                },
            )
            entry["qty"] += float(item.get("qty") or 0)
            entry["orders"] += 1

    rows: list[dict[str, Any]] = []
    for key, entry in totals.items():
        item = catalog.get(entry["catalog_id"]) if entry["catalog_id"] else None
        over = stock.get(int(entry["catalog_id"])) if entry["catalog_id"] else None
        stock_left = None
        available = True
        if over:
            available = bool(over.get("available", True))
            stock_left = float(over["stock_left"]) if over.get("stock_left") is not None else None
        need = entry["qty"] - (stock_left or 0.0)
        rows.append(
            {
                **entry,
                "category": (item.category if item else "") or "Без категории",
                "price": float(item.price or 0) if item else 0.0,
                "purchase": float(item.purchase or 0) if item and item.purchase else 0.0,
                "supplier": (item.supplier if item else "") or "",
                "stock_left": stock_left,
                "available": available,
                "need": max(need, 0.0),
            }
        )
    rows.sort(key=lambda row: (row["category"], row["name"]))
    # F8: прогноз по дню недели — «сколько обычно уходит в этот день».
    try:
        from datetime import date as _date

        from .forecast import add_forecasts

        target = _date.fromisoformat(str(day))
        history = await db.weekday_history(56)
        add_forecasts(rows, history, target.weekday())
    except Exception:
        for row in rows:
            row.setdefault("forecast", {"ready": False, "samples": 0, "avg": 0.0})
    return rows, pending


def as_text(rows: list[dict[str, Any]], pending: list[dict[str, Any]], day: str) -> str:
    if not rows:
        return f"🧺 <b>Закупка на {day}</b>\n\nЗаказов нет — закупать нечего."
    total_sum = sum(row["need"] * row["purchase"] for row in rows)
    from .forecast import summary_lines

    forecast_lines = summary_lines(rows)
    lines = [f"🧺 <b>Закупка на {day}</b>", ""]
    by_supplier = any(row.get("supplier") for row in rows)
    section = None
    for row in sorted(
        rows,
        key=lambda r: (
            (r.get("supplier") or "Я без поставщика") if by_supplier else r["category"],
            r["category"],
            r["name"],
        ),
    ):
        key = (row.get("supplier") or "Я без поставщика") if by_supplier else row["category"]
        if key != section:
            section = key
            lines.append(f"<b>{key}</b>" if by_supplier else f"<b>{key}</b>")
        parts = [f"• {row['name']} — <b>{_fmt_qty(row['need'])} {row['unit']}</b>"]
        if by_supplier and row["category"]:
            parts.append(f"[{row['category']}]")
        if row["stock_left"]:
            parts.append(f"(заказано {_fmt_qty(row['qty'])}, остаток {_fmt_qty(row['stock_left'])})")
        if not row["available"]:
            parts.append("⚠️ нет в наличии")
        lines.append(" ".join(parts))
    if forecast_lines:
        lines.append("")
        lines.append("📈 Обычно в этот день недели:")
        lines.extend(forecast_lines)
    if total_sum:
        lines.append("")
        lines.append(f"По закупочным ценам из прайса: <b>{money(total_sum)}</b>")
    if pending:
        ids = ", ".join(f"№{order['id']}" for order in pending[:8])
        more = f" и ещё {len(pending) - 8}" if len(pending) > 8 else ""
        lines.append("")
        lines.append(f"⏳ Не приняты: {ids}{more} — их количество может измениться")
    return "\n".join(lines)


def as_xlsx(rows: list[dict[str, Any]], day: str) -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Закупка"
    sheet.append(["Дата доставки", day])
    sheet.append([])
    sheet.append(["Поставщик", "Категория", "ID", "Название", "Ед.", "Заказано", "Остаток", "Закупить", "Закупка $"])
    for row in rows:
        sheet.append(
            [
                row.get("supplier") or "",
                row["category"],
                row["catalog_id"],
                row["name"],
                row["unit"],
                round(row["qty"], 3),
                row["stock_left"],
                round(row["need"], 3),
                round(row["need"] * row["purchase"], 2) if row["purchase"] else None,
            ]
        )
    for column, width in zip("ABCDEFGHI", (18, 18, 6, 38, 6, 10, 10, 10, 12)):
        sheet.column_dimensions[column].width = width
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
