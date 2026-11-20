"""C2: умный черновик по расписанию — «обычно вы заказываете по средам».

Идея: если клиент стабильно заказывает в один день недели и сегодня этот день
наступил, а заказа нет — бот сам присылает «Повторим как обычно?» с кнопкой.
Никакого ИИ: только история заказов (минимум 3 заказа в этот день недели
и не меньше трети всех заказов клиента).

Здесь чистая логика, чтобы её покрывали тесты без Telegram.
"""
from __future__ import annotations

from collections import Counter
from datetime import date

WEEKDAYS = (
    "понедельникам",
    "вторникам",
    "средам",
    "четвергам",
    "пятницам",
    "субботам",
    "воскресеньям",
)
MIN_ORDERS = 3
MIN_SHARE = 0.3


def weekday_counts(days: list[str]) -> Counter:
    """Считает заказы по дням недели из списка дат ISO."""
    counts: Counter = Counter()
    for raw in days:
        try:
            day = date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        counts[day.weekday()] += 1
    return counts


def usual_weekday(
    days: list[str],
    weekday: int,
    min_orders: int = MIN_ORDERS,
    min_share: float = MIN_SHARE,
) -> bool:
    """Заказывает ли клиент именно в этот день недели (стабильно и часто)."""
    counts = weekday_counts(days)
    total = sum(counts.values())
    hit = counts.get(int(weekday), 0)
    if total < min_orders or hit < min_orders:
        return False
    return hit / total >= min_share


def nudge_text(weekday: int, names: list[str] | None = None) -> str:
    day = WEEKDAYS[int(weekday) % 7]
    lines = [f"Обычно вы заказываете по {day}. Собрать как в прошлый раз?"]
    if names:
        shown = ", ".join(str(name) for name in names[:5])
        lines.append(f"Обычно берёте: {shown}.")
    lines.append("Соберу черновик — проверите и подтвердите.")
    return "\n".join(lines)


def should_nudge(
    days: list[str],
    today: date,
    ordered_today: bool,
    min_orders: int = MIN_ORDERS,
    min_share: float = MIN_SHARE,
) -> bool:
    """Полное условие напоминания: день недели совпал, заказа сегодня нет."""
    if ordered_today:
        return False
    return usual_weekday(days, today.weekday(), min_orders, min_share)
