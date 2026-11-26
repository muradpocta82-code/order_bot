"""Книга расчётов: FIFO-зачёт платежей и балансы (B3).

Клиент в опте платит одной суммой за несколько заказов, иногда частично,
иногда с зачётом претензии. Раньше оплата жила полями заказа
(`paid_amount`, `payment_status`) — один платёж на заказ. Здесь появляется
книга: `payments` (деньги пришли), `allocations` (что этим закрыли),
`credit_notes` (зачёт по претензии) и `credit_applications` (в какой счёт
зачёт ушёл).

Шаг 1 (этот): книга наполняется и сверяется со старыми полями, но чтение
отчётов пока идёт по-старому. Логика зачёта — чистые функции, без базы.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .money import MoneyIn, to_money

ZERO = Decimal("0.00")
EPS = Decimal("0.005")  # копейки/центы: меньше — считаем закрытым


def open_amount(order: dict[str, Any]) -> Decimal:
    """Сколько ещё не закрыто по заказу (с учётом старого paid_amount)."""
    if str(order.get("status")) == "cancelled":
        return ZERO
    total = to_money(order.get("total") or 0)
    paid = to_money(order.get("paid_amount") or 0)
    credited = to_money(order.get("credited") or 0)
    left = total - paid - credited
    return left if left > 0 else ZERO


def fifo_allocate(
    amount: MoneyIn, orders: list[dict[str, Any]]
) -> tuple[list[tuple[int, Decimal]], Decimal]:
    """Раскладывает сумму по заказам в порядке возрастания даты (FIFO).

    orders — список словарей с id, total, paid_amount, status (ожидается
    порядок «старые сверху», как их отдаёт база).
    Возвращает ([(order_id, сумма)], остаток нераспределённых денег).
    Остаток > 0 значит, что клиент заплатил вперёд — это аванс.
    """
    left = to_money(amount or 0)
    result: list[tuple[int, Decimal]] = []
    if left <= 0:
        return result, ZERO
    for order in orders:
        if left <= EPS:
            break
        if str(order.get("status")) == "cancelled":
            continue
        debt = open_amount(order)
        if debt <= EPS:
            continue
        part = to_money(min(debt, left))
        result.append((int(order["id"]), part))
        left = to_money(left - part)
    return result, left if left > 0 else ZERO


def credit_plan(
    amount: MoneyIn,
    orders: list[dict[str, Any]],
    order_id: int | None = None,
) -> tuple[list[tuple[int, Decimal]], Decimal]:
    """Раскладка кредит-ноты: указанный заказ первым, дальше FIFO.

    Возвращает ([(order_id, сумма)], неприменённый остаток).
    Остаток > 0 — аванс, который уйдёт в следующий счёт.
    """
    left = to_money(amount or 0)
    result: list[tuple[int, Decimal]] = []
    if left <= 0:
        return result, ZERO
    ordered = list(orders)
    if order_id is not None:
        first = [o for o in ordered if int(o["id"]) == int(order_id)]
        rest = [o for o in ordered if int(o["id"]) != int(order_id)]
        ordered = first + rest
    for order in ordered:
        if left <= EPS:
            break
        if str(order.get("status")) == "cancelled":
            continue
        debt = open_amount(order)
        if debt <= EPS:
            continue
        part = to_money(min(debt, left))
        result.append((int(order["id"]), part))
        left = to_money(left - part)
    return result, left if left > 0 else ZERO


def balances(
    billed: dict[int, Decimal],
    paid: dict[int, Decimal],
    credited: dict[int, Decimal] | None = None,
) -> dict[int, Decimal]:
    """Долг по клиентам: выставлено − оплачено − зачтено."""
    credited = credited or {}
    clients = set(billed) | set(paid) | set(credited)
    return {
        int(client): to_money(
            to_money(billed.get(client, 0)) - to_money(paid.get(client, 0)) - to_money(credited.get(client, 0))
        )
        for client in clients
    }


def differences(
    old: dict[int, Decimal],
    new: dict[int, Decimal],
    tolerance: Decimal = Decimal("0.01"),
) -> dict[int, tuple[Decimal, Decimal]]:
    """Расхождения между старым расчётом долга и книгой: client → (old, new)."""
    result: dict[int, tuple[Decimal, Decimal]] = {}
    for client in set(old) | set(new):
        old_value = to_money(old.get(client, 0))
        new_value = to_money(new.get(client, 0))
        if abs(old_value - new_value) > tolerance:
            result[int(client)] = (old_value, new_value)
    return result
