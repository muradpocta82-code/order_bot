"""Асинхронный слой PostgreSQL на psycopg 3 с пулом соединений.

Интерфейс совпадает с orderbot/pg.py: execute(), fetchone(), fetchall(),
commit(), rollback(), executescript(), ping(), close(), плюс transaction()
для db._tx().

Отличие от прежней версии: вместо одного соединения на процесс — пул.
Одиночные запросы идут в автокоммите (каждый на своём соединении), а всё,
что должно быть атомарным, выполняется внутри db._tx(): транзакция берёт
соединение из пула и привязывает его к текущей задаче через contextvars,
поэтому все вызовы внутри блока идут по одному соединению.
"""
from __future__ import annotations

import asyncio
import contextvars
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .clock import TZ_NAME, from_db, to_db, to_db_params

POOL_MIN = 1
POOL_MAX = 8

# Соединение текущей транзакции (если она открыта в этой задаче).
_tx_conn: contextvars.ContextVar[psycopg.AsyncConnection | None] = contextvars.ContextVar(
    "tx_conn", default=None
)


def _dsn(url: str) -> str:
    parsed = urlparse(url)
    return (
        f"host={parsed.hostname or '127.0.0.1'} port={parsed.port or 5432} "
        f"dbname={(parsed.path or '/postgres').lstrip('/') or 'postgres'} "
        f"user={parsed.username} password={parsed.password} "
        "connect_timeout=15"
    )


async def _configure(conn: psycopg.AsyncConnection) -> None:
    """Настройки сессии на каждом соединении пула."""
    await conn.set_autocommit(True)
    await conn.execute("SET statement_timeout = '10s'")
    await conn.execute("SET idle_in_transaction_session_timeout = '60s'")
    await conn.execute(f"SET TimeZone = '{TZ_NAME}'")


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    """Приводим значения из базы к типам, с которыми работает остальной код:
    numeric -> float (деньги), timestamptz -> местная строка времени."""
    for key, value in row.items():
        if isinstance(value, Decimal):
            row[key] = float(value)
        elif value.__class__.__name__ == "datetime":
            row[key] = from_db(value)
    return row


class PsycopgCursor:
    def __init__(self, rows: list[dict[str, Any]], rowcount: int) -> None:
        self._rows = [_normalize(dict(row)) for row in rows]
        self.rowcount = rowcount
        self._index = 0

    async def fetchone(self) -> dict[str, Any] | None:
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    async def fetchall(self) -> list[dict[str, Any]]:
        rows = self._rows[self._index :]
        self._index = len(rows)
        return rows


class _PoolTransaction:
    """Транзакция на соединении из пула.

    Соединение запоминается в contextvar, поэтому все execute() внутри блока
    попадают в одну транзакцию, а чужие задачи параллельно работают на своих
    соединениях и не видят незакоммиченных изменений.
    """

    def __init__(self, owner: "PsycopgConnection") -> None:
        self.owner = owner
        self._conn_ctx = None
        self._tx = None
        self._conn: psycopg.AsyncConnection | None = None
        self._token = None

    async def __aenter__(self) -> "_PoolTransaction":
        self._conn_ctx = self.owner.pool.connection()
        self._conn = await self._conn_ctx.__aenter__()
        await self._conn.set_autocommit(False)
        self._tx = self._conn.transaction()
        await self._tx.__aenter__()
        self._token = _tx_conn.set(self._conn)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            return await self._tx.__aexit__(exc_type, exc, tb)
        finally:
            _tx_conn.reset(self._token)
            try:
                await self._conn.set_autocommit(True)
            finally:
                await self._conn_ctx.__aexit__(None, None, None)


class PsycopgConnection:
    dialect = "postgres"

    def __init__(self, url: str) -> None:
        self.url = url
        self.pool: AsyncConnectionPool | None = None

    async def connect(self) -> None:
        if self.pool is not None and not self.pool.closed:
            return
        self.pool = AsyncConnectionPool(
            _dsn(self.url),
            min_size=POOL_MIN,
            max_size=POOL_MAX,
            open=False,
            configure=_configure,
            timeout=15,
            name="order_bot",
        )
        await self.pool.open()
        await self.pool.wait(timeout=15)

    def transaction(self) -> _PoolTransaction:
        return _PoolTransaction(self)

    async def execute(self, sql: str, params: tuple | list = ()) -> PsycopgCursor:
        await self.connect()
        assert self.pool is not None
        query = sql.replace("?", "%s")
        args = to_db_params(params)
        current = _tx_conn.get()
        if current is not None:
            # Внутри db._tx(): всё идёт по соединению транзакции.
            async with current.cursor(row_factory=dict_row) as cur:
                await cur.execute(query, args, prepare=False)
                rows = list(await cur.fetchall()) if cur.description else []
                return PsycopgCursor(rows, int(cur.rowcount or 0))
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(query, args, prepare=False)
                rows = list(await cur.fetchall()) if cur.description else []
                rowcount = int(cur.rowcount or 0)
        return PsycopgCursor(rows, rowcount)

    async def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            if statement.strip():
                await self.execute(statement)

    async def commit(self) -> None:
        """Одиночные запросы идут в автокоммите, внутри транзакции коммитит она."""
        return None

    async def rollback(self) -> None:
        """Откат делает транзакция (db._tx); вне её откатывать нечего."""
        return None

    async def ping(self) -> float:
        start = time.perf_counter()
        await self.execute("SELECT 1")
        return round((time.perf_counter() - start) * 1000, 1)

    async def close(self) -> None:
        if self.pool is not None and not self.pool.closed:
            await self.pool.close()
        self.pool = None
