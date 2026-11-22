"""Теневой режим промптов (E10).

Новый промпт разбирает небольшую долю боевых заказов. Результат никому не
показывается: он сравнивается с рабочим разбором и записывается в
`shadow_parses`, чтобы потом решать «менять промпт или нет» по цифрам.

Здесь только чистые функции: выборка и сравнение.
"""
from __future__ import annotations

from typing import Any


def should_sample(pct: float | None, roll: float | None) -> bool:
    """Пора ли делать теневой разбор: доля в процентах и случайное число 0…1."""
    try:
        share = float(pct or 0)
        value = float(roll if roll is not None else 1.0)
    except (TypeError, ValueError):
        return False
    if share <= 0 or share > 100:
        return False
    return value < share / 100


def _key(item: dict[str, Any]) -> tuple[int, float] | None:
    raw_id = item.get("catalog_id")
    try:
        catalog_id = int(raw_id) if raw_id else 0
    except (TypeError, ValueError):
        return None
    if not catalog_id:
        return None
    qty = item.get("final_qty")
    if qty is None:
        qty = item.get("qty")
    try:
        amount = round(float(qty), 3) if qty is not None else 0.0
    except (TypeError, ValueError):
        amount = 0.0
    return catalog_id, amount


def compare_items(
    main_items: list[dict[str, Any]],
    shadow_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Сравнение рабочего и теневого разбора.

    Совпадение — та же позиция и то же количество. Возвращает числа для
    статистики и короткий список расхождений.
    """
    main = [key for key in (_key(item) for item in main_items) if key]
    shadow = [key for key in (_key(item) for item in shadow_items) if key]
    remaining = list(shadow)
    matched = 0
    diffs: list[dict[str, Any]] = []
    for key in main:
        if key in remaining:
            remaining.remove(key)
            matched += 1
        else:
            diffs.append({"catalog_id": key[0], "qty_main": key[1], "qty_shadow": None})
    for key in remaining:
        diffs.append({"catalog_id": key[0], "qty_main": None, "qty_shadow": key[1]})
    total = max(1, len(main))
    return {
        "items_main": len(main),
        "items_shadow": len(shadow),
        "matched": matched,
        "match_pct": round(matched / total * 100, 2),
        "diffs": diffs[:10],
    }
