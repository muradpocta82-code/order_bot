"""Сверка книги расчётов со старыми полями заказов (B3, шаг 1).

Проверяет главный инвариант шага 1: по каждому оплаченному заказу сумма
зачётов в книге равна `orders.paid_amount`, а сумма всех платежей равна
сумме оплаченных заказов. Долг по книге (выставлено − оплачено) печатается
справочно: книга считает долгом и ещё не доставленные заказы, старый
`/debt` — только помеченные «в долг».

Запуск: venv/bin/python scripts/ledger_check.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from orderbot.config import load_settings  # noqa: E402
from orderbot.db import Database  # noqa: E402
from orderbot.ledger import balances  # noqa: E402

TOLERANCE = 0.01


async def main() -> int:
    settings = load_settings()
    db = Database(settings.db_path, settings.database_url)
    await db.connect()
    assert db.conn is not None
    problems = 0
    try:
        cursor = await db.conn.execute(
            "SELECT o.id, COALESCE(o.paid_amount, 0) AS paid, "
            "COALESCE((SELECT SUM(a.amount) FROM allocations a WHERE a.order_id = o.id), 0) AS allocated "
            "FROM orders o WHERE o.payment_status = 'paid' ORDER BY o.id"
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        mismatches = [
            (int(row["id"]), float(row["paid"]), float(row["allocated"]))
            for row in rows
            if abs(float(row["paid"]) - float(row["allocated"])) > TOLERANCE
        ]
        print(f"Оплаченных заказов: {len(rows)}")
        print(f"Заказов, где книга расходится с полем paid_amount: {len(mismatches)}")
        for order_id, paid, allocated in mismatches[:10]:
            print(f"  заказ №{order_id}: paid_amount {paid:.2f}, зачётов {allocated:.2f}")
        problems += len(mismatches)

        cursor = await db.conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM payments"
        )
        payments_total = float((await cursor.fetchone())["s"] or 0)
        cursor = await db.conn.execute(
            "SELECT COALESCE(SUM(paid_amount), 0) AS s FROM orders "
            "WHERE payment_status = 'paid'"
        )
        paid_total = float((await cursor.fetchone())["s"] or 0)
        print(f"Сумма платежей в книге: {payments_total:.2f}")
        print(f"Сумма оплаченных заказов: {paid_total:.2f}")
        if abs(payments_total - paid_total) > TOLERANCE:
            print("  ❌ суммы не совпали: бэкфилл неполный (scripts/backfill_ledger.py)")
            problems += 1

        totals = await db.ledger_totals()
        book = balances(totals["billed"], totals["paid"], totals["credited"])
        old_rows = await db.debt_list()
        old_total = sum(float(row.get("s") or 0) for row in old_rows)
        print("")
        print(f"Долг по старому /debt (только «в долг»): {old_total:.2f}")
        print(f"Открыто по книге (все неоплаченные):    {sum(book.values()):.2f}")
        print("  (числа разные по смыслу: книга видит и заказы, которые ещё не доставлены)")
    finally:
        await db.close()
    if problems:
        print("\n❌ Есть расхождения — книгу нельзя считать эталоном.")
        return 1
    print("\n✅ Книга сходится со старыми полями заказов.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
