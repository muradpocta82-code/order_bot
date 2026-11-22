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

from typing import Any

EPS = 0.005  # копейки/центы: меньше — считаем закрытым


def open_amount(order: dict[str, Any]) -> float:
    """Сколько ещё не закрыто по заказу (с учётом старого paid_amount)."""
    if str(order.get("status")) == "cancelled":
        return 0.0
    total = float(order.get("total") or 0)
    paid = float(order.get("paid_amount") or 0)
    credited = float(order.get("credited") or 0)
    return max(0.0, round(total - paid - credited, 2))


def fifo_allocate(
    amount: float, orders: list[dict[str, Any]]
) -> tuple[list[tuple[int, float]], float]:
    """Раскладывает сумму по заказам в порядке возрастания даты (FIFO).

    orders — список словарей с id, total, paid_amount, status (ожидается
    порядок «старые сверху», как их отдаёт база).
    Возвращает ([(order_id, сумма)], остаток нераспределённых денег).
    Остаток > 0 значит, что клиент заплатил вперёд — это аванс.
    """
    left = round(float(amount or 0), 2)
    result: list[tuple[int, float]] = []
    if left <= 0:
        return result, 0.0
    for order in orders:
        if left <= EPS:
            break
        if str(order.get("status")) == "cancelled":
            continue
        debt = open_amount(order)
        if debt <= EPS:
            continue
        part = round(min(debt, left), 2)
        result.append((int(order["id"]), part))
        left = round(left - part, 2)
    return result, max(0.0, left)


def credit_plan(
    amount: float,
    orders: list[dict[str, Any]],
    order_id: int | None = None,
) -> tuple[list[tuple[int, float]], float]:
    """Раскладка кредит-ноты: указанный заказ первым, дальше FIFO.

    Возвращает ([(order_id, сумма)], неприменённый остаток).
    Остаток > 0 — аванс, который уйдёт в следующий счёт.
    """
    left = round(float(amount or 0), 2)
    result: list[tuple[int, float]] = []
    if left <= 0:
        return result, 0.0
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
        part = round(min(debt, left), 2)
        result.append((int(order["id"]), part))
        left = round(left - part, 2)
    return result, max(0.0, left)


def balances(
    billed: dict[int, float],
    paid: dict[int, float],
    credited: dict[int, float] | None = None,
) -> dict[int, float]:
    """Долг по клиентам: выставлено − оплачено − зачтено."""
    credited = credited or {}
    clients = set(billed) | set(paid) | set(credited)
    return {
        int(client): round(
            billed.get(client, 0.0) - paid.get(client, 0.0) - credited.get(client, 0.0), 2
        )
        for client in clients
    }


def differences(
    old: dict[int, float], new: dict[int, float], tolerance: float = 0.01
) -> dict[int, tuple[float, float]]:
    """Расхождения между старым расчётом долга и книгой: client → (old, new)."""
    result: dict[int, tuple[float, float]] = {}
    for client in set(old) | set(new):
        old_value = round(float(old.get(client, 0.0)), 2)
        new_value = round(float(new.get(client, 0.0)), 2)
        if abs(old_value - new_value) > tolerance:
            result[int(client)] = (old_value, new_value)
    return result
