#!/usr/bin/env python3
"""H1: демо-данные для показа бота — только в отдельную базу.

Создаёт вымышленных клиентов и заказы за 8 недель (часть оплачена, часть в долг,
есть оценки). Никогда не пишет в боевую базу: если в DATABASE_URL нет слова
«demo», скрипт отказывается работать.

Порядок:
    createdb -O order_bot order_bot_demo
    DATABASE_URL=.../order_bot_demo venv/bin/python scripts/migrate.py
    DATABASE_URL=.../order_bot_demo venv/bin/python scripts/seed_demo.py
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from orderbot.catalog import Catalog  # noqa: E402
from orderbot.config import load_settings  # noqa: E402
from orderbot.db import Database  # noqa: E402

CLIENTS = [
    (501, "Кафе «Роза»", "Азиз Каримов"),
    (502, "Столовая «Ош»", "Дильшод Рахимов"),
    (503, "Пиццерия «Форно»", "Мария Ким"),
    (504, "Кофейня «Ботаника»", "Сергей Ли"),
    (505, "Ресторан «Плов-хаус»", "Наргиза Юсупова"),
]
WEEKS = 8
SLOTS = ("09:00-12:00", "12:00-15:00", "15:00-18:00")


def guard(url: str) -> None:
    if "demo" not in str(url).lower():
        raise SystemExit(
            "Отказ: в DATABASE_URL нет слова «demo». Демо-данные пишутся только "
            "в отдельную базу — боевую трогать нельзя."
        )


async def seed(url: str, catalog_path: Path, orders_limit: int) -> None:
    catalog = Catalog.load(catalog_path)
    pool = [item for item in catalog.items if float(item.price or 0) > 0]
    rnd = random.Random(20261122)
    db = Database(Path("data/demo.db"), url)
    await db.connect()
    created = 0
    try:
        assert db.conn is not None
        now = datetime.now()
        for chef_id, venue, person in CLIENTS:
            await db.ensure_user(chef_id, name=person, username=f"demo{chef_id}")
            await db.set_user_venue(chef_id, venue)
            day = now - timedelta(days=WEEKS * 7)
            while day < now - timedelta(days=1) and created < orders_limit:
                day += timedelta(days=rnd.randint(4, 8))
                if day.weekday() == 6:
                    continue
                items = rnd.sample(pool, min(rnd.randint(2, 5), len(pool)))
                rows = []
                total = 0.0
                for item in items:
                    qty = float(rnd.choice([1, 2, 3, 5, 10, 12, 20]))
                    price = float(item.price or 0)
                    rows.append((item, qty, price, round(qty * price, 2)))
                    total += qty * price
                paid = rnd.random() > 0.25
                status = "done" if paid else ("new" if day > now - timedelta(days=4) else "done")
                created_at = (day - timedelta(hours=rnd.randint(2, 12))).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                cursor = await db.conn.execute(
                    "INSERT INTO orders(created_at, chef_id, chef_name, chef_username, "
                    "delivery_date, delivery_time, total, status, source, raw_text, questions, "
                    "payment_method, payment_status, paid_amount, rating, delivery_pin) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (
                        created_at,
                        chef_id,
                        person,
                        f"demo{chef_id}",
                        day.date().isoformat(),
                        rnd.choice(SLOTS),
                        round(total, 2),
                        status,
                        "text",
                        "демо-заказ",
                        0,
                        "cash" if paid else "credit",
                        "paid" if paid else "unpaid",
                        round(total, 2) if paid else 0,
                        rnd.choice([5, 5, 4, 4, 3, 2]) if paid else None,
                        f"{rnd.randint(1000, 9999)}",
                    ),
                )
                row = await cursor.fetchone()
                order_id = int(row["id"])
                for item, qty, price, line_total in rows:
                    await db.conn.execute(
                        "INSERT INTO order_items(order_id, catalog_id, name, qty, unit, "
                        "price, total, raw, status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            order_id,
                            item.id,
                            item.name,
                            qty,
                            item.unit,
                            price,
                            line_total,
                            item.name,
                            "ok",
                        ),
                    )
                created += 1
            print(f"  · {venue} — клиент id {chef_id}")
        await db.conn.commit()
        print(f"Готово: заказов {created}, клиентов {len(CLIENTS)}. Боевая база не тронута.")
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Демо-данные в отдельную базу")
    parser.add_argument("--catalog", default="catalog.xlsx")
    parser.add_argument("--orders", type=int, default=60)
    args = parser.parse_args()
    settings = load_settings()
    guard(settings.database_url)
    print("Пишу демо-данные в базу со словом demo в адресе")
    asyncio.run(seed(settings.database_url, Path(args.catalog), args.orders))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
