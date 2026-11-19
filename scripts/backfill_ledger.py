"""Бэкфилл книги расчётов из старых полей заказов (B3, шаг 1).

Идемпотентно: если по заказу уже есть зачёт, повторно не переносим.
Запуск: venv/bin/python scripts/backfill_ledger.py [--dry-run]
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from orderbot.config import load_settings  # noqa: E402
from orderbot.db import Database  # noqa: E402


async def main() -> int:
    dry = "--dry-run" in sys.argv
    settings = load_settings()
    db = Database(settings.db_path, settings.database_url)
    await db.connect()
    assert db.conn is not None
    try:
        cursor = await db.conn.execute(
            "SELECT o.id, o.chef_id, o.total, o.paid_amount, o.payment_method, "
            "o.paid_at, o.created_at FROM orders o "
            "WHERE o.payment_status = 'paid' AND o.chef_id IS NOT NULL "
            "AND COALESCE(o.paid_amount, 0) > 0 "
            "AND NOT EXISTS (SELECT 1 FROM allocations a WHERE a.order_id = o.id) "
            "ORDER BY o.created_at, o.id"
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        print(f"Заказов без зачёта в книге: {len(rows)}")
        created = 0
        for row in rows:
            amount = round(float(row["paid_amount"] or 0), 2)
            stamp = row.get("paid_at") or row["created_at"] or datetime.now()
            if dry:
                print(f"  + заказ №{row['id']}: {amount:.2f} ({row['payment_method']})")
                continue
            cursor = await db.conn.execute(
                "INSERT INTO payments(client_id, amount, method, ts, note, source) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    int(row["chef_id"]),
                    amount,
                    row.get("payment_method") or "cash",
                    stamp,
                    f"бэкфилл из заказа №{row['id']}",
                    "backfill",
                ),
            )
            payment_id = int((await cursor.fetchone())["id"])
            await db.conn.execute(
                "INSERT INTO allocations(payment_id, order_id, amount, created_at) "
                "VALUES (%s, %s, %s, %s)",
                (payment_id, int(row["id"]), amount, stamp),
            )
            created += 1
        if not dry:
            await db.conn.commit()
        print("Готово" if not dry else "Пробный прогон")
        print(f"Создано платежей: {created}")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
