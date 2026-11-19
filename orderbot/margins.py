"""Маржа по срезам: позиция, категория, клиент (F7).

Считает по трём разрезам одновременно и отдельно показывает убыточные
и низкомаржинальные позиции с объёмом — раньше в `/margin` была только
общая цифра за период.

Деньги считаются в валюте прайса (USD), количество — в единицах позиции.
"""
from __future__ import annotations

from typing import Any, Iterable

LOW_MARGIN_PCT = 10.0   # ниже этого процента позиция считается слабой


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _percent(part: float, whole: float) -> float:
    return (part / whole * 100) if whole else 0.0


def collect(
    rows: Iterable[dict[str, Any]],
    catalog: Any,
    costs: dict[int, float],
) -> dict[str, Any]:
    """Собирает разрезы из проданных позиций.

    rows — строки `db.sold_items` (catalog_id, name, qty, price, chef_id, chef_name),
    catalog — прайс (нужен для категории и закупки из файла),
    costs — закупочные цены из базы (catalog_id → цена).
    """
    totals = {"revenue": 0.0, "cost": 0.0, "unknown": 0, "items": 0}
    positions: dict[int, dict[str, Any]] = {}
    categories: dict[str, dict[str, float]] = {}
    clients: dict[int, dict[str, Any]] = {}

    for row in rows:
        try:
            qty = float(row.get("qty") or 0)
            price = float(row.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        raw_id = row.get("catalog_id")
        catalog_id = int(raw_id) if raw_id else 0
        item = catalog.get(catalog_id) if catalog_id else None
        name = str(row.get("name") or (item.name if item else "позиция"))
        revenue = qty * price
        purchase = costs.get(catalog_id) if catalog_id else None
        if purchase is None and item is not None and item.purchase:
            purchase = float(item.purchase)
        cost = qty * float(purchase) if purchase is not None else 0.0
        if purchase is None:
            totals["unknown"] += 1

        totals["revenue"] += revenue
        totals["cost"] += cost
        totals["items"] += 1

        entry = positions.setdefault(
            catalog_id or -len(positions) - 1,
            {"name": name, "qty": 0.0, "revenue": 0.0, "cost": 0.0, "known": True},
        )
        entry["qty"] += qty
        entry["revenue"] += revenue
        entry["cost"] += cost
        if purchase is None:
            entry["known"] = False

        category = str(getattr(item, "category", "") or "без категории")
        bucket = categories.setdefault(category, {"revenue": 0.0, "cost": 0.0})
        bucket["revenue"] += revenue
        bucket["cost"] += cost

        raw_chef = row.get("chef_id")
        chef_id = int(raw_chef) if raw_chef else 0
        client = clients.setdefault(
            chef_id,
            {
                "name": str(row.get("chef_name") or f"id{chef_id}"),
                "revenue": 0.0,
                "cost": 0.0,
                "orders": 0,
            },
        )
        client["revenue"] += revenue
        client["cost"] += cost

    for entry in positions.values():
        entry["margin"] = entry["revenue"] - entry["cost"]
        entry["margin_pct"] = _percent(entry["margin"], entry["revenue"])
    for bucket in categories.values():
        bucket["margin"] = bucket["revenue"] - bucket["cost"]
        bucket["margin_pct"] = _percent(bucket["margin"], bucket["revenue"])
    for client in clients.values():
        client["margin"] = client["revenue"] - client["cost"]
        client["margin_pct"] = _percent(client["margin"], client["revenue"])

    by_margin = sorted(positions.values(), key=lambda e: (-e["margin"], e["name"]))
    lossy = [entry for entry in by_margin if entry["known"] and entry["margin"] < 0]
    weak = [
        entry
        for entry in by_margin
        if entry["known"]
        and entry["margin"] >= 0
        and entry["margin_pct"] < LOW_MARGIN_PCT
    ]
    return {
        "totals": totals,
        "margin": totals["revenue"] - totals["cost"],
        "positions": by_margin,
        "categories": sorted(
            categories.items(), key=lambda kv: -kv[1]["margin"]
        ),
        "clients": sorted(
            ((key, value) for key, value in clients.items()),
            key=lambda kv: -kv[1]["margin"],
        ),
        "lossy": lossy,
        "weak": weak,
    }


def report_lines(data: dict[str, Any], top: int = 3) -> list[str]:
    """Текст отчёта по срезам в HTML-разметке Telegram."""
    lines: list[str] = []
    categories = data["categories"]
    if categories:
        lines.append("")
        lines.append("<b>Категории</b>")
        for name, bucket in categories[:top + 2]:
            lines.append(
                f"• {name}: {_money(bucket['margin'])} "
                f"({bucket['margin_pct']:.0f}%)"
            )
    clients = data["clients"]
    if clients:
        lines.append("")
        lines.append("<b>Клиенты</b>")
        for _client_id, client in clients[:top]:
            lines.append(
                f"• {client['name']}: {_money(client['margin'])} "
                f"({client['margin_pct']:.0f}%)"
            )
    positions = data["positions"]
    if positions:
        lines.append("")
        lines.append("<b>Позиции</b>")
        for entry in positions[:top]:
            lines.append(
                f"• {entry['name']}: {_money(entry['margin'])} "
                f"({entry['margin_pct']:.0f}%), объём {entry['qty']:g}"
            )
    lossy = data["lossy"]
    if lossy:
        lines.append("")
        lines.append("⚠️ <b>Убыточные позиции</b>")
        for entry in lossy[:5]:
            lines.append(
                f"• {entry['name']}: {_money(entry['margin'])}, объём {entry['qty']:g}"
            )
    weak = data["weak"]
    if weak:
        lines.append("")
        lines.append(f"<b>Маржа ниже {LOW_MARGIN_PCT:.0f}%</b>")
        for entry in weak[:5]:
            lines.append(
                f"• {entry['name']}: {entry['margin_pct']:.0f}%, объём {entry['qty']:g}"
            )
    return lines
