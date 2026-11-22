#!/usr/bin/env python3
"""Топ-5 самых тяжёлых запросов из pg_stat_statements (I4).

Запуск (раз в месяц или когда бот стал медленнее):
    venv/bin/python scripts/slow_queries.py
    venv/bin/python scripts/slow_queries.py --by mean --limit 10

`total` — сколько времени заняли все вызовы, `mean` — среднее на вызов.
Счётчик сбрасывается: SELECT pg_stat_statements_reset();
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from orderbot.config import load_settings  # noqa: E402
from orderbot.pg_psycopg import PsycopgConnection  # noqa: E402

ORDER = {
    "total": "total_exec_time DESC",
    "mean": "mean_exec_time DESC",
    "calls": "calls DESC",
}


async def main_async(args) -> int:
    settings = load_settings()
    if not settings.database_url:
        print("DATABASE_URL не задан — нечего смотреть")
        return 2
    conn = PsycopgConnection(settings.database_url)
    await conn.connect()
    try:
        try:
            rows = await conn.execute(
                "SELECT calls, round(total_exec_time::numeric, 1) AS total_ms, "
                "round(mean_exec_time::numeric, 2) AS mean_ms, "
                "rows AS rows_total, "
                "left(regexp_replace(query, '[[:space:]]+', ' ', 'g'), 140) AS query "
                "FROM pg_stat_statements ORDER BY " + ORDER[args.by] + " LIMIT %s",
                (int(args.limit),),
            )
        except Exception as exc:
            print(f"pg_stat_statements недоступен: {exc}")
            return 1
        rows = await rows.fetchall()
        print(f"Топ-{args.limit} по «{args.by}» за всё время наблюдения")
        print("-" * 78)
        for index, row in enumerate(rows, start=1):
            print(
                f"{index}. вызовов {int(row['calls'])}, всего {float(row['total_ms']):g} мс, "
                f"среднее {float(row['mean_ms']):g} мс"
            )
            print(f"   {row['query']}")
    finally:
        await conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Медленные запросы (I4)")
    parser.add_argument("--by", choices=tuple(ORDER), default="total")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
