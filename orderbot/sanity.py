"""Проверки «на глаз»: подозрительное количество и повторный заказ (B8).

Две защиты от опечаток, которые дороже всего стоят на опте:

* количество позиции больше, чем клиент когда-либо брал, в 3 раза (или выше
  жёсткого потолка) — клиенту уходит короткое уточнение, заказ при этом идёт дальше;
* тот же клиент присылает те же позиции в те же количества в течение 10 минут —
  второй заказ не создаётся, клиент видит «вы уже отправили, вот №».

Модуль не мешает разбору: он только смотрит на готовые позиции заказа.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

QTY_FACTOR = 3.0          # во сколько раз больше «обычного» считаем подозрительным
HARD_QTY_LIMIT = 300.0    # абсолютный потолок по одной позиции (кг/шт)
DUP_WINDOW_MIN = 10       # окно поиска повторного заказа


def _signature(items: list[dict]) -> dict[tuple[int, str], float]:
    """Позиции заказа без цен: (catalog_id, единица) → суммарное количество.

    Единица входит в подпись: «5 штук» и «5 кг» — разные заказы.
    """
    sign: dict[tuple[int, str], float] = {}
    for item in items or []:
        try:
            catalog_id = int(item.get("catalog_id") or 0)
        except (TypeError, ValueError):
            continue
        if not catalog_id:
            continue
        try:
            qty = float(item.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            continue
        unit = str(
            item.get("unit") or item.get("catalog_unit") or ""
        ).strip().lower()
        key = (catalog_id, unit)
        sign[key] = round(sign.get(key, 0.0) + qty, 3)
    return sign


def is_duplicate(items: list[dict], previous_items: list[dict]) -> bool:
    """Тот же набор позиций с теми же количествами."""
    current = _signature(items)
    previous = _signature(previous_items)
    return bool(current) and current == previous


def suspicious_items(
    items: list[dict], usual_max: dict[int, float]
) -> list[tuple[dict, float | None]]:
    """Позиции, где количество выглядит опечаткой: [(позиция, обычный максимум)]."""
    result: list[tuple[dict, float | None]] = []
    for item in items or []:
        try:
            qty = float(item.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        try:
            catalog_id = int(item.get("catalog_id") or 0)
        except (TypeError, ValueError):
            catalog_id = 0
        usual = usual_max.get(catalog_id) if catalog_id else None
        if qty > HARD_QTY_LIMIT:
            result.append((item, usual))
            continue
        if usual and usual > 0 and qty > usual * QTY_FACTOR:
            result.append((item, usual))
    return result
