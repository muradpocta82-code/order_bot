"""Токены подтверждения цены: теперь в базе, а не в памяти (I3).

Раньше новое подтверждение прайса жило в словаре процесса: перезапуск бота
в момент подтверждения — и кнопки «Применить» становились мёртвыми.
Теперь запись лежит в `settings` под ключом `pending_price:<token>`,
а сам прайс — во временном xlsx-файле, который читается при применении.

Срок жизни — час. Просроченные записи и их файлы убирает обслуживание БД.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from .db import Database

log = logging.getLogger(__name__)

KEY_PREFIX = "pending_price:"
TTL_SECONDS = 3600


async def put_pending_price(
    db: Database, token: str, path: str | Path, items: int = 0
) -> None:
    """Сохраняет подтверждение прайса в БД (переживает перезапуск)."""
    payload = {
        "path": str(path),
        "created": time.time(),
        "items": int(items),
    }
    await db.set_setting(KEY_PREFIX + str(token), json.dumps(payload))


async def take_pending_price(db: Database, token: str) -> dict[str, Any] | None:
    """Достаёт и удаляет подтверждение. None — нет такого или просрочено."""
    key = KEY_PREFIX + str(token)
    raw = await db.get_setting(key)
    if not raw:
        return None
    await db.set_setting(key, "")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    path = Path(str(data.get("path") or ""))
    try:
        created = float(data.get("created") or 0)
    except (TypeError, ValueError):
        created = 0.0
    if time.time() - created > TTL_SECONDS:
        path.unlink(missing_ok=True)
        return None
    if not path.exists():
        return None
    data["path"] = str(path)
    return data


COSTS_PREFIX = "pending_costs:"


async def put_pending_costs(
    db: Database, token: str, lines: list[dict], actor_id: int = 0
) -> None:
    """Список закупочных цен ждёт подтверждения (переживает перезапуск)."""
    payload = {"created": time.time(), "actor": int(actor_id), "lines": lines}
    await db.set_setting(COSTS_PREFIX + str(token), json.dumps(payload, ensure_ascii=False))


async def take_pending_costs(db: Database, token: str) -> dict | None:
    key = COSTS_PREFIX + str(token)
    raw = await db.get_setting(key)
    if not raw:
        return None
    await db.set_setting(key, "")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    try:
        created = float(data.get("created") or 0)
    except (TypeError, ValueError):
        created = 0.0
    if time.time() - created > TTL_SECONDS:
        return None
    return data


async def purge_pending_prices(db: Database) -> int:
    """Убирает просроченные подтверждения и их файлы. Возвращает число удалённых."""
    removed = 0
    rows = await db.settings_like(KEY_PREFIX)
    now = time.time()
    for row in rows:
        key = str(row.get("key") or "")
        raw = str(row.get("value") or "")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            await db.set_setting(key, "")
            removed += 1
            continue
        try:
            created = float(data.get("created") or 0)
        except (TypeError, ValueError):
            created = 0.0
        if now - created <= TTL_SECONDS:
            continue
        path = Path(str(data.get("path") or ""))
        try:
            if path.exists():
                path.unlink()
        except OSError:
            log.debug("Не удалось удалить %s", path)
        await db.set_setting(key, "")
        removed += 1
    if removed:
        log.info("Убрано просроченных подтверждений прайса: %s", removed)
    return removed
