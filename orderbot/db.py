"""Слой хранения: aiosqlite, черновики, заказы, настройки."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiosqlite

from .migrations import apply_migrations
from .pg import PgConnection

log = logging.getLogger("orderbot.db")


async def reset_test_database(db: "Database") -> None:
    """Полностью очищает ТЕСТОВУЮ PostgreSQL-базу перед прогоном тестов.

    Разрешено только для базы, имя которой оканчивается на `_test`, чтобы
    случайно не стереть боевую.
    """
    if not db.is_postgres:
        return
    name = (urlparse(db.url).path or "/").lstrip("/")
    if not name.endswith("_test"):
        raise RuntimeError(f"reset_test_database: {name} — не тестовая база")
    assert db.conn is not None
    await db.conn.execute("DROP SCHEMA public CASCADE")
    await db.conn.execute("CREATE SCHEMA public")
    await db.conn.commit()
    # Схему строим тем же раннером, что и на проде: иначе тесты живут
    # на устаревшей схеме и ловят расхождения типов.
    import subprocess
    import sys as _sys

    runner = Path(__file__).resolve().parent.parent / "scripts" / "migrate.py"
    env = dict(os.environ)
    env["DATABASE_URL"] = db.url
    result = subprocess.run(
        [_sys.executable, str(runner)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "reset_test_database: миграции не применились: "
            + (result.stderr or result.stdout).strip()[-400:]
        )


class _ManualTx:
    """Транзакция для драйверов без своего context manager (pg8000)."""

    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None:
            await self.conn.commit()
        else:
            await self.conn.rollback()
        return False


class _LockedTx:
    """Транзакция под общим замком: на одном коннекте — одна транзакция."""

    def __init__(self, db):
        self.db = db
        self.inner = None

    async def __aenter__(self):
        await self.db._tx_lock.acquire()
        tx = getattr(self.db.conn, "transaction", None)
        self.inner = tx() if tx is not None else _ManualTx(self.db.conn)
        await self.inner.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            return await self.inner.__aexit__(exc_type, exc, tb)
        finally:
            self.db._tx_lock.release()


class Database:
    def __init__(self, path: str | Path, url: str = ""):
        self.path = Path(path)
        self.url = url
        self.is_postgres = bool(url)
        self.conn: Any = None
        self._tx_lock = asyncio.Lock()

    async def connect(self) -> None:
        if self.url:
            driver = os.getenv("DB_DRIVER", "psycopg").strip().lower()
            if driver == "psycopg":
                from .pg_psycopg import PsycopgConnection

                self.conn = PsycopgConnection(self.url)
            else:
                self.conn = PgConnection(self.url)
            await self.conn.connect()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def init(self) -> None:
        """PostgreSQL: схему строят файлы migrations/*.sql (scripts/migrate.py).

        Старый in-code раннер оставлен только для SQLite-режима.
        """
        assert self.conn is not None
        if self.is_postgres:
            return
        await apply_migrations(self.conn)

    def _tx(self):
        """Транзакция: psycopg даёт свою, для pg8000 — ручная."""
        return _LockedTx(self)

    # ------------------------------------------------------------------
    # настройки
    # ------------------------------------------------------------------
    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        assert self.conn is not None
        cursor = await self.conn.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = await cursor.fetchone()
        return row["value"] if row else default

    # ------------------------------------------------------------------
    # состояния чата (короткие «жду ответа» вместо ключей в settings)
    # ------------------------------------------------------------------
    async def get_chat_state(self, chat_id: int, name: str) -> str | None:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT value FROM chat_state WHERE chat_id = %s AND name = %s",
            (int(chat_id), str(name)),
        )
        row = await cursor.fetchone()
        return row["value"] if row else None

    async def set_chat_state(self, chat_id: int, name: str, value: str) -> None:
        """Пустое значение — сброс состояния (строка удаляется)."""
        assert self.conn is not None
        if not value:
            await self.conn.execute(
                "DELETE FROM chat_state WHERE chat_id = %s AND name = %s",
                (int(chat_id), str(name)),
            )
            await self.conn.commit()
            return
        await self.conn.execute(
            "INSERT INTO chat_state(chat_id, name, value, updated_at) VALUES(%s,%s,%s,%s) "
            "ON CONFLICT(chat_id, name) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at",
            (
                int(chat_id),
                str(name),
                str(value),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        await self.conn.commit()

    async def clear_chat_state(self, chat_id: int) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "DELETE FROM chat_state WHERE chat_id = %s", (int(chat_id),)
        )
        await self.conn.commit()

    async def set_setting(self, key: str, value: str) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(%s, %s) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------
    # алиасы (выбор пользователя: фраза -> catalog_id)
    # ------------------------------------------------------------------
    async def get_aliases(self, chef_id: int = 0) -> dict[str, int]:
        """Алиасы повара поверх общих: личный выбор важнее общего."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT phrase, catalog_id FROM aliases WHERE chef_id IN (0, %s) "
            "ORDER BY chef_id",
            (int(chef_id or 0),),
        )
        return {
            str(row["phrase"]): int(row["catalog_id"])
            for row in await cursor.fetchall()
        }

    async def save_alias(
        self, phrase: str, catalog_id: int, chef_id: int = 0
    ) -> None:
        """Запоминает выбор кандидата. Ключ должен быть уже нормализован.

        chef_id = 0 — общий алиас. Общий создаётся автоматически, когда
        одну и ту же пару фраза → позиция подтвердили три разных повара.
        """
        assert self.conn is not None
        phrase = " ".join(str(phrase).lower().replace("ё", "е").split())
        if not phrase:
            return
        chef_id = int(chef_id or 0)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with self._tx():
            await self.conn.execute(
                "INSERT INTO aliases(chef_id, phrase, catalog_id, hits, updated_at) "
                "VALUES(%s, %s, %s, 1, %s) "
                "ON CONFLICT(chef_id, phrase) DO UPDATE SET catalog_id = excluded.catalog_id, "
                "hits = aliases.hits + 1, updated_at = excluded.updated_at",
                (chef_id, phrase, int(catalog_id), now),
            )
            if chef_id != 0 and getattr(self, "alias_auto_global", False):
                await self._promote_alias(phrase, int(catalog_id), now)

    async def _promote_alias(
        self, phrase: str, catalog_id: int, now: str
    ) -> None:
        """Делает личный выбор общим, если его подтвердили три разных повара."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT COUNT(DISTINCT chef_id) AS c FROM aliases "
            "WHERE chef_id != 0 AND phrase = %s AND catalog_id = %s",
            (phrase, catalog_id),
        )
        row = await cursor.fetchone()
        if row is None or int(row["c"]) < 3:
            return
        cursor = await self.conn.execute(
            "SELECT 1 FROM aliases WHERE chef_id = 0 AND phrase = %s", (phrase,)
        )
        if await cursor.fetchone() is not None:
            # Общий уже закреплён — не даём разным группам переписывать его.
            return
        cursor = await self.conn.execute(
            "SELECT COALESCE(SUM(hits), 0) AS h FROM aliases "
            "WHERE chef_id != 0 AND phrase = %s AND catalog_id = %s",
            (phrase, catalog_id),
        )
        hits_row = await cursor.fetchone()
        await self.conn.execute(
            "INSERT INTO aliases(chef_id, phrase, catalog_id, hits, updated_at) "
            "VALUES(0, %s, %s, %s, %s)",
            (phrase, catalog_id, int(hits_row["h"]) if hits_row else 0, now),
        )

    # ------------------------------------------------------------------
    # чего нет в прайсе
    # ------------------------------------------------------------------
    async def record_missing(self, phrase: str) -> None:
        """Считает, сколько раз повара спрашивали позицию вне прайса."""
        assert self.conn is not None
        phrase = " ".join(str(phrase).lower().replace("ё", "е").split())
        if not phrase:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.conn.execute(
            "INSERT INTO missing_items(phrase, count, last_seen) VALUES(%s, 1, %s) "
            "ON CONFLICT(phrase) DO UPDATE SET count = missing_items.count + 1, "
            "last_seen = excluded.last_seen",
            (phrase, now),
        )
        await self.conn.commit()

    async def list_missing(
        self, days: int = 30, limit: int = 15
    ) -> list[dict[str, Any]]:
        assert self.conn is not None
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self.conn.execute(
            "SELECT phrase, count, last_seen FROM missing_items "
            "WHERE last_seen >= %s ORDER BY count DESC, last_seen DESC LIMIT %s",
            (cutoff, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]

    # ------------------------------------------------------------------
    # качество разбора
    # ------------------------------------------------------------------
    async def log_question(self, phrase: str, kind: str) -> None:
        """Запоминает фразу, на которой бот задал уточняющий вопрос."""
        assert self.conn is not None
        phrase = " ".join(str(phrase).lower().replace("ё", "е").split())
        if not phrase:
            return
        await self.conn.execute(
            "INSERT INTO question_log(phrase, kind, created_at) VALUES(%s, %s, %s)",
            (
                phrase,
                str(kind),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        await self.conn.commit()

    async def quality_stats(self, days: int = 30) -> dict[str, Any]:
        assert self.conn is not None
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self.conn.execute(
            "SELECT COUNT(*) AS orders, "
            "COALESCE(SUM(CASE WHEN questions = 0 THEN 1 ELSE 0 END), 0) AS clean, "
            "COALESCE(AVG(questions), 0) AS avg "
            "FROM orders WHERE created_at >= %s",
            (cutoff,),
        )
        row = await cursor.fetchone()
        cursor = await self.conn.execute(
            "SELECT phrase, COUNT(*) AS c FROM question_log "
            "WHERE created_at >= %s GROUP BY phrase "
            "ORDER BY c DESC, phrase ASC LIMIT 10",
            (cutoff,),
        )
        top = [dict(item) for item in await cursor.fetchall()]
        return {
            "orders": int(row["orders"]),
            "clean": int(row["clean"]),
            "avg": round(float(row["avg"]), 2),
            "top": top,
        }

    # ------------------------------------------------------------------
    # «как обычно»: частые позиции повара
    # ------------------------------------------------------------------
    async def usual_items(
        self, chef_id: int, limit: int = 5
    ) -> list[dict[str, Any]]:
        assert self.conn is not None
        cursor = await self.conn.execute(
            # MAX(oi.name): имя зависит только от catalog_id, но PostgreSQL
            # требует агрегат для колонки вне GROUP BY (SQLite это прощал).
            "SELECT oi.catalog_id, MAX(oi.name) AS name, oi.qty, oi.unit, COUNT(*) AS c "
            "FROM order_items oi JOIN orders o ON o.id = oi.order_id "
            "WHERE o.chef_id = %s AND o.status != 'cancelled' "
            "AND oi.catalog_id IS NOT NULL "
            "GROUP BY oi.catalog_id, oi.qty, oi.unit ORDER BY c DESC",
            (int(chef_id),),
        )
        stats: dict[int, dict[str, Any]] = {}
        for row in await cursor.fetchall():
            entry = stats.setdefault(
                int(row["catalog_id"]),
                {
                    "catalog_id": int(row["catalog_id"]),
                    "name": row["name"],
                    "total": 0,
                    "options": {},
                },
            )
            count = int(row["c"])
            entry["total"] += count
            key = (float(row["qty"] or 1), row["unit"] or "шт")
            entry["options"][key] = entry["options"].get(key, 0) + count

        result: list[dict[str, Any]] = []
        for entry in sorted(
            stats.values(), key=lambda item: (-item["total"], item["catalog_id"])
        )[:limit]:
            qty, unit = sorted(
                entry["options"].items(), key=lambda kv: (-kv[1], kv[0][0])
            )[0][0]
            result.append(
                {
                    "catalog_id": entry["catalog_id"],
                    "name": entry["name"],
                    "qty": qty,
                    "unit": unit,
                    "count": entry["total"],
                }
            )
        return result

    async def usual_for_item(
        self, chef_id: int, catalog_id: int
    ) -> dict[str, Any] | None:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT oi.qty, oi.unit, COUNT(*) AS c "
            "FROM order_items oi JOIN orders o ON o.id = oi.order_id "
            "WHERE o.chef_id = %s AND oi.catalog_id = %s AND o.status != 'cancelled' "
            "GROUP BY oi.qty, oi.unit ORDER BY c DESC, oi.qty ASC LIMIT 1",
            (int(chef_id), int(catalog_id)),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {"qty": float(row["qty"] or 1), "unit": row["unit"] or "шт"}

    # ------------------------------------------------------------------
    # черновики
    # ------------------------------------------------------------------
    async def create_draft(self, payload: dict[str, Any]) -> None:
        assert self.conn is not None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.conn.execute(
            "INSERT INTO drafts(id, chat_id, user_id, payload, status, updated_at) "
            "VALUES(%s,%s,%s,%s::jsonb,%s,%s)",
            (
                payload["id"],
                int(payload["chat_id"]),
                int(payload["user_id"]),
                json.dumps(payload, ensure_ascii=False),
                payload.get("status", "draft"),
                now,
            ),
        )
        await self.conn.commit()

    async def save_draft(self, payload: dict[str, Any]) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE drafts SET payload = %s::jsonb, status = %s, updated_at = %s WHERE id = %s",
            (
                json.dumps(payload, ensure_ascii=False),
                payload.get("status", "draft"),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                payload["id"],
            ),
        )
        await self.conn.commit()

    async def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT payload::text AS payload, status FROM drafts WHERE id = %s", (draft_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            return None
        # Статус-колонка авторитетнее JSON: claim_draft меняет только её,
        # и второй параллельный callback должен увидеть confirming.
        payload["status"] = row["status"]
        return payload

    async def claim_draft(
        self, draft_id: str, from_status: str = "draft", to_status: str = "confirming"
    ) -> bool:
        """Атомарно забирает черновик. True — только у одного обработчика."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "UPDATE drafts SET status = %s, updated_at = %s WHERE id = %s AND status = %s",
            (
                to_status,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                draft_id,
                from_status,
            ),
        )
        await self.conn.commit()
        return cursor.rowcount == 1

    async def release_draft(
        self, draft_id: str, from_status: str = "confirming", to_status: str = "draft"
    ) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE drafts SET status = %s, updated_at = %s WHERE id = %s AND status = %s",
            (
                to_status,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                draft_id,
                from_status,
            ),
        )
        await self.conn.commit()

    async def set_draft_status(self, draft_id: str, status: str) -> None:
        assert self.conn is not None
        draft = await self.get_draft(draft_id)
        if draft is not None:
            draft["status"] = status
            await self.save_draft(draft)
        else:
            await self.conn.execute(
                "UPDATE drafts SET status = %s WHERE id = %s", (status, draft_id)
            )
            await self.conn.commit()

    async def cancel_active_drafts(self, chat_id: int) -> int:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT id, payload::text AS payload FROM drafts WHERE chat_id = %s "
            "AND status = 'draft'", (chat_id,)
        )
        rows = await cursor.fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            payload["status"] = "cancelled"
            await self.save_draft(payload)
        return len(rows)

    # ------------------------------------------------------------------
    # заказы
    # ------------------------------------------------------------------
    async def create_order(
        self,
        draft: dict[str, Any],
        items: list[dict[str, Any]],
        manager_chat_id: int | None,
        draft_id: str | None = None,
    ) -> int:
        assert self.conn is not None
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total = sum(float(item.get("total") or 0) for item in items)
        async with self._tx():
            add_id = " RETURNING id" if self.is_postgres else ""
            cursor = await self.conn.execute(
                "INSERT INTO orders(created_at, chef_id, chef_name, chef_username, manager_chat_id, "
                "delivery_date, delivery_time, comment, total, status, source, raw_text, questions, "
                "delivery_address, delivery_phone, payment_method, payment_status, delivery_pin) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)" + add_id,
                (
                    created_at,
                    int(draft["user_id"]),
                    draft.get("user_name"),
                    draft.get("username"),
                    manager_chat_id,
                    draft.get("delivery_date"),
                    draft.get("delivery_time"),
                    draft.get("comment"),
                    round(total, 2),
                    "new",
                    draft.get("source"),
                    draft.get("raw_text"),
                    int(draft.get("questions") or 0),
                    draft.get("delivery_address"),
                    draft.get("delivery_phone"),
                    draft.get("payment_method") or "cash",
                    "unpaid",
                    f"{secrets.randbelow(9000) + 1000}",
                ),
            )
            if self.is_postgres:
                row = await cursor.fetchone()
                order_id = int(row["id"])
            else:
                order_id = int(cursor.lastrowid)
            for item in items:
                await self.conn.execute(
                    "INSERT INTO order_items(order_id, catalog_id, name, qty, unit, price, total, raw, status, stock_note) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        order_id,
                        item.get("catalog_id"),
                        item.get("name"),
                        item.get("qty"),
                        item.get("unit"),
                        item.get("price") or 0,
                        item.get("total") or 0,
                        item.get("raw"),
                        item.get("status"),
                        item.get("stock_note"),
                    ),
                )
            if draft_id is not None:
                claimed = await self.conn.execute(
                    "UPDATE drafts SET status = 'confirmed', updated_at = %s "
                    "WHERE id = %s AND status = 'confirming'",
                    (created_at, draft_id),
                )
                if claimed.rowcount != 1:
                    raise RuntimeError("Черновик уже обработан другим запросом")
            return order_id

    async def replace_order_items(
        self, order_id: int, items: list[dict[str, Any]], total: float
    ) -> None:
        """Правка заказа текстом: заменяет позиции и сумму одним коммитом."""
        assert self.conn is not None
        async with self._tx():
            await self.conn.execute(
                "DELETE FROM order_items WHERE order_id = %s", (int(order_id),)
            )
            for item in items:
                await self.conn.execute(
                    "INSERT INTO order_items(order_id, catalog_id, name, qty, unit, price, total, raw, status, stock_note) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        int(order_id),
                        item.get("catalog_id"),
                        item.get("name"),
                        item.get("qty"),
                        item.get("unit"),
                        item.get("price") or 0,
                        item.get("total") or 0,
                        item.get("raw"),
                        item.get("status"),
                        item.get("stock_note"),
                    ),
                )
            await self.conn.execute(
                "UPDATE orders SET total = %s WHERE id = %s",
                (round(float(total), 2), int(order_id)),
            )

    async def get_order(self, order_id: int) -> dict[str, Any] | None:
        assert self.conn is not None
        cursor = await self.conn.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        order = dict(row)
        items_cursor = await self.conn.execute(
            "SELECT * FROM order_items WHERE order_id = %s ORDER BY id", (order_id,)
        )
        order["items"] = [dict(item) for item in await items_cursor.fetchall()]
        return order

    async def list_orders_page(
        self,
        limit: int = 10,
        offset: int = 0,
        statuses: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        assert self.conn is not None
        where = ""
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("%s" for _ in statuses)
            where = f" WHERE status IN ({placeholders})"
            params.extend(statuses)
        params.extend([limit, offset])
        cursor = await self.conn.execute(
            "SELECT id, created_at, chef_name, delivery_date, delivery_time, total, status, "
            "payment_status, payment_method FROM orders"
            + where
            + " ORDER BY id DESC LIMIT %s OFFSET %s",
            params,
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def list_orders(self, limit: int = 10) -> list[dict[str, Any]]:
        return await self.list_orders_page(limit=limit, offset=0)

    async def count_orders(self, statuses: tuple[str, ...] | None = None) -> int:
        assert self.conn is not None
        query = "SELECT COUNT(*) AS c FROM orders"
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("%s" for _ in statuses)
            query += f" WHERE status IN ({placeholders})"
            params.extend(statuses)
        cursor = await self.conn.execute(query, params)
        row = await cursor.fetchone()
        return int(row["c"]) if row else 0

    async def count_orders_since(self, chef_id: int, minutes: int = 60) -> int:
        assert self.conn is not None
        cutoff = (datetime.now() - timedelta(minutes=minutes)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cursor = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE chef_id = %s AND created_at >= %s",
            (int(chef_id), cutoff),
        )
        row = await cursor.fetchone()
        return int(row["c"]) if row else 0

    async def sum_orders(self, statuses: tuple[str, ...] | None = None) -> float:
        assert self.conn is not None
        query = "SELECT COALESCE(SUM(total), 0) AS s FROM orders"
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("%s" for _ in statuses)
            query += f" WHERE status IN ({placeholders})"
            params.extend(statuses)
        cursor = await self.conn.execute(query, params)
        row = await cursor.fetchone()
        return float(row["s"]) if row else 0.0

    async def last_order_for_user(self, user_id: int) -> dict[str, Any] | None:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT id FROM orders WHERE chef_id = %s ORDER BY id DESC LIMIT 1", (user_id,)
        )
        row = await cursor.fetchone()
        return await self.get_order(int(row["id"])) if row else None

    async def log_event(
        self,
        order_id: int,
        kind: str,
        payload: dict[str, Any] | None = None,
        actor_id: int | None = None,
    ) -> None:
        """Хронология заказа: что, когда и кем изменено.

        Ошибка записи события не должна ломать заказ — поэтому только в журнал.
        """
        if self.conn is None:
            return
        try:
            await self.conn.execute(
                "INSERT INTO order_events(order_id, kind, actor_id, payload) "
                "VALUES (%s, %s, %s, %s::jsonb)",
                (
                    int(order_id),
                    kind,
                    actor_id,
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )
            await self.conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("Событие заказа №%s не записано: %s", order_id, exc)

    async def order_events(self, order_id: int, limit: int = 50) -> list[dict[str, Any]]:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT created_at, kind, actor_id, payload FROM order_events "
            "WHERE order_id = %s ORDER BY id DESC LIMIT %s",
            (int(order_id), int(limit)),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def update_order_status(self, order_id: int, status: str) -> bool:
        """Меняет статус, только если он реально другой. False — уже был этот."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "UPDATE orders SET status = %s WHERE id = %s AND status != %s",
            (status, order_id, status),
        )
        await self.conn.commit()
        changed = cursor.rowcount == 1
        if changed:
            await self.log_event(order_id, "status", {"to": status})
        return changed

    _ORDER_FIELDS = {
        "packed_at",
        "delivering_at",
        "delivered_at",
        "paid_at",
        "payment_method",
        "payment_status",
        "paid_amount",
        "courier_chat_id",
        "courier_notified_at",
    }

    async def transition_order(
        self,
        order_id: int,
        from_statuses: tuple[str, ...],
        to_status: str,
        require_paid: bool = False,
        **fields: Any,
    ) -> bool:
        """Атомарно переводит заказ в новый статус.

        WHERE по текущему статусу защищает от двойного нажатия и от гонки
        упаковщика/доставщика: второй запрос просто вернёт False.
        """
        assert self.conn is not None
        if not from_statuses:
            raise ValueError("from_statuses не может быть пустым")
        sets = ["status = %s"]
        params: list[Any] = [to_status]
        for name, value in fields.items():
            if name not in self._ORDER_FIELDS:
                raise ValueError(f"Недопустимое поле заказа: {name}")
            sets.append(f"{name} = %s")
            params.append(value)
        placeholders = ",".join("%s" for _ in from_statuses)
        where = f"id = %s AND status IN ({placeholders})"
        params.extend([int(order_id), *from_statuses])
        if require_paid:
            where += " AND payment_status IN ('paid', 'credit')"
        cursor = await self.conn.execute(
            "UPDATE orders SET " + ", ".join(sets) + " WHERE " + where, params
        )
        await self.conn.commit()
        changed = int(cursor.rowcount or 0) == 1
        if changed:
            await self.log_event(
                order_id,
                "status",
                {"from": list(from_statuses), "to": to_status, **{k: str(v) for k, v in fields.items()}},
            )
        return changed

    async def mark_order_ready(
        self, order_id: int, courier_chat_id: int | None
    ) -> bool:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return await self.transition_order(
            order_id,
            ("new", "accepted"),
            "ready",
            packed_at=now,
            courier_chat_id=courier_chat_id,
            courier_notified_at=None,
        )

    async def mark_out_for_delivery(self, order_id: int) -> bool:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return await self.transition_order(
            order_id, ("ready",), "delivering", delivering_at=now
        )

    async def mark_order_credit(self, order_id: int, amount: float) -> bool:
        """Заказ отдан в долг: закрывается, но деньги ещё не пришли."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "UPDATE orders SET payment_status = 'credit', payment_method = 'credit', "
            "paid_amount = 0 WHERE id = %s AND payment_status = 'unpaid' "
            "AND status IN ('new', 'accepted', 'ready', 'delivering')",
            (int(order_id),),
        )
        await self.conn.commit()
        changed = int(cursor.rowcount or 0) == 1
        if changed:
            await self.log_event(order_id, "payment", {"method": "credit", "amount": 0})
        return changed

    async def client_debt(self, chef_id: int) -> tuple[float, int, int | None]:
        """(сумма долга, число заказов, возраст самого старого долга в днях)."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT COALESCE(SUM(total), 0) AS s, COUNT(*) AS n, "
            "MIN(created_at) AS oldest FROM orders "
            "WHERE chef_id = %s AND payment_status = 'credit'",
            (int(chef_id),),
        )
        row = await cursor.fetchone()
        if not row or not int(row["n"] or 0):
            return 0.0, 0, None
        oldest = row.get("oldest")
        age: int | None = None
        if oldest is not None:
            try:
                moment = oldest
                if isinstance(moment, str):
                    moment = datetime.fromisoformat(moment[:19])
                if getattr(moment, "tzinfo", None) is not None:
                    moment = moment.replace(tzinfo=None)
                age = (datetime.now() - moment).days
            except (TypeError, ValueError):
                age = None
        return float(row["s"] or 0), int(row["n"] or 0), age

    async def set_credit_terms(
        self, chef_id: int, limit: float | None, days: int | None
    ) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE users SET credit_limit = %s, credit_days = %s, updated_at = %s "
            "WHERE user_id = %s",
            (limit, days, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(chef_id)),
        )
        await self.conn.commit()

    async def credit_orders(self) -> list[dict[str, Any]]:
        """Заказы в долг — для отчёта по возрасту задолженности."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT id, chef_id, chef_name, total, created_at FROM orders "
            "WHERE payment_status = 'credit' ORDER BY created_at"
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def debt_list(self) -> list[dict[str, Any]]:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT chef_id, chef_name, COUNT(*) AS n, "
            "COALESCE(SUM(total), 0) AS s, MIN(created_at) AS since "
            "FROM orders WHERE payment_status = 'credit' "
            "GROUP BY chef_id, chef_name ORDER BY s DESC",
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def settle_debt(self, chef_id: int) -> tuple[int, float]:
        assert self.conn is not None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS s FROM orders "
            "WHERE chef_id = %s AND payment_status = 'credit'",
            (int(chef_id),),
        )
        row = await cursor.fetchone()
        count = int(row["n"] or 0)
        amount = float(row["s"] or 0)
        if not count:
            return 0, 0.0
        ids = await self.conn.execute(
            "SELECT id FROM orders WHERE chef_id = %s AND payment_status = 'credit'",
            (int(chef_id),),
        )
        order_ids = [int(row["id"]) for row in await ids.fetchall()]
        await self.conn.execute(
            "UPDATE orders SET payment_status = 'paid', payment_method = 'cash', "
            "paid_amount = total, paid_at = %s "
            "WHERE chef_id = %s AND payment_status = 'credit'",
            (now, int(chef_id)),
        )
        await self.conn.commit()
        for order_id in order_ids:
            await self.log_event(
                order_id, "payment", {"method": "cash", "settled_debt": True}
            )
        return count, amount

    async def sold_items(self, days: int = 30) -> list[dict[str, Any]]:
        """Проданные позиции за период — для расчёта маржи."""
        assert self.conn is not None
        since = (datetime.now() - timedelta(days=int(days))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cursor = await self.conn.execute(
            "SELECT i.catalog_id, i.name, i.qty, i.price FROM order_items i "
            "JOIN orders o ON o.id = i.order_id "
            "WHERE o.status = 'done' AND o.created_at >= %s",
            (since,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def analytics(self, days: int = 30) -> dict[str, Any]:
        """Сводка для владельца: выручка, чек, клиенты, топ позиций."""
        assert self.conn is not None
        since = (datetime.now() - timedelta(days=int(days))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cursor = await self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS s FROM orders "
            "WHERE status = 'done' AND created_at >= %s",
            (since,),
        )
        row = await cursor.fetchone()
        count = int(row["n"] or 0)
        revenue = float(row["s"] or 0)
        cursor = await self.conn.execute(
            "SELECT chef_id, chef_name, COUNT(*) AS n, COALESCE(SUM(total), 0) AS s "
            "FROM orders WHERE status = 'done' AND created_at >= %s "
            "GROUP BY chef_id, chef_name ORDER BY s DESC LIMIT 5",
            (since,),
        )
        clients = [dict(r) for r in await cursor.fetchall()]
        cursor = await self.conn.execute(
            "SELECT i.name AS name, COALESCE(SUM(i.qty), 0) AS q "
            "FROM order_items i JOIN orders o ON o.id = i.order_id "
            "WHERE o.status = 'done' AND o.created_at >= %s "
            "GROUP BY i.name ORDER BY q DESC LIMIT 5",
            (since,),
        )
        items = [dict(r) for r in await cursor.fetchall()]
        return {
            "days": int(days),
            "orders": count,
            "revenue": round(revenue, 2),
            "avg": round(revenue / count, 2) if count else 0.0,
            "clients": clients,
            "items": items,
        }

    async def active_client_ids(self, days: int = 60) -> list[int]:
        """Клиенты, которые заказывали за последние N дней."""
        assert self.conn is not None
        since = (datetime.now() - timedelta(days=int(days))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cursor = await self.conn.execute(
            "SELECT DISTINCT chef_id FROM orders WHERE created_at >= %s "
            "AND status = 'done' AND chef_id IS NOT NULL",
            (since,),
        )
        return [int(row["chef_id"]) for row in await cursor.fetchall()]

    async def client_order_dates(
        self, chef_id: int, limit: int = 30
    ) -> list[str]:
        """Даты заказов клиента, новые сверху."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT created_at FROM orders WHERE chef_id = %s "
            "AND status = 'done' ORDER BY created_at DESC LIMIT %s",
            (int(chef_id), int(limit)),
        )
        return [str(row["created_at"]) for row in await cursor.fetchall()]

    async def churn_candidates(self, days: int = 10) -> list[dict[str, Any]]:
        """Клиенты, которые перестали заказывать."""
        assert self.conn is not None
        threshold = (datetime.now() - timedelta(days=int(days))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cursor = await self.conn.execute(
            "SELECT chef_id, chef_name, COUNT(*) AS n, MAX(created_at) AS last "
            "FROM orders WHERE status = 'done' GROUP BY chef_id, chef_name "
            "HAVING COUNT(*) >= 2 AND MAX(created_at) <= %s ORDER BY last DESC",
            (threshold,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def add_claim(self, order_id: int, user_id: int, text: str) -> int:
        assert self.conn is not None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        add_id = " RETURNING id" if self.is_postgres else ""
        cursor = await self.conn.execute(
            "INSERT INTO claims(order_id, user_id, text, status, created_at) "
            "VALUES (%s,%s,%s,%s,%s)" + add_id,
            (int(order_id), int(user_id), text[:1000], "open", now),
        )
        if self.is_postgres:
            row = await cursor.fetchone()
            claim_id = int(row["id"])
        else:
            claim_id = int(cursor.lastrowid)
        await self.conn.commit()
        return claim_id

    async def open_claims(self, limit: int = 20) -> list[dict[str, Any]]:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT * FROM claims WHERE status = 'open' ORDER BY id DESC LIMIT %s",
            (int(limit),),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def close_claim(self, claim_id: int) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE claims SET status = 'closed' WHERE id = %s", (int(claim_id),)
        )
        await self.conn.commit()

    async def set_order_photo(self, order_id: int, file_id: str) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE orders SET photo_file_id = %s WHERE id = %s",
            (file_id, int(order_id)),
        )
        await self.conn.commit()

    async def order_by_card(self, message_id: int) -> dict[str, Any] | None:
        """Ищет заказ по id карточки (ответ на карточку)."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT * FROM orders WHERE chef_card_message_id = %s "
            "OR manager_card_message_id = %s OR courier_card_message_id = %s "
            "ORDER BY id DESC LIMIT 1",
            (int(message_id), int(message_id), int(message_id)),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def add_staff(self, user_id: int, role: str, name: str = "") -> None:
        assert self.conn is not None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.conn.execute(
            "INSERT INTO staff(user_id, role, name, active, created_at) "
            "VALUES (%s,%s,%s,TRUE,%s) ON CONFLICT(user_id) DO UPDATE SET "
            "role = excluded.role, name = excluded.name, active = TRUE",
            (int(user_id), role, name, now),
        )
        await self.conn.commit()

    async def remove_staff(self, user_id: int) -> bool:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "UPDATE staff SET active = FALSE WHERE user_id = %s", (int(user_id),)
        )
        await self.conn.commit()
        return int(cursor.rowcount or 0) == 1

    async def list_staff(self, role: str | None = None) -> list[dict[str, Any]]:
        assert self.conn is not None
        sql = "SELECT * FROM staff WHERE active"
        params: list[Any] = []
        if role:
            sql += " AND role = %s"
            params.append(role)
        sql += " ORDER BY role, user_id"
        cursor = await self.conn.execute(sql, params)
        return [dict(row) for row in await cursor.fetchall()]

    async def cash_report(self, day: str) -> dict[str, Any]:
        """Наличные за день: ожидалось, получено, расхождения."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT id, chef_name, total, paid_amount FROM orders "
            "WHERE payment_method = 'cash' AND paid_at::date = %s::date "
            "AND status IN ('delivering', 'done') ORDER BY id",
            (day,),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        expected = sum(float(r.get("total") or 0) for r in rows)
        received = sum(float(r.get("paid_amount") or 0) for r in rows)
        diff = [
            r
            for r in rows
            if abs(float(r.get("paid_amount") or 0) - float(r.get("total") or 0)) > 0.01
        ]
        return {
            "orders": rows,
            "expected": round(expected, 2),
            "received": round(received, 2),
            "diff": diff,
        }

    async def set_order_rating(self, order_id: int, rating: int) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE orders SET rating = %s WHERE id = %s",
            (int(rating), int(order_id)),
        )
        await self.conn.commit()

    async def update_order_total(self, order_id: int, total: float) -> float | None:
        """Меняет итог заказа (довес на сборке).

        Возвращает прежнюю сумму или None, если заказ уже закрыт.
        """
        assert self.conn is not None
        closed = ("done", "cancelled", "failed")
        async with self._tx():
            cursor = await self.conn.execute(
                "SELECT total, status FROM orders WHERE id = %s", (int(order_id),)
            )
            row = await cursor.fetchone()
            if row is None or str(row["status"]) in closed:
                return None
            old_total = float(row["total"] or 0)
            await self.conn.execute(
                "UPDATE orders SET total = %s WHERE id = %s "
                "AND status NOT IN ('done', 'cancelled', 'failed')",
                (round(float(total), 2), int(order_id)),
            )
        return old_total

    async def claim_escalation(self, order_id: int) -> bool:
        """Атомарно отмечает заказ как «упаковщику напомнили». True — только один раз."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "UPDATE orders SET escalated_at = %s WHERE id = %s AND escalated_at IS NULL",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(order_id)),
        )
        await self.conn.commit()
        return int(cursor.rowcount or 0) == 1

    async def claim_once(self, key: str, value: str = "1") -> bool:
        """Атомарно ставит флаг в settings: True — флаг достался этому вызову."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(%s, %s) "
            "ON CONFLICT(key) DO NOTHING",
            (key, value),
        )
        await self.conn.commit()
        return int(cursor.rowcount or 0) == 1

    async def stale_new_orders(self, minutes: int = 15) -> list[dict[str, Any]]:
        """Заказы, которые так и не приняли в работу."""
        assert self.conn is not None
        since = (datetime.now() - timedelta(minutes=int(minutes))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cursor = await self.conn.execute(
            "SELECT * FROM orders WHERE status = 'new' AND created_at <= %s "
            "ORDER BY id",
            (since,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def mark_order_paid(
        self, order_id: int, method: str, amount: float
    ) -> bool:
        assert self.conn is not None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self.conn.execute(
            "UPDATE orders SET payment_status = 'paid', payment_method = %s, "
            "paid_amount = %s, paid_at = %s "
            "WHERE id = %s AND status IN ('ready', 'delivering') "
            "AND payment_status != 'paid'",
            (method, round(float(amount), 2), now, int(order_id)),
        )
        await self.conn.commit()
        changed = int(cursor.rowcount or 0) == 1
        if changed:
            await self.log_event(
                order_id, "payment", {"method": method, "amount": round(float(amount), 2)}
            )
        return changed

    async def create_ton_payment(
        self, order_id: int, comment: str, amount_nano: int, minutes: int = 15
    ) -> int:
        """Создаёт счёт USDT; прошлые незакрытые счета заказа закрывает."""
        assert self.conn is not None
        now = datetime.now()
        created = now.strftime("%Y-%m-%d %H:%M:%S")
        expires = (now + timedelta(minutes=int(minutes))).strftime("%Y-%m-%d %H:%M:%S")
        await self.conn.execute(
            "UPDATE ton_payments SET status = 'expired' "
            "WHERE order_id = %s AND status = 'pending'",
            (int(order_id),),
        )
        add_id = " RETURNING id" if self.is_postgres else ""
        cursor = await self.conn.execute(
            "INSERT INTO ton_payments(order_id, comment, amount_usdt, amount_nano, "
            "status, created_at, expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s)" + add_id,
            (
                int(order_id),
                comment,
                f"{int(amount_nano) / 1_000_000:.2f}",
                int(amount_nano),
                "pending",
                created,
                expires,
            ),
        )
        if self.is_postgres:
            row = await cursor.fetchone()
            payment_id = int(row["id"])
        else:
            payment_id = int(cursor.lastrowid)
        await self.conn.commit()
        return payment_id

    async def list_pending_ton_payments(self) -> list[dict[str, Any]]:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT * FROM ton_payments WHERE status = 'pending' ORDER BY id"
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def expire_ton_payments(self) -> list[dict[str, Any]]:
        """Помечает истёкшие счета и возвращает их список."""
        assert self.conn is not None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self.conn.execute(
            "SELECT * FROM ton_payments WHERE status = 'pending' AND expires_at <= %s",
            (now,),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        if rows:
            await self.conn.execute(
                "UPDATE ton_payments SET status = 'expired' "
                "WHERE status = 'pending' AND expires_at <= %s",
                (now,),
            )
            await self.conn.commit()
        return rows

    async def list_open_ton_payments(self, hours: int = 24) -> list[dict[str, Any]]:
        """Счета, которые ещё можно закрыть: pending и недавно истёкшие."""
        assert self.conn is not None
        since = (datetime.now() - timedelta(hours=int(hours))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cursor = await self.conn.execute(
            "SELECT * FROM ton_payments WHERE status IN ('pending', 'expired') "
            "AND created_at >= %s ORDER BY id",
            (since,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def credit_ton_payment(
        self, order_id: int, tx_hash: str, amount_nano: int, comment: str = ""
    ) -> int:
        """Ручной зачёт платежа админом (без ожидания TonAPI)."""
        assert self.conn is not None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        add_id = " RETURNING id" if self.is_postgres else ""
        cursor = await self.conn.execute(
            "INSERT INTO ton_payments(order_id, comment, amount_usdt, amount_nano, "
            "status, tx_hash, created_at, expires_at, paid_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)" + add_id,
            (
                int(order_id),
                comment or f"order_{int(order_id)}",
                f"{int(amount_nano) / 1_000_000:.2f}",
                int(amount_nano),
                "paid",
                tx_hash,
                now,
                now,
                now,
            ),
        )
        if self.is_postgres:
            row = await cursor.fetchone()
            payment_id = int(row["id"])
        else:
            payment_id = int(cursor.lastrowid)
        await self.conn.commit()
        return payment_id

    async def ton_tx_used(self, tx_hash: str) -> bool:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT 1 FROM ton_payments WHERE tx_hash = %s LIMIT 1", (tx_hash,)
        )
        return await cursor.fetchone() is not None

    async def apply_ton_payment(
        self, payment_id: int, tx_hash: str, order_id: int, amount: float
    ) -> bool:
        """Атомарно: счёт закрыт и заказ оплачен (или ни то, ни другое)."""
        assert self.conn is not None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with self._tx():
            paid = await self.conn.execute(
                "UPDATE ton_payments SET status = 'paid', tx_hash = %s, paid_at = %s "
                "WHERE id = %s AND status IN ('pending', 'expired') "
                "AND tx_hash IS NULL",
                (tx_hash, now, int(payment_id)),
            )
            if int(paid.rowcount or 0) != 1:
                raise RuntimeError("payment already applied")
            order = await self.conn.execute(
                "UPDATE orders SET payment_status = 'paid', payment_method = 'usdt', "
                "paid_amount = %s, paid_at = %s WHERE id = %s "
                "AND payment_status != 'paid' AND status IN ('ready', 'delivering')",
                (round(float(amount), 2), now, int(order_id)),
            )
            if int(order.rowcount or 0) != 1:
                raise RuntimeError("order not payable")
        await self.log_event(
            order_id, "payment", {"method": "usdt", "amount": round(float(amount), 2), "tx": tx_hash}
        )
        return True

    async def mark_ton_payment_paid(
        self, payment_id: int, tx_hash: str
    ) -> bool:
        assert self.conn is not None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self.conn.execute(
            "UPDATE ton_payments SET status = 'paid', tx_hash = %s, paid_at = %s "
            "WHERE id = %s AND status IN ('pending', 'expired') AND tx_hash IS NULL",
            (tx_hash, now, int(payment_id)),
        )
        await self.conn.commit()
        return int(cursor.rowcount or 0) == 1

    async def mark_order_delivered(self, order_id: int) -> bool:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return await self.transition_order(
            order_id,
            ("ready", "delivering"),
            "done",
            require_paid=True,
            delivered_at=now,
        )

    async def mark_order_failed(self, order_id: int) -> bool:
        return await self.transition_order(
            order_id, ("ready", "delivering"), "failed"
        )

    async def mark_courier_notified(self, order_id: int) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE orders SET courier_notified_at = %s "
            "WHERE id = %s AND courier_notified_at IS NULL",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(order_id)),
        )
        await self.conn.commit()

    async def list_unnotified_delivery_orders(self, limit: int = 20) -> list[int]:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT id FROM orders WHERE courier_notified_at IS NULL "
            "AND status = 'ready' AND courier_chat_id IS NOT NULL "
            "ORDER BY id LIMIT %s",
            (limit,),
        )
        return [int(row["id"]) for row in await cursor.fetchall()]

    async def update_order_delivery(
        self, order_id: int, delivery_date: str, delivery_time: str | None = None
    ) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE orders SET delivery_date = %s, delivery_time = %s WHERE id = %s",
            (delivery_date, delivery_time, int(order_id)),
        )
        await self.conn.commit()

    async def mark_order_notified(self, order_id: int) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE orders SET notified_at = %s WHERE id = %s AND notified_at IS NULL",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), order_id),
        )
        await self.conn.commit()

    async def set_order_cards(
        self,
        order_id: int,
        chef_card: int | None = None,
        manager_card: int | None = None,
        courier_card: int | None = None,
    ) -> None:
        """Запоминает id карточек заказа, чтобы потом убрать кнопки/плашки."""
        assert self.conn is not None
        sets: list[str] = []
        params: list[Any] = []
        if chef_card is not None:
            sets.append("chef_card_message_id = %s")
            params.append(int(chef_card))
        if manager_card is not None:
            sets.append("manager_card_message_id = %s")
            params.append(int(manager_card))
        if courier_card is not None:
            sets.append("courier_card_message_id = %s")
            params.append(int(courier_card))
        if not sets:
            return
        params.append(int(order_id))
        await self.conn.execute(
            "UPDATE orders SET " + ", ".join(sets) + " WHERE id = %s", params
        )
        await self.conn.commit()

    async def list_unnotified_orders(self, limit: int = 20) -> list[int]:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT id FROM orders "
            "WHERE notified_at IS NULL AND status IN ('new', 'accepted') "
            "ORDER BY id LIMIT %s",
            (limit,),
        )
        return [int(row["id"]) for row in await cursor.fetchall()]

    async def old_unnotified_orders(self, minutes: int = 15) -> list[int]:
        assert self.conn is not None
        cutoff = (datetime.now() - timedelta(minutes=minutes)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cursor = await self.conn.execute(
            "SELECT id FROM orders WHERE notified_at IS NULL "
            "AND status IN ('new', 'accepted') AND created_at <= %s ORDER BY id",
            (cutoff,),
        )
        return [int(row["id"]) for row in await cursor.fetchall()]

    async def purge_closed_drafts(self, days: int = 7) -> int:
        """Удаляет закрытые черновики старше N дней."""
        assert self.conn is not None
        cutoff = (datetime.now() - timedelta(days=days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cursor = await self.conn.execute(
            "DELETE FROM drafts WHERE status != 'draft' AND updated_at < %s",
            (cutoff,),
        )
        await self.conn.commit()
        return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------
    # пользователи: заведение и допуск
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # заказы из мини-приложения каталога
    # ------------------------------------------------------------------
    async def add_webapp_order(self, user_id: int, chat_id: int, text: str) -> int:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "INSERT INTO webapp_orders(user_id, chat_id, text) VALUES(%s, %s, %s) "
            "RETURNING id",
            (int(user_id), int(chat_id), str(text)),
        )
        row = await cursor.fetchone()
        await self.conn.commit()
        return int(row["id"]) if row else 0

    async def take_webapp_orders(self, limit: int = 5) -> list[dict[str, Any]]:
        """Неразобранные заказы из каталога, по одному на пользователя."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT DISTINCT ON (user_id) id, user_id, chat_id, text, created_at "
            "FROM webapp_orders WHERE processed_at IS NULL "
            "ORDER BY user_id, id LIMIT %s",
            (int(limit),),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def close_webapp_order(self, order_id: int, error: str | None = None) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE webapp_orders SET processed_at = %s, error = %s WHERE id = %s",
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                (error or "")[:300] or None,
                int(order_id),
            ),
        )
        await self.conn.commit()

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT * FROM users WHERE user_id = %s", (int(user_id),)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def ensure_user(
        self, user_id: int, name: str | None = None, username: str | None = None
    ) -> dict[str, Any]:
        """Создаёт профиль. У кого уже есть заказы — сразу допущен."""
        assert self.conn is not None
        user = await self.get_user(user_id)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if user is not None:
            if (name and name != user.get("name")) or (
                username and username != user.get("username")
            ):
                await self.conn.execute(
                    "UPDATE users SET name = %s, username = %s, updated_at = %s "
                    "WHERE user_id = %s",
                    (name or user.get("name"), username or user.get("username"), now, int(user_id)),
                )
                await self.conn.commit()
                user = await self.get_user(user_id) or user
            return user
        access = "approved" if await self.has_orders(user_id) else "pending"
        await self.conn.execute(
            "INSERT INTO users(user_id, name, username, venue, access, created_at, updated_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (int(user_id), name, username, None, access, now, now),
        )
        await self.conn.commit()
        created = await self.get_user(user_id)
        assert created is not None
        return created

    async def set_user_venue(self, user_id: int, venue: str) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE users SET venue = %s, updated_at = %s WHERE user_id = %s",
            (venue.strip()[:120], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(user_id)),
        )
        await self.conn.commit()

    async def set_user_contact(
        self, user_id: int, phone: str, address: str
    ) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE users SET phone = %s, address = %s, updated_at = %s WHERE user_id = %s",
            (
                phone.strip()[:40],
                address.strip()[:300],
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                int(user_id),
            ),
        )
        await self.conn.commit()

    async def set_user_phone(self, user_id: int, phone: str) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE users SET phone = %s, updated_at = %s WHERE user_id = %s",
            (
                phone.strip()[:40],
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                int(user_id),
            ),
        )
        await self.conn.commit()

    async def set_user_address(self, user_id: int, address: str) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE users SET address = %s, updated_at = %s WHERE user_id = %s",
            (
                address.strip()[:300],
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                int(user_id),
            ),
        )
        await self.conn.commit()

    async def set_user_access(self, user_id: int, access: str) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE users SET access = %s, updated_at = %s WHERE user_id = %s",
            (access, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(user_id)),
        )
        await self.conn.commit()

    async def list_pending_users(self, limit: int = 20) -> list[dict[str, Any]]:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT * FROM users WHERE access = 'pending' ORDER BY created_at LIMIT %s",
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def has_orders(self, user_id: int) -> bool:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT 1 FROM orders WHERE chef_id = %s LIMIT 1", (int(user_id),)
        )
        return await cursor.fetchone() is not None

    async def venue_map(self, user_ids: list[int]) -> dict[int, str]:
        if not user_ids:
            return {}
        assert self.conn is not None
        placeholders = ",".join("%s" for _ in user_ids)
        cursor = await self.conn.execute(
            f"SELECT user_id, venue FROM users WHERE user_id IN ({placeholders})",
            [int(uid) for uid in user_ids],
        )
        return {
            int(row["user_id"]): row["venue"]
            for row in await cursor.fetchall()
            if row["venue"]
        }

    # ------------------------------------------------------------------
    # остатки
    # ------------------------------------------------------------------
    async def set_item_available(
        self, catalog_id: int, available: bool, stock_left: float | None = None
    ) -> None:
        assert self.conn is not None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.conn.execute(
            "INSERT INTO stock_items(catalog_id, available, stock_left, updated_at) "
            "VALUES(%s,%s,%s,%s) "
            "ON CONFLICT(catalog_id) DO UPDATE SET available = excluded.available, "
            "stock_left = excluded.stock_left, updated_at = excluded.updated_at",
            (int(catalog_id), bool(available), stock_left, now),
        )
        await self.conn.commit()

    async def stock_overrides(self) -> dict[int, dict[str, Any]]:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT catalog_id, available, stock_left FROM stock_items"
        )
        return {
            int(row["catalog_id"]): {
                "available": bool(row["available"]),
                "stock_left": row["stock_left"],
            }
            for row in await cursor.fetchall()
        }

    # ------------------------------------------------------------------
    # версии прайса
    # ------------------------------------------------------------------
    async def add_price_version(self, path: str, summary: str, items: int) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "INSERT INTO price_versions(created_at, path, summary, items) "
            "VALUES(%s,%s,%s,%s)",
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                str(path),
                summary,
                int(items),
            ),
        )
        await self.conn.commit()

    async def list_price_versions(self, limit: int = 5) -> list[dict[str, Any]]:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT * FROM price_versions ORDER BY id DESC LIMIT %s", (limit,)
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def get_price_version(self, version_id: int) -> dict[str, Any] | None:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT * FROM price_versions WHERE id = %s", (int(version_id),)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def prune_price_versions(self, keep: int = 5) -> list[str]:
        """Оставляет последние N версий, возвращает пути удалённых файлов."""
        assert self.conn is not None
        rows = await self.list_price_versions(500)
        old = rows[keep:]
        if not old:
            return []
        ids = [int(row["id"]) for row in old]
        placeholders = ",".join("%s" for _ in ids)
        await self.conn.execute(
            f"DELETE FROM price_versions WHERE id IN ({placeholders})", ids
        )
        await self.conn.commit()
        return [str(row["path"]) for row in old]

    # ------------------------------------------------------------------
    # отчёты и сводки
    # ------------------------------------------------------------------
    async def orders_for_date(self, day: str) -> list[dict[str, Any]]:
        """Заказы на дату доставки (без отменённых), с позициями."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT * FROM orders WHERE delivery_date = %s AND status != 'cancelled' "
            "ORDER BY delivery_time, id",
            (str(day),),
        )
        orders = [dict(row) for row in await cursor.fetchall()]
        for order in orders:
            items_cursor = await self.conn.execute(
                "SELECT * FROM order_items WHERE order_id = %s ORDER BY id",
                (order["id"],),
            )
            order["items"] = [dict(row) for row in await items_cursor.fetchall()]
        return orders

    async def orders_between(self, start: str, end: str) -> list[dict[str, Any]]:
        """Заказы по created_at в диапазоне [start, end) — для отчётов."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT * FROM orders WHERE created_at >= %s AND created_at < %s "
            "ORDER BY created_at",
            (start, end),
        )
        orders = [dict(row) for row in await cursor.fetchall()]
        for order in orders:
            items_cursor = await self.conn.execute(
                "SELECT * FROM order_items WHERE order_id = %s ORDER BY id",
                (order["id"],),
            )
            order["items"] = [dict(row) for row in await items_cursor.fetchall()]
        return orders

    async def user_weekdays(self, user_id: int, limit: int = 60) -> list[int]:
        """Дни недели, в которые повар заказывал (по дате доставки)."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT delivery_date FROM orders WHERE chef_id = %s "
            "AND status != 'cancelled' ORDER BY id DESC LIMIT %s",
            (int(user_id), limit),
        )
        result: list[int] = []
        for row in await cursor.fetchall():
            try:
                result.append(datetime.fromisoformat(str(row["delivery_date"])).weekday())
            except (TypeError, ValueError):
                continue
        return result

    async def active_chefs(self) -> list[int]:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT DISTINCT chef_id FROM orders WHERE chef_id IS NOT NULL"
        )
        return [int(row["chef_id"]) for row in await cursor.fetchall()]

    async def vacuum(self) -> None:
        assert self.conn is not None
        if self.is_postgres:
            # Postgres сам делает autovacuum; ручной VACUUM требует autocommit.
            return
        await self.conn.commit()
        await self.conn.execute("VACUUM")
        await self.conn.commit()

    async def ping(self) -> float:
        """Латентность простого запроса, мс — для /health."""
        assert self.conn is not None
        start = time.perf_counter()
        await self.conn.execute("SELECT 1 AS ok")
        return round((time.perf_counter() - start) * 1000, 1)

    async def size_bytes(self) -> int:
        assert self.conn is not None
        if self.is_postgres:
            cursor = await self.conn.execute(
                "SELECT pg_database_size(current_database()) AS size"
            )
            row = await cursor.fetchone()
            return int(row["size"]) if row else 0
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    async def stats(self) -> dict[str, int]:
        assert self.conn is not None
        cursor = await self.conn.execute("SELECT COUNT(*) AS c FROM orders")
        orders = (await cursor.fetchone())["c"]
        cursor = await self.conn.execute("SELECT COUNT(*) AS c FROM drafts")
        drafts = (await cursor.fetchone())["c"]
        return {"orders": int(orders), "drafts": int(drafts)}
