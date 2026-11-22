"""Цены и скидки по клиентам (B13).

Крупный клиент получает либо скидку процентом на всё, либо особую цену
на конкретную позицию. Правило простое: особая цена важнее скидки.

Сумма позиции пересчитывается пропорционально, поэтому фасовка, вопросы
о единицах и «как есть» работают как раньше.
"""
from __future__ import annotations


def client_price(
    base_price: float | None,
    discount_pct: float | None = 0.0,
    override: float | None = None,
) -> tuple[float, float]:
    """Цена для клиента и применённая скидка в процентах.

    Возвращает (цена, скидка %). Особая цена важнее процента.
    """
    base = round(float(base_price or 0), 2)
    if override is not None and float(override) > 0:
        price = round(float(override), 2)
        pct = round((1 - price / base) * 100, 2) if base > 0 else 0.0
        return price, pct
    pct = max(0.0, min(100.0, float(discount_pct or 0)))
    if pct <= 0 or base <= 0:
        return base, 0.0
    return round(base * (1 - pct / 100), 2), pct


def scale_sum(total: float | None, base_price: float | None, price: float | None) -> float:
    """Сумма позиции с ценой клиента: фасовка и количество не меняются."""
    total = round(float(total or 0), 2)
    base = float(base_price or 0)
    if total <= 0 or base <= 0:
        return total
    return round(total * (float(price or 0) / base), 2)


def describe(discount_pct: float, special_prices: int = 0) -> str:
    """Короткая строка для карточки клиента."""
    parts: list[str] = []
    if float(discount_pct or 0) > 0:
        parts.append(f"скидка {float(discount_pct):g}%")
    if special_prices:
        parts.append(f"особых цен: {special_prices}")
    return " · ".join(parts)
