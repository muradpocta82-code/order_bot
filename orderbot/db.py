"""Слой хранения: PostgreSQL (psycopg), черновики, заказы, настройки."""
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
        """Подключение к PostgreSQL. SQLite больше не поддерживается (I2)."""
        if not self.url:
            raise RuntimeError(
                "DATABASE_URL не задан: бот работает только с PostgreSQL. "
                "SQLite-режим удалён 19.11.2026."
            )
        from .pg_psycopg import PsycopgConnection

        self.conn = PsycopgConnection(self.url)
        await self.conn.connect()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def init(self) -> None:
        """Оставлено для совместимости: схему строит scripts/migrate.py.

        Раньше здесь был in-code раннер для SQLite; он удалён 19.11.2026 (I2).
        """
        return

    def _tx(self):
        """Транзакция: psycopg даёт свою, для pg8000 — ручная."""
        return _LockedTx(self)

    # ------------------------------------------------------------------
    # настройки
    # ------------------------------------------------------------------
    async def settings_like(self, prefix: str) -> list[dict[str, Any]]:
        """Пары ключ-значение по префиксу (например, все приглашения ref:)."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE %s ORDER BY key",
            (f"{prefix}%",),
        )
        return [dict(row) for row in await cursor.fetchall()]

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

    async def add_missing_request(self, user_id: int, phrase: str) -> None:
        """Запоминает, кто и что просил вне прайса (B11)."""
        assert self.conn is not None
        clean = " ".join(str(phrase).lower().replace("ё", "е").split())
        if not clean:
            return
        await self.conn.execute(
            "INSERT INTO missing_requests(user_id, phrase) VALUES (%s, %s)",
            (int(user_id or 0), clean[:200]),
        )
        await self.conn.commit()

    async def missing_digest(
        self, days: int = 30, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Топ запросов вне прайса: сколько раз и сколько разных клиентов."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT phrase, count(*) AS count, count(DISTINCT user_id) AS users "
            "FROM missing_requests WHERE ts >= now() - (%s || ' days')::interval "
            "GROUP BY phrase ORDER BY count DESC, users DESC, phrase LIMIT %s",
            (int(days), int(limit)),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def missing_askers(self, phrase: str, days: int = 30) -> list[int]:
        """Кто просил позицию: id клиентов для уведомления о появлении."""
        assert self.conn is not None
        clean = " ".join(str(phrase).lower().replace("ё", "е").split())
        cursor = await self.conn.execute(
            "SELECT DISTINCT user_id FROM missing_requests "
            "WHERE phrase = %s AND ts >= now() - (%s || ' days')::interval "
            "AND user_id IS NOT NULL",
            (clean[:200], int(days)),
        )
        return [int(row["user_id"]) for row in await cursor.fetchall() if row["user_id"]]

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

    async def max_qty_per_item(self, chef_id: int) -> dict[int, float]:
        """Сколько максимум клиент когда-либо брал по каждой позиции."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT oi.catalog_id, MAX(oi.qty) AS m "
            "FROM order_items oi JOIN orders o ON o.id = oi.order_id "
            "WHERE o.chef_id = %s AND oi.catalog_id IS NOT NULL "
            "AND o.status != 'cancelled' GROUP BY oi.catalog_id",
            (int(chef_id),),
        )
        result: dict[int, float] = {}
        for row in await cursor.fetchall():
            try:
                result[int(row["catalog_id"])] = float(row["m"] or 0)
            except (TypeError, ValueError):
                continue
        return result

    async def recent_order(self, chef_id: int, minutes: int = 10) -> dict[str, Any] | None:
        """Последний неотменённый заказ клиента за последние N минут (с позициями)."""
        assert self.conn is not None
        cutoff = (datetime.now() - timedelta(minutes=max(1, minutes))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cursor = await self.conn.execute(
            "SELECT id FROM orders WHERE chef_id = %s AND status != 'cancelled' "
            "AND created_at >= %s ORDER BY created_at DESC, id DESC LIMIT 1",
            (int(chef_id), cutoff),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return await self.get_order(int(row["id"]))

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
    # курс доллара
    # ------------------------------------------------------------------
    async def usd_rate(self) -> float:
        """Текущий курс из настроек (0 — не показывать суммы в сумах)."""
        raw = await self.get_setting("usd_rate", "0")
        try:
            return max(0.0, float(raw or 0))
        except (TypeError, ValueError):
            return 0.0

    async def set_usd_rate(self, rate: float, source: str = "manager") -> None:
        """Ставит курс и пишет его в историю (для отчёта по прошлым периодам)."""
        await self.set_setting("usd_rate", str(rate))
        assert self.conn is not None
        try:
            await self.conn.execute(
                "INSERT INTO rate_history(rate, source, ts) VALUES(%s, %s, %s)",
                (
                    round(float(rate), 4),
                    source,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            await self.conn.commit()
        except Exception:
            log.warning("Не удалось записать курс в историю", exc_info=True)

    async def rate_history(self, limit: int = 10) -> list[dict[str, Any]]:
        """Последние смены курса, свежие сверху."""
        assert self.conn is not None
        try:
            cursor = await self.conn.execute(
                "SELECT ts, rate, source FROM rate_history ORDER BY ts DESC, id DESC "
                "LIMIT %s",
                (int(limit),),
            )
            return [dict(row) for row in await cursor.fetchall()]
        except Exception:
            return []

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
            # Слепок курса: смена курса не меняет суммы уже подтверждённого заказа.
            if self.is_postgres:
                rate = await self.usd_rate()
                if rate > 0:
                    try:
                        await self.conn.execute(
                            "UPDATE orders SET usd_rate_at_order = %s, total_local = %s "
                            "WHERE id = %s",
                            (round(rate, 4), round(total * rate, 2), order_id),
                        )
                    except Exception:
                        log.warning(
                            "Не удалось сохранить курс в заказе %s", order_id,
                            exc_info=True,
                        )
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
        note = await self.get_setting(f"client_note:{order.get('chef_id')}", "")
        if note:
            order["client_note"] = note
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

    async def orders_for_client(
        self, chef_id: int, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Последние заказы клиента — для кнопки «Мои заказы»."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT id, created_at, delivery_date, delivery_time, total, status, "
            "payment_status FROM orders WHERE chef_id = %s ORDER BY id DESC LIMIT %s",
            (int(chef_id), int(limit)),
        )
        return [dict(row) for row in await cursor.fetchall()]

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

    async def log_parse(
        self,
        chef_id: int | None,
        chat_id: int | None,
        source: str,
        path: str,
        items: int,
        questions: int,
        latency_ms: int | None,
        text_len: int,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Одна строка на каждый разобранный заказ: качество и скорость разбора."""
        if self.conn is None:
            return
        try:
            await self.conn.execute(
                "INSERT INTO parse_events(chef_id, chat_id, source, path, items, questions, "
                "latency_ms, text_len, payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                (
                    chef_id,
                    chat_id,
                    source,
                    path,
                    int(items),
                    int(questions),
                    latency_ms,
                    int(text_len),
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )
            await self.conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("Событие разбора не записано: %s", exc)

    async def ops_stats(self, days: int = 30) -> dict[str, Any]:
        """Скорость работы и причины сбоев — из журнала событий заказов."""
        assert self.conn is not None
        since = (datetime.now() - timedelta(days=int(days))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cursor = await self.conn.execute(
            "SELECT o.id, "
            "MIN(e_pack.created_at) AS packed_at, "
            "MIN(e_done.created_at) AS done_at, o.created_at AS created_at "
            "FROM orders o "
            "LEFT JOIN order_events e_pack ON e_pack.order_id = o.id "
            "  AND e_pack.kind = 'status' AND e_pack.payload->>'to' = 'ready' "
            "LEFT JOIN order_events e_done ON e_done.order_id = o.id "
            "  AND e_done.kind = 'status' AND e_done.payload->>'to' = 'done' "
            "WHERE o.created_at >= %s AND o.status != 'cancelled' "
            "GROUP BY o.id, o.created_at",
            (since,),
        )
        rows = [dict(row) for row in await cursor.fetchall()]

        def _minutes(start, end) -> float | None:
            if not start or not end:
                return None
            try:
                if isinstance(start, str):
                    start = datetime.fromisoformat(start[:19])
                if isinstance(end, str):
                    end = datetime.fromisoformat(end[:19])
                if getattr(start, "tzinfo", None) is not None:
                    start = start.replace(tzinfo=None)
                if getattr(end, "tzinfo", None) is not None:
                    end = end.replace(tzinfo=None)
                return max((end - start).total_seconds() / 60, 0)
            except (TypeError, ValueError):
                return None

        pack_times = [value for value in (
            _minutes(row.get("created_at"), row.get("packed_at")) for row in rows
        ) if value is not None]
        deliver_times = [value for value in (
            _minutes(row.get("packed_at"), row.get("done_at")) for row in rows
        ) if value is not None]

        failed_cursor = await self.conn.execute(
            "SELECT COALESCE(payload->>'reason', 'без причины') AS reason, "
            "COUNT(*) AS n FROM order_events WHERE kind = 'failed' "
            "AND created_at >= %s GROUP BY 1 ORDER BY n DESC",
            (since,),
        )
        failed = [dict(row) for row in await failed_cursor.fetchall()]

        claims_cursor = await self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM claims WHERE created_at >= %s "
            "GROUP BY 1 ORDER BY n DESC",
            (since,),
        )
        claims = [dict(row) for row in await claims_cursor.fetchall()]

        return {
            "orders": len(rows),
            "pack_avg": sum(pack_times) / len(pack_times) if pack_times else None,
            "pack_count": len(pack_times),
            "deliver_avg": (
                sum(deliver_times) / len(deliver_times) if deliver_times else None
            ),
            "deliver_count": len(deliver_times),
            "failed": failed,
            "claims": claims,
        }

    async def parse_stats(self, days: int = 30) -> dict[str, Any]:
        assert self.conn is not None
        since = (datetime.now() - timedelta(days=int(days))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cursor = await self.conn.execute(
            "SELECT source, path, COUNT(*) AS n, "
            "AVG(questions) AS avg_q, AVG(latency_ms) AS avg_ms, "
            "AVG(items) AS avg_items "
            "FROM parse_events WHERE created_at >= %s "
            "GROUP BY source, path ORDER BY n DESC",
            (since,),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        total_cursor = await self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(questions), 0) AS q "
            "FROM parse_events WHERE created_at >= %s",
            (since,),
        )
        total = dict(await total_cursor.fetchone() or {})
        return {"rows": rows, "total": int(total.get("n") or 0), "questions": int(total.get("q") or 0)}

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

    async def set_cost(
        self, catalog_id: int, cost: float, source: str = "manager", day: str | None = None
    ) -> None:
        """Закупочная цена позиции с даты (по умолчанию с сегодня)."""
        assert self.conn is not None
        await self.conn.execute(
            "INSERT INTO cost_prices(catalog_id, cost, valid_from, source) "
            "VALUES (%s, %s, %s, %s)",
            (int(catalog_id), round(float(cost), 4), day or datetime.now().date().isoformat(), source),
        )
        await self.conn.commit()

    async def costs_map(self) -> dict[int, float]:
        """Действующая закупочная цена по каждой позиции (последняя запись)."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT DISTINCT ON (catalog_id) catalog_id, cost FROM cost_prices "
            "ORDER BY catalog_id, valid_from DESC, id DESC"
        )
        return {int(row["catalog_id"]): float(row["cost"]) for row in await cursor.fetchall()}

    async def cost_on(self, catalog_id: int, day: str) -> float | None:
        """Закупочная цена, действовавшая на дату."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT cost FROM cost_prices WHERE catalog_id = %s AND valid_from <= %s "
            "ORDER BY valid_from DESC, id DESC LIMIT 1",
            (int(catalog_id), day),
        )
        row = await cursor.fetchone()
        return float(row["cost"]) if row else None

    async def cost_history(self, catalog_id: int, limit: int = 10) -> list[dict[str, Any]]:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT cost, valid_from, source, created_at FROM cost_prices "
            "WHERE catalog_id = %s ORDER BY valid_from DESC, id DESC LIMIT %s",
            (int(catalog_id), int(limit)),
        )
        return [dict(row) for row in await cursor.fetchall()]

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

    async def set_credit_days(self, chef_id: int, days: int) -> None:
        """Только срок отсрочки — лимит суммы не трогаем."""
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE users SET credit_days = %s, updated_at = %s WHERE user_id = %s",
            (int(days), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(chef_id)),
        )
        await self.conn.commit()

    async def _count(self, query: str, params: tuple = ()) -> int:
        assert self.conn is not None
        cursor = await self.conn.execute(query, params)
        row = await cursor.fetchone()
        return int(list(dict(row).values())[0]) if row else 0

    async def webapp_stats(self, days: int = 28) -> dict[str, int]:
        """Открытия и заказы из мини-приложения за период (G5)."""
        assert self.conn is not None
        cutoff = (datetime.now() - timedelta(days=max(1, days))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        result = {"opens": 0, "users": 0, "orders": 0, "orders_users": 0}
        try:
            result["opens"] = await self._count(
                "SELECT COUNT(*) AS c FROM webapp_events "
                "WHERE kind = 'open' AND ts >= %s",
                (cutoff,),
            )
            result["users"] = await self._count(
                "SELECT COUNT(DISTINCT user_id) AS c FROM webapp_events "
                "WHERE kind = 'open' AND ts >= %s",
                (cutoff,),
            )
            result["orders"] = await self._count(
                "SELECT COUNT(*) AS c FROM webapp_events "
                "WHERE kind = 'order' AND ts >= %s",
                (cutoff,),
            )
            result["orders_users"] = await self._count(
                "SELECT COUNT(DISTINCT user_id) AS c FROM webapp_events "
                "WHERE kind = 'order' AND ts >= %s",
                (cutoff,),
            )
        except Exception:
            log.warning("Статистика мини-аппа недоступна", exc_info=True)
        return result

    async def client_funnel(self, active_days: int = 30) -> dict[str, int]:
        """Воронка клиентов: заявки → допущены → первый заказ → активные."""
        cutoff = (datetime.now() - timedelta(days=active_days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        return {
            "total": await self._count("SELECT COUNT(*) AS c FROM users"),
            "approved": await self._count(
                "SELECT COUNT(*) AS c FROM users WHERE access = %s", ("approved",)
            ),
            "pending": await self._count(
                "SELECT COUNT(*) AS c FROM users WHERE access = %s", ("pending",)
            ),
            "rejected": await self._count(
                "SELECT COUNT(*) AS c FROM users WHERE access = %s", ("rejected",)
            ),
            "ordered": await self._count(
                "SELECT COUNT(DISTINCT chef_id) AS c FROM orders WHERE chef_id IS NOT NULL"
            ),
            "active": await self._count(
                "SELECT COUNT(DISTINCT chef_id) AS c FROM orders "
                "WHERE chef_id IS NOT NULL AND created_at >= %s",
                (cutoff,),
            ),
        }

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
            "SELECT i.catalog_id, i.name, i.qty, i.price, o.chef_id, o.chef_name, "
            "o.created_at AS created_at "
            "FROM order_items i "
            "JOIN orders o ON o.id = i.order_id "
            "WHERE o.status = 'done' AND o.created_at >= %s",
            (since,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def client_usage(self, chef_id: int, days: int = 30) -> dict[str, Any]:
        """Что клиент покупал за период: суммы, позиции, деньги по дням."""
        assert self.conn is not None
        since = (datetime.now() - timedelta(days=int(days))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cursor = await self.conn.execute(
            "SELECT i.catalog_id, i.name, i.unit, SUM(i.qty) AS q, "
            "SUM(i.total) AS s, COUNT(*) AS n FROM order_items i "
            "JOIN orders o ON o.id = i.order_id "
            "WHERE o.chef_id = %s AND o.status != 'cancelled' AND o.created_at >= %s "
            "GROUP BY i.catalog_id, i.name, i.unit ORDER BY s DESC",
            (int(chef_id), since),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        totals_cursor = await self.conn.execute(
            "SELECT COUNT(*) AS orders, COALESCE(SUM(total), 0) AS sum "
            "FROM orders WHERE chef_id = %s AND status != 'cancelled' "
            "AND created_at >= %s",
            (int(chef_id), since),
        )
        totals = dict(await totals_cursor.fetchone() or {})
        return {
            "orders": int(totals.get("orders") or 0),
            "sum": float(totals.get("sum") or 0),
            "items": rows,
        }

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

    async def client_stats(self, chef_id: int) -> dict[str, Any]:
        """Заказы, выручка и последняя активность клиента (F6)."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS s, MAX(created_at) AS last "
            "FROM orders WHERE chef_id = %s AND status = 'done'",
            (int(chef_id),),
        )
        row = await cursor.fetchone()
        orders = int(row["n"] or 0) if row else 0
        revenue = float(row["s"] or 0) if row else 0.0
        last = row["last"] if row else None
        return {
            "orders": orders,
            "revenue": revenue,
            "last": str(last) if last else "",
            "dates": await self.client_order_dates(chef_id, 20),
        }

    async def client_claims_count(self, chef_id: int) -> int:
        """Сколько претензий было по заказам клиента."""
        assert self.conn is not None
        try:
            return await self._count(
                "SELECT COUNT(*) AS c FROM claims cl "
                "JOIN orders o ON o.id = cl.order_id WHERE o.chef_id = %s",
                (int(chef_id),),
            )
        except Exception:
            return 0

    async def client_avg_rating(self, chef_id: int) -> float | None:
        """Средняя оценка клиента по доставленным заказам."""
        assert self.conn is not None
        try:
            cursor = await self.conn.execute(
                "SELECT AVG(rating) AS a FROM orders "
                "WHERE chef_id = %s AND rating IS NOT NULL",
                (int(chef_id),),
            )
            row = await cursor.fetchone()
            if not row or row["a"] is None:
                return None
            return float(row["a"])
        except Exception:
            return None

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

    async def client_order_days(self, days: int = 120) -> list[dict[str, Any]]:
        """Даты заказов по клиентам за период (C2: привычный день недели)."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT o.chef_id, COALESCE(u.name, o.chef_name, o.chef_id::text) AS chef_name, "
            "o.delivery_date::date AS day "
            "FROM orders o LEFT JOIN users u ON u.user_id = o.chef_id "
            "WHERE o.status NOT IN ('cancelled') "
            "AND o.created_at >= now() - (%s || ' days')::interval "
            "ORDER BY o.chef_id, o.delivery_date",
            (int(days),),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def ordered_on(self, chef_id: int, day: str) -> bool:
        """Есть ли у клиента заказ на эту дату доставки."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT 1 FROM orders WHERE chef_id = %s AND delivery_date::date = %s "
            "AND status != 'cancelled' LIMIT 1",
            (int(chef_id), str(day)[:10]),
        )
        return await cursor.fetchone() is not None

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

    async def close_claim(self, claim_id: int, status: str = "closed") -> None:
        """Закрывает претензию с итогом: replaced / credit / rejected / closed."""
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE claims SET status = %s WHERE id = %s", (status, int(claim_id))
        )
        await self.conn.commit()

    async def get_claim(self, claim_id: int) -> dict[str, Any] | None:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT * FROM claims WHERE id = %s", (int(claim_id),)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # книга расчётов (B3)
    # ------------------------------------------------------------------
    async def client_open_orders(self, client_id: int) -> list[dict[str, Any]]:
        """Заказы клиента с остатком долга, старые сверху — для зачёта FIFO."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT id, total, paid_amount, status, created_at FROM orders "
            "WHERE chef_id = %s AND status != 'cancelled' "
            "ORDER BY created_at, id",
            (int(client_id),),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def add_payment(
        self,
        client_id: int,
        amount: float,
        method: str = "cash",
        note: str = "",
        source: str = "manager",
    ) -> dict[str, Any]:
        """Принятая сумма: пишет платёж и раскидывает его по заказам FIFO (B3).

        Старые поля заказа (payment_status/paid_amount) тоже обновляются —
        до полного перехода отчётов на книгу.
        """
        from .ledger import fifo_allocate, open_amount

        assert self.conn is not None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        amount = round(float(amount), 2)
        if amount <= 0:
            return {"payment_id": 0, "allocated": [], "advance": 0.0}
        cursor = await self.conn.execute(
            "INSERT INTO payments(client_id, amount, method, ts, note, source) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (int(client_id), amount, method, now, note, source),
        )
        row = await cursor.fetchone()
        payment_id = int(row["id"])
        orders = await self.client_open_orders(client_id)
        allocations, advance = fifo_allocate(amount, orders)
        by_id = {int(order["id"]): order for order in orders}
        for order_id, part in allocations:
            await self.conn.execute(
                "INSERT INTO allocations(payment_id, order_id, amount, created_at) "
                "VALUES (%s, %s, %s, %s)",
                (payment_id, int(order_id), part, now),
            )
            order = by_id[order_id]
            paid_total = round(float(order.get("paid_amount") or 0) + part, 2)
            total = float(order.get("total") or 0)
            closed = paid_total + 0.005 >= total
            await self.conn.execute(
                "UPDATE orders SET paid_amount = %s, payment_status = %s, "
                "paid_at = COALESCE(paid_at, %s) WHERE id = %s",
                (paid_total, "paid" if closed else "unpaid", now, int(order_id)),
            )
        await self.conn.execute(
            "UPDATE payments SET note = %s WHERE id = %s",
            (
                (note + (" | аванс" if advance > 0 else "")).strip(" |"),
                payment_id,
            ),
        )
        await self.conn.commit()
        return {
            "payment_id": payment_id,
            "allocated": allocations,
            "advance": advance,
        }

    async def add_writeoff(
        self,
        catalog_id: int | None,
        name: str,
        qty: float,
        unit: str,
        reason: str,
        cost: float | None,
        actor_id: int = 0,
    ) -> int:
        """Записывает списание/усушку (B9)."""
        assert self.conn is not None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self.conn.execute(
            "INSERT INTO writeoffs(catalog_id, name, qty, unit, reason, cost, actor_id, ts) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                int(catalog_id) if catalog_id else None,
                name[:200],
                round(float(qty), 3),
                unit[:20],
                reason[:100],
                round(float(cost), 4) if cost is not None else None,
                int(actor_id or 0),
                now,
            ),
        )
        writeoff_id = int((await cursor.fetchone())["id"])
        await self.conn.commit()
        return writeoff_id

    async def writeoff_rows(self, days: int = 30) -> list[dict[str, Any]]:
        """Списания за период (B9) — для отчёта по потерям."""
        assert self.conn is not None
        since = (datetime.now() - timedelta(days=max(1, days))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cursor = await self.conn.execute(
            "SELECT catalog_id, name, qty, unit, reason, cost, "
            "COALESCE(cost, 0) * qty AS cost_value FROM writeoffs WHERE ts >= %s "
            "ORDER BY ts DESC",
            (since,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def ledger_client_debts(self) -> list[dict[str, Any]]:
        """Долги по книге: доставлено, но не оплачено (B3, шаг 2)."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT o.chef_id, MAX(o.chef_name) AS chef_name, COUNT(*) AS n, "
            "COALESCE(SUM(o.total - COALESCE(o.paid_amount, 0)), 0) AS open, "
            "MIN(o.created_at) AS since "
            "FROM orders o WHERE o.chef_id IS NOT NULL AND o.status = 'done' "
            "AND o.payment_status != 'paid' "
            "GROUP BY o.chef_id HAVING COALESCE(SUM(o.total - COALESCE(o.paid_amount, 0)), 0) > 0.01 "
            "ORDER BY open DESC"
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def add_credit_note(
        self,
        client_id: int,
        amount: float,
        reason: str = "",
        order_id: int | None = None,
    ) -> int:
        """Кредит-нота: зачёт по претензии уменьшает долг клиента (B3)."""
        assert self.conn is not None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self.conn.execute(
            "INSERT INTO credit_notes(client_id, order_id, amount, reason, ts) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (
                int(client_id),
                int(order_id) if order_id else None,
                round(float(amount), 2),
                reason[:300],
                now,
            ),
        )
        note_id = int((await cursor.fetchone())["id"])
        await self.conn.commit()
        return note_id

    async def client_book_debt(self, client_id: int) -> dict[str, float]:
        """Долг клиента по книге: выставлено − оплачено − зачтено (B3)."""
        totals = await self.ledger_totals()
        billed = float(totals["billed"].get(int(client_id), 0.0))
        paid = float(totals["paid"].get(int(client_id), 0.0))
        credited = float(totals["credited"].get(int(client_id), 0.0))
        return {
            "billed": billed,
            "paid": paid,
            "credited": credited,
            "open": round(billed - paid - credited, 2),
        }

    async def credit_orders_sum(self, client_id: int) -> float:
        """Сколько всего помечено «в долг» — для /settle без суммы."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT COALESCE(SUM(total - COALESCE(paid_amount, 0)), 0) AS s "
            "FROM orders WHERE chef_id = %s AND payment_status = 'credit'",
            (int(client_id),),
        )
        row = await cursor.fetchone()
        return float(row["s"] or 0) if row else 0.0

    async def ledger_totals(self) -> dict[str, dict[int, float]]:
        """Итоги книги по клиентам: выставлено, оплачено, зачтено (B3)."""
        assert self.conn is not None
        billed: dict[int, float] = {}
        paid: dict[int, float] = {}
        credited: dict[int, float] = {}
        cursor = await self.conn.execute(
            "SELECT chef_id, SUM(total) AS s FROM orders "
            "WHERE chef_id IS NOT NULL AND status != 'cancelled' "
            "GROUP BY chef_id"
        )
        for row in await cursor.fetchall():
            billed[int(row["chef_id"])] = float(row["s"] or 0)
        cursor = await self.conn.execute(
            "SELECT client_id, SUM(amount) AS s FROM payments GROUP BY client_id"
        )
        for row in await cursor.fetchall():
            paid[int(row["client_id"])] = float(row["s"] or 0)
        cursor = await self.conn.execute(
            "SELECT client_id, SUM(amount) AS s FROM credit_notes GROUP BY client_id"
        )
        for row in await cursor.fetchall():
            credited[int(row["client_id"])] = float(row["s"] or 0)
        return {"billed": billed, "paid": paid, "credited": credited}

    async def client_orders_between(
        self, chef_id: int, start: str, end: str
    ) -> list[dict[str, Any]]:
        """Заказы клиента за период — для акта сверки (B4)."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT * FROM orders WHERE chef_id = %s AND created_at >= %s "
            "AND created_at <= %s ORDER BY created_at, id",
            (int(chef_id), start, end),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def client_credit_notes(
        self, chef_id: int, start: str, end: str
    ) -> list[dict[str, Any]]:
        """Претензии клиента с решением «кредит-нота» за период (B4)."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT cl.id, cl.order_id, cl.text, cl.status, cl.created_at "
            "FROM claims cl JOIN orders o ON o.id = cl.order_id "
            "WHERE o.chef_id = %s AND cl.status = %s "
            "AND cl.created_at >= %s AND cl.created_at <= %s ORDER BY cl.id",
            (int(chef_id), "credit", start, end),
        )
        notes = []
        for row in await cursor.fetchall():
            data = dict(row)
            data.setdefault("amount", 0)
            notes.append(data)
        return notes

    async def claims_for_order(self, order_id: int) -> list[dict[str, Any]]:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT * FROM claims WHERE order_id = %s ORDER BY id DESC",
            (int(order_id),),
        )
        return [dict(row) for row in await cursor.fetchall()]

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

    async def set_staff_shift(self, user_id: int, on_shift: bool) -> bool:
        """Ставит сотрудника на смену или снимает с неё (D4)."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "UPDATE staff SET on_shift = %s WHERE user_id = %s AND active",
            (bool(on_shift), int(user_id)),
        )
        await self.conn.commit()
        return int(cursor.rowcount or 0) == 1

    async def staff_on_shift(self, role: str = "manager") -> list[dict[str, Any]]:
        """Кто сейчас на смене в указанной роли."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT * FROM staff WHERE active AND on_shift AND role = %s "
            "ORDER BY last_order_at NULLS FIRST, user_id",
            (role,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def pick_packer(self) -> int | None:
        """Упаковщик на смене, который дольше всех не получал заказ (по кругу)."""
        rows = await self.staff_on_shift("manager")
        if not rows:
            return None
        return int(rows[0]["user_id"])

    async def assign_order(self, order_id: int, user_id: int) -> None:
        """Запоминает, кому отдан заказ, и отмечает занятость сотрудника."""
        assert self.conn is not None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.conn.execute(
            "UPDATE orders SET assigned_to = %s WHERE id = %s",
            (int(user_id), int(order_id)),
        )
        await self.conn.execute(
            "UPDATE staff SET last_order_at = %s WHERE user_id = %s",
            (now, int(user_id)),
        )
        await self.conn.commit()

    async def assigned_staff(self, order_id: int) -> dict[str, Any] | None:
        """Сотрудник, которому отдан заказ."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT s.* FROM staff s JOIN orders o ON o.assigned_to = s.user_id "
            "WHERE o.id = %s",
            (int(order_id),),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

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

    async def set_payer(self, user_id: int, venue_id: int, role: str = "payer") -> None:
        """Плательщик точки: получает накладные и напоминания о долге (C6)."""
        assert self.conn is not None
        await self.conn.execute(
            "INSERT INTO venue_contacts(user_id, venue_id, role) VALUES (%s, %s, %s) "
            "ON CONFLICT(user_id) DO UPDATE SET venue_id = excluded.venue_id, "
            "role = excluded.role",
            (int(user_id), int(venue_id), role[:20]),
        )
        await self.conn.commit()

    async def remove_payer(self, user_id: int) -> bool:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "DELETE FROM venue_contacts WHERE user_id = %s AND role = 'payer'",
            (int(user_id),),
        )
        await self.conn.commit()
        return int(cursor.rowcount or 0) > 0

    async def payers_for(self, venue_id: int) -> list[int]:
        """Кто оплачивает эту точку."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT user_id FROM venue_contacts WHERE venue_id = %s AND role = 'payer'",
            (int(venue_id),),
        )
        return [int(row["user_id"]) for row in await cursor.fetchall()]

    async def payer_rows(self) -> list[dict[str, Any]]:
        """Все плательщики: id, точка, имя точки если известно."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT v.user_id, v.venue_id, v.role, "
            "COALESCE(u.name, v.venue_id::text) AS venue_name "
            "FROM venue_contacts v LEFT JOIN users u ON u.user_id = v.venue_id "
            "WHERE v.role = 'payer' ORDER BY v.created_at",
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def staff_ratings(self, days: int = 7) -> list[dict[str, Any]]:
        """Оценки по упаковщикам и курьерам за период (C13)."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT 'packer' AS role, o.assigned_to AS staff_id, "
            "COALESCE(s.name, o.assigned_to::text) AS name, "
            "COUNT(*) AS n, SUM(o.rating) AS total, "
            "SUM(CASE WHEN o.rating <= 3 THEN 1 ELSE 0 END) AS low "
            "FROM orders o LEFT JOIN staff s ON s.user_id = o.assigned_to "
            "WHERE o.rating IS NOT NULL AND o.assigned_to IS NOT NULL "
            "AND o.created_at >= now() - (%s || ' days')::interval "
            "GROUP BY o.assigned_to, s.name "
            "UNION ALL "
            "SELECT 'courier' AS role, o.courier_chat_id AS staff_id, "
            "COALESCE(s.name, o.courier_chat_id::text) AS name, "
            "COUNT(*) AS n, SUM(o.rating) AS total, "
            "SUM(CASE WHEN o.rating <= 3 THEN 1 ELSE 0 END) AS low "
            "FROM orders o LEFT JOIN staff s ON s.user_id = o.courier_chat_id "
            "WHERE o.rating IS NOT NULL AND o.courier_chat_id IS NOT NULL "
            "AND o.created_at >= now() - (%s || ' days')::interval "
            "GROUP BY o.courier_chat_id, s.name",
            (int(days), int(days)),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def order_ratings(self, days: int = 7) -> list[int]:
        """Все оценки за период — для строки «средняя оценка»."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT rating FROM orders WHERE rating IS NOT NULL "
            "AND created_at >= now() - (%s || ' days')::interval",
            (int(days),),
        )
        return [int(row["rating"]) for row in await cursor.fetchall()]

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

    async def reschedule_order(
        self, order_id: int, new_date: str, status: str = "new"
    ) -> bool:
        """Переносит заказ на другую дату и возвращает его в работу (D7).

        Разрешено для недоставленного и для ещё не собранного заказа —
        доставленный или отменённый трогать нельзя.
        """
        assert self.conn is not None
        cursor = await self.conn.execute(
            "UPDATE orders SET delivery_date = %s, status = %s "
            "WHERE id = %s AND status IN ('failed', 'new', 'accepted', 'ready')",
            (str(new_date), str(status), int(order_id)),
        )
        await self.conn.commit()
        return int(cursor.rowcount or 0) == 1

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

    async def add_arrival(
        self,
        catalog_id: int | None,
        name: str,
        qty: float | None,
        unit: str,
        cost: float | None,
        actor_id: int = 0,
    ) -> int:
        """Записывает приход товара (G7): остаток и закупка уже применены."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "INSERT INTO arrivals(catalog_id, name, qty, unit, cost, actor_id) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                int(catalog_id) if catalog_id else None,
                name[:200],
                round(float(qty), 3) if qty is not None else None,
                (unit or "")[:20],
                round(float(cost), 4) if cost is not None else None,
                int(actor_id or 0),
            ),
        )
        row = await cursor.fetchone()
        await self.conn.commit()
        return int(row["id"]) if row else 0

    async def arrival_rows(self, days: int = 30) -> list[dict[str, Any]]:
        """Приходы за период, свежие сверху."""
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT id, catalog_id, name, qty, unit, cost, ts FROM arrivals "
            "WHERE ts >= now() - (%s || ' days')::interval ORDER BY ts DESC, id DESC",
            (int(days),),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def adjust_stock(
        self, catalog_id: int, delta: float, base: float | None = None
    ) -> float:
        """Меняет остаток позиции (списание, приход). Возвращает новый остаток.

        Если строки в stock_items ещё нет, берём остаток из прайса (`base`).
        """
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT available, stock_left FROM stock_items WHERE catalog_id = %s",
            (int(catalog_id),),
        )
        row = await cursor.fetchone()
        available = bool(row["available"]) if row else True
        if row is not None and row["stock_left"] is not None:
            current = float(row["stock_left"])
        else:
            current = float(base or 0.0)
        new_left = max(0.0, round(current + float(delta), 3))
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.conn.execute(
            "INSERT INTO stock_items(catalog_id, available, stock_left, updated_at) "
            "VALUES(%s,%s,%s,%s) "
            "ON CONFLICT(catalog_id) DO UPDATE SET available = excluded.available, "
            "stock_left = excluded.stock_left, updated_at = excluded.updated_at",
            (int(catalog_id), available, new_left, now),
        )
        await self.conn.commit()
        return new_left

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
