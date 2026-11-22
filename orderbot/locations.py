"""Точки одного клиента (C11).

У сети несколько заведений: повар выбирает точку при заказе, курьер везёт
на адрес точки, а счёт и долг остаются на клиента целиком.

Здесь только разбор и тексты — без Telegram.
"""
from __future__ import annotations

import re
from typing import Any

SEP_RE = re.compile(r"\s*[|;]\s*|\s+[—–-]\s+")
MAX_NAME = 80
MAX_ADDRESS = 200


def parse_add(args: str) -> tuple[str, str] | None:
    """«Склад Навои | ул. Навои 12» → (название, адрес)."""
    raw = str(args or "").strip()
    if not raw:
        return None
    parts = SEP_RE.split(raw, maxsplit=1)
    name = parts[0].strip()[:MAX_NAME]
    address = parts[1].strip()[:MAX_ADDRESS] if len(parts) > 1 else ""
    if not name:
        return None
    return name, address


def pick_by_name(rows: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    """Находит точку по названию: сначала точное совпадение, потом часть."""
    wanted = str(query or "").strip().lower()
    if not wanted:
        return None
    for row in rows:
        if str(row.get("name") or "").strip().lower() == wanted:
            return row
    for row in rows:
        if wanted in str(row.get("name") or "").strip().lower():
            return row
    return None


def list_text(rows: list[dict[str, Any]], with_money: bool = False) -> str:
    """Список точек клиента для сообщения."""
    if not rows:
        return (
            "🏠 Точек пока нет. Добавить: <code>/venue_add Склад Навои | ул. Навои 12</code>\n"
            "Тогда при заказе бот спросит, куда везти."
        )
    lines = ["🏠 <b>Точки</b>"]
    for row in rows:
        line = f"• {row.get('name') or 'без названия'}"
        if row.get("address"):
            line += f" — {row['address']}"
        if with_money:
            orders = int(row.get("orders") or 0)
            revenue = float(row.get("revenue") or 0)
            line += f" · заказов {orders}, на ${revenue:.2f}"
        lines.append(line)
    lines.append("")
    lines.append("Удалить: <code>/venue_del Название</code>")
    return "\n".join(lines)


def choice_text() -> str:
    """Вопрос при заказе, когда точек несколько."""
    return "🏠 Куда везти заказ? Выберите точку — адрес подставится сам."


def too_many_text(limit: int) -> str:
    return f"Точек уже {limit}. Больше не добавляю: удалите ненужную /venue_del Название."
