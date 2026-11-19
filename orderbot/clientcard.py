"""Карточка клиента: один экран обо всём, что важно менеджеру (F6).

Собирает из базы: сколько заказов и на какую сумму, как часто заказывает,
средний чек, долг и его возраст, любимые позиции, претензии, среднюю оценку,
заметку, последнюю активность и кто привёл.

Текст собирается здесь, чтобы `/client` в обработчиках оставался коротким.
"""
from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Any

from .formatting import esc, money


def _days_between(values: list[str]) -> float | None:
    """Медианный интервал между заказами в днях (по списку дат, новые сверху)."""
    days: list[int] = []
    for value in values:
        try:
            days.append(datetime.fromisoformat(str(value)[:19]).date().toordinal())
        except (TypeError, ValueError):
            continue
    if len(days) < 2:
        return None
    gaps = [abs(days[i] - days[i + 1]) for i in range(len(days) - 1)]
    gaps = [gap for gap in gaps if gap > 0]
    if not gaps:
        return None
    return float(median(gaps))


def frequency_text(dates: list[str]) -> str:
    """«каждые ~4 дня» / «заказов мало для оценки»."""
    gap = _days_between(dates)
    if gap is None:
        return "частота: мало данных"
    if gap < 1.5:
        return "почти каждый день"
    if gap < 2.5:
        return "через день"
    return f"примерно каждые {gap:.0f} дн."


def card_text(
    user: dict[str, Any],
    stats: dict[str, Any],
    debt: dict[str, Any],
    usual: list[dict[str, Any]],
    note: str = "",
    referrer: str = "",
    claims: int = 0,
    rating: float | None = None,
) -> str:
    """Текст карточки клиента в разметке Telegram."""
    venue = user.get("venue") or user.get("name") or "клиент"
    lines = [f"👤 <b>{esc(venue)}</b>"]

    status = {
        "approved": "✅ допущен",
        "pending": "⏳ ждёт допуска",
        "rejected": "⛔ отклонён",
    }.get(str(user.get("access") or ""), str(user.get("access") or "—"))
    lines.append(f"{status} · id <code>{user.get('user_id')}</code>")
    if user.get("name") and user.get("name") != venue:
        lines.append(f"Повар: {esc(user.get('name'))}")
    if user.get("phone"):
        lines.append(f"📞 {esc(user.get('phone'))}")
    if user.get("address"):
        lines.append(f"📍 {esc(user.get('address'))}")

    lines.append("")
    orders = int(stats.get("orders") or 0)
    revenue = float(stats.get("revenue") or 0)
    lines.append(f"Заказов (доставлено): <b>{orders}</b> на <b>{money(revenue)}</b>")
    if orders:
        lines.append(f"Средний чек: <b>{money(revenue / orders)}</b>")
    lines.append(f"Ритм: <b>{frequency_text(stats.get('dates') or [])}</b>")
    if stats.get("last"):
        lines.append(f"Последний заказ: {esc(str(stats['last'])[:16])}")
    if not orders:
        lines.append("<i>Заказов ещё не было.</i>")

    lines.append("")
    debt_sum = float(debt.get("sum") or 0)
    if debt_sum > 0:
        age = debt.get("age")
        lines.append(
            f"💰 Долг: <b>{money(debt_sum)}</b> ({int(debt.get('count') or 0)} заказ(ов)"
            + (f", старейший {int(age)} дн." if age is not None else "")
            + ")"
        )
    else:
        lines.append("💰 Долга нет")
    limit = user.get("credit_limit")
    days = user.get("credit_days")
    if limit or days:
        parts = []
        if limit:
            parts.append(f"лимит {money(float(limit))}")
        if days:
            parts.append(f"срок {int(days)} дн.")
        lines.append("Условия отсрочки: " + ", ".join(parts))

    if usual:
        lines.append("")
        lines.append("<b>Берёт чаще всего</b>")
        for item in usual[:3]:
            lines.append(
                f"• {esc(item.get('name') or '')} — "
                f"{float(item.get('qty') or 0):g} {esc(str(item.get('unit') or ''))}"
            )

    if claims or rating:
        lines.append("")
        if claims:
            lines.append(f"⚠️ Претензий: {claims}")
        if rating:
            lines.append(f"⭐ Средняя оценка: {rating:.1f} из 5")

    if note:
        lines.append("")
        lines.append(f"📝 {esc(note)}")

    if referrer:
        lines.append("")
        lines.append(f"Привёл: {esc(referrer)}")
    return "\n".join(lines)
