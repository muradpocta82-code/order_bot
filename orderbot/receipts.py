"""Проверка чека по фото (G3).

Vision-модель читает сумму, валюту и дату перевода, бот сверяет сумму
с заказом. Совпало — оплата отмечается сама, нет — решает менеджер кнопкой.

Только чистые функции: разбор ответа модели и вердикт.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .money import to_money

RECEIPT_PROMPT = """На фото — чек или скриншот перевода. Верни СТРОГО один JSON без пояснений:
{"amount": 123.45, "currency": "USD", "date": "2026-11-22", "merchant": "название получателя"}

Правила:
- amount — итоговая сумма перевода числом; если не видно — null.
- currency — USD, UZS, RUB, EUR или другая; если не видно — null.
- date — дата перевода ISO (YYYY-MM-DD) или null.
- merchant — получатель или название магазина, коротко, или "".
- Верни только JSON."""

TOLERANCE_PCT = 1.0


def _amount(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value).replace(" ", "").replace(",", ".").replace("$", "").strip()
    try:
        result = to_money(text)
    except (TypeError, ValueError, ArithmeticError):
        return None
    return result if result > 0 else None


def parse_receipt(data: dict[str, Any] | None) -> dict[str, Any]:
    """Приводит ответ модели к аккуратному виду."""
    if not isinstance(data, dict):
        return {"amount": None, "currency": "", "date": "", "merchant": ""}
    currency = str(data.get("currency") or "").strip().upper()[:8]
    date = str(data.get("date") or "").strip()[:10]
    if len(date) != 10:
        date = ""
    merchant = " ".join(str(data.get("merchant") or "").split())[:60]
    return {
        "amount": _amount(data.get("amount")),
        "currency": currency,
        "date": date,
        "merchant": merchant,
    }


def verdict(
    receipt: dict[str, Any],
    order_total: float,
    tolerance_pct: float = TOLERANCE_PCT,
) -> str:
    """match — сумма сходится, mismatch — нет, unknown — сумму не распознали."""
    amount = receipt.get("amount")
    if amount is None:
        return "unknown"
    total = to_money(order_total or 0)
    if total <= 0:
        return "unknown"
    diff = abs(to_money(amount) - total)
    allowed = max(Decimal("0.01"), total * to_money(tolerance_pct) / 100)
    return "match" if diff <= allowed else "mismatch"


def short_text(receipt: dict[str, Any], order_total: float) -> str:
    """Строка для карточки менеджеру: что распознали и как это соотносится."""
    amount = receipt.get("amount")
    currency = receipt.get("currency") or ""
    parts = [
        f"чек: {amount if amount is not None else '—'} {currency}".strip(),
        f"заказ: {round(float(order_total or 0), 2)}",
    ]
    if receipt.get("date"):
        parts.append(f"дата {receipt['date']}")
    if receipt.get("merchant"):
        parts.append(receipt["merchant"])
    return " · ".join(parts)
