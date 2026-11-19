"""Маршрут курьера: порядок объезда и ссылка «весь маршрут» (D5).

Координаты точек лежат в адресе клиента строкой «📍 41.311081, 69.240562»
(так их записывает бот, когда повар присылает геопозицию).

Сортировка бесплатная, без внешних API: сначала по слоту доставки,
внутри слота — «ближайший сосед» по расстоянию (формула гаверсинуса).
Ссылка ведёт в Яндекс.Карты с уже построенным маршрутом по точкам.
"""
from __future__ import annotations

import math
import re
from typing import Any

COORDS_RE = re.compile(r"(-?\d{1,3}\.\d{3,})\s*,\s*(-?\d{1,3}\.\d{3,})")


def parse_coords(text: str | None) -> tuple[float, float] | None:
    """Достаёт координаты из адреса. None — если их там нет."""
    match = COORDS_RE.search(str(text or ""))
    if not match:
        return None
    try:
        lat = float(match.group(1))
        lon = float(match.group(2))
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Расстояние по прямой между двумя точками, километры."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _slot_key(order: dict[str, Any]) -> str:
    return str(order.get("delivery_time") or "99:99")


def _time_key(order: dict[str, Any]) -> str:
    """Первое время слота как ключ сортировки («09:00–12:00» → «09:00»)."""
    raw = str(order.get("delivery_time") or "")
    match = re.match(r"(\d{1,2}):(\d{2})", raw)
    if not match:
        return "99:99"
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def plan(order: list[dict[str, Any]], start: tuple[float, float] | None = None) -> list[dict[str, Any]]:
    """Раскладывает заказы по порядку объезда.

    Сначала по слоту (утро → вечер), внутри слота — ближайший сосед.
    Заказы без координат уходят в конец своего слота.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for order in order:
        groups.setdefault(_time_key(order), []).append(order)

    result: list[dict[str, Any]] = []
    for slot in sorted(groups):
        pending = list(groups[slot])
        with_coords: list[dict[str, Any]] = []
        without: list[dict[str, Any]] = []
        for order in pending:
            point = parse_coords(order.get("delivery_address") or order.get("address"))
            if point is None:
                without.append(order)
            else:
                with_coords.append({**order, "_point": point})
        current = start
        while with_coords:
            if current is None:
                nearest = min(with_coords, key=lambda item: int(item.get("id") or 0))
            else:
                nearest = min(
                    with_coords,
                    key=lambda item: distance_km(current, item["_point"]),
                )
            result.append(nearest)
            current = nearest["_point"]
            with_coords.remove(nearest)
        result.extend(without)
    return [{key: value for key, value in order.items() if key != "_point"} for order in result]


def maps_link(order: list[dict[str, Any]]) -> str:
    """Одна ссылка на маршрут по всем точкам с координатами."""
    points = []
    for item in order:
        point = parse_coords(item.get("delivery_address") or item.get("address"))
        if point is not None:
            points.append(f"{point[0]:.6f},{point[1]:.6f}")
    if len(points) < 1:
        return ""
    return "https://yandex.com/maps/?rtext=" + "~".join(points) + "&rtt=auto"


def total_distance_km(order: list[dict[str, Any]], start: tuple[float, float] | None = None) -> float:
    """Длина маршрута «по прямой» — для оценки, не для расчёта топлива."""
    total = 0.0
    current = start
    for item in order:
        point = parse_coords(item.get("delivery_address") or item.get("address"))
        if point is None:
            continue
        if current is not None:
            total += distance_km(current, point)
        current = point
    return total
