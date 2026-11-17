"""Минимальный async-адаптер PostgreSQL поверх pg8000.

Повторяет тот минимум интерфейса aiosqlite, который использует Database:
execute(), executescript(), commit(), close(), курсор с fetchone/fetchall.
Результаты читаются в память сразу, поэтому курсор не привязан к соединению.
"""
from __future__ import annotations

import asyncio
import re
import time
from decimal import Decimal

from .clock import from_db, to_db_params
from typing import Any
from urllib.parse import urlparse

import pg8000


_READ_ONLY_PREFIXES = ("SELECT", "SHOW", "PRAGMA", "EXPLAIN", "VALUES")


def _is_read_only(query: str) -> bool:
    head = query.lstrip().lstrip("(").upper()
    return head.startswith(_READ_ONLY_PREFIXES)


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    """numeric -> float, timestamptz -> местная строка времени."""
    for key, value in row.items():
        if isinstance(value, Decimal):
            row[key] = float(value)
        elif value.__class__.__name__ == "datetime":
            row[key] = from_db(value)
    return row


class PgCursor:
    def __init__(self, rows: list[dict[str, Any]], rowcount: int):
        self._rows = [_normalize(dict(row)) for row in rows]
        self._index = 0
        self.rowcount = rowcount

    async def fetchone(self) -> dict[str, Any] | None:
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    async def fetchall(self) -> list[dict[str, Any]]:
        rows = self._rows[self._index :]
        self._index = len(self._rows)
        return rows


class PgConnection:
    dialect = "postgres"

    def __init__(self, url: str):
        self.url = url
        self._conn = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._conn is not None:
            return
        parsed = urlparse(self.url)
        self._conn = await asyncio.to_thread(
            pg8000.connect,
            user=parsed.username,
            password=parsed.password,
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 5432,
            database=(parsed.path or "/postgres").lstrip("/") or "postgres",
            timeout=15,
        )
        self._conn.autocommit = False
        # Зависший запрос не должен вешать бота: PostgreSQL сам оборвёт его.
        try:
            cursor = self._conn.cursor()
            await asyncio.to_thread(cursor.execute, "SET statement_timeout = '10s'")
            # Брошенная транзакция держит блокировки и мешает миграциям.
            await asyncio.to_thread(
                cursor.execute, "SET idle_in_transaction_session_timeout = '60s'"
            )
            await asyncio.to_thread(self._conn.commit)
        except Exception:
            pass

    async def execute(self, sql: str, params: tuple | list = ()) -> PgCursor:
        await self.connect()
        query = sql.replace("?", "%s")
        async with self._lock:
            try:
                return await self._execute_locked(query, params)
            except Exception:
                # PostgreSQL после ошибки держит транзакцию в состоянии aborted:
                # без rollback все следующие запросы падают с 25P02, пока бота
                # не перезапустят. Откатываем сразу после любой ошибки.
                await self._rollback_after_error()
                raise

    async def _execute_locked(
        self, query: str, params: tuple | list
    ) -> "PgCursor":
        cursor = self._conn.cursor()
        await asyncio.to_thread(cursor.execute, query, to_db_params(params))
        rows: list[dict[str, Any]] = []
        if cursor.description:
            names = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                rows.append(dict(zip(names, row)))
        # Чтение не должно оставлять «idle in transaction»: иначе SELECT
        # держит AccessShareLock и блокирует ALTER TABLE/миграции.
        if _is_read_only(query):
            await asyncio.to_thread(self._conn.commit)
        return PgCursor(rows, int(cursor.rowcount or 0))

    async def ping(self) -> float:
        """Время простого запроса в миллисекундах — для /health."""
        start = time.perf_counter()
        await self.execute("SELECT 1 AS ok")
        return round((time.perf_counter() - start) * 1000, 1)

    async def _rollback_after_error(self) -> None:
        """Откат сбитой транзакции; при потере соединения — переподключение."""
        try:
            if self._conn is not None:
                await asyncio.to_thread(self._conn.rollback)
        except Exception:
            # Соединение могло умереть — следующему execute нужно новое.
            try:
                if self._conn is not None:
                    await asyncio.to_thread(self._conn.close)
            except Exception:
                pass
            self._conn = None

    async def executescript(self, script: str) -> None:
        for statement in re.split(r";\s*(?:\n|$)", script):
            statement = statement.strip()
            if not statement or statement in ("BEGIN", "COMMIT"):
                continue
            await self.execute(statement)

    async def commit(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.commit)

    async def rollback(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.rollback)

    async def close(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None
