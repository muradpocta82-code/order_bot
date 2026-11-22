"""C12: окно прибытия — «курьер выехал, будет примерно к 15:40».

Точного трекинга нет (геопина у точек пока не спрашиваем), поэтому время
считается честно как «обычно столько-то минут по городу» плюс разброс.
Настройка `courier_travel_min` меняется командой /travel без деплоя.
"""
from __future__ import annotations

from datetime import datetime, timedelta

DEFAULT_TRAVEL_MIN = 30
SPREAD_MIN = 15
MIN_TRAVEL = 5
MAX_TRAVEL = 240


def travel_minutes(raw: str | None = None) -> int:
    """Минуты в пути из настройки; мусор и крайности приводим к разумным."""
    try:
        value = int(float(str(raw).replace(",", ".")))
    except (TypeError, ValueError):
        return DEFAULT_TRAVEL_MIN
    return max(MIN_TRAVEL, min(MAX_TRAVEL, value))


def arrival(now: datetime, minutes: int) -> datetime:
    return now + timedelta(minutes=max(MIN_TRAVEL, int(minutes)))


def window_text(now: datetime, minutes: int) -> str:
    """«к 15:40 (±15 мин)»."""
    eta = arrival(now, minutes)
    return f"к {eta.strftime('%H:%M')} (±{SPREAD_MIN} мин)"


def client_text(order_id: int, now: datetime, minutes: int, slot: str = "") -> str:
    lines = [f"🚚 Заказ №{order_id} в пути. Курьер будет {window_text(now, minutes)}."]
    if slot:
        lines.append(f"Ваш слот доставки: {slot}.")
    lines.append("Если планы меняются — напишите менеджеру.")
    return "\n".join(lines)
