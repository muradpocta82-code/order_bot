"""Деньги как Decimal: единая точка округления (I6).

Раньше суммы считались через float, и на копейках это давало расхождения
(`0.1 + 0.2 = 0.30000000000000004`). Теперь все расчёты по деньгам идут
через `Decimal` с округлением половины вверх, как в кассе.

Смысл маленький: `to_money()` на входе, `as_float()` только там, где сумму
надо отдать наружу (старый код базы и отчётов ещё живёт на float).
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

CENT = Decimal("0.01")

#: Сумма на входе: новый код передаёт Decimal, старый — по-прежнему float.
MoneyIn = Decimal | float


def to_money(value: Any) -> Decimal:
    """Приводит любое число к деньгам: 2 знака после запятой."""
    if isinstance(value, Decimal):
        return value.quantize(CENT, rounding=ROUND_HALF_UP)
    if value is None or value == "":
        return Decimal("0.00")
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def as_float(value: Any) -> float:
    """Деньги в float — только для стыка со старым кодом."""
    return float(to_money(value))


def money_text(value: Any) -> str:
    """Коротко: 1200.5 -> «1 200,50» без знака валюты."""
    number = to_money(value or 0)
    return f"{number:,.2f}".replace(",", " ")
