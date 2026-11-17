#!/usr/bin/env python3
"""Переносит данные из SQLite в PostgreSQL (DATABASE_URL). Идемпотентно."""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from orderbot.config import load_settings  # noqa: E402
from orderbot.db import Database  # noqa: E402

TABLES = ("settings", "aliases", "missing_items", "drafts", "orders", "order_items", "question_log")


async def main() -> int:
    settings = load_settings()
    if not settings.database_url:
        print("DATABASE_URL не задан")
        return 1
    if not Path(settings.db_path).exists():
        print("SQLite-база не найдена")
        return 1

    source = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    target = Database(settings.db_path, settings.database_url)
    await target.connect()
    await target.init()
    try:
        for table in TABLES:
            try:
                rows = source.execute(f"SELECT * FROM {table}").fetchall()
            except sqlite3.OperationalError:
                continue
            if not rows:
                print(f"{table}: 0")
                continue
            columns = rows[0].keys()
            placeholders = ",".join("?" for _ in columns)
            names = ",".join(columns)
            inserted = 0
            for row in rows:
                cursor = await target.conn.execute(
                    f"INSERT INTO {table}({names}) VALUES({placeholders}) "
                    "ON CONFLICT DO NOTHING",
                    tuple(row[column] for column in columns),
                )
                inserted += max(cursor.rowcount, 0)
            print(f"{table}: {inserted}/{len(rows)}")
        for table in ("orders", "order_items"):
            await target.conn.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE(MAX(id), 0) + 1, false) FROM {table}"
            )
        await target.conn.commit()
    finally:
        source.close()
        await target.close()
    print("MIGRATION_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
