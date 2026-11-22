"""C12: окно прибытия — «курьер выехал, будет примерно к 15:40».

Время считается от склада: если менеджер задал координаты склада (`/depot`)
и у клиента есть геопин, берём расстояние по прямой и скорость из настройки,
иначе — «обычно столько-то минут по городу» (`/travel`). Разброс ±15 минут.
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


def minutes_from_depot(
    depot: tuple[float, float] | None,
    point: tuple[float, float] | None,
    base_minutes: int,
    speed_kmh: float = 25.0,
) -> int:
    """Минуты в пути: от склада до клиента, если известны обе точки.

    Иначе — обычные минуты по городу. Плюс 10 минут на сборы у подъезда.
    """
    if depot is None or point is None:
        return max(MIN_TRAVEL, int(base_minutes))
    from .route import distance_km

    km = distance_km(depot, point)
    speed = max(5.0, float(speed_kmh or 25.0))
    return max(MIN_TRAVEL, int(round(km / speed * 60 + 10)))


def order_minutes(
    order: dict,
    depot: tuple[float, float] | None,
    base_minutes: int,
    speed_kmh: float = 25.0,
) -> int:
    """Минуты для конкретного заказа: с геопином клиента — по расстоянию."""
    from .route import parse_coords

    point = parse_coords(str(order.get("delivery_address") or ""))
    if point is None and order.get("lat") is not None and order.get("lon") is not None:
        point = (float(order["lat"]), float(order["lon"]))
    return minutes_from_depot(depot, point, base_minutes, speed_kmh)


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
