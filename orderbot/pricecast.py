"""Утреннее сообщение клиентам: свежие цены и кнопка «Как обычно».

Идея простая: заказывать должно быть проще, чем звонить. Раз в день активный
клиент получает короткое сообщение с изменениями цен и одной кнопкой, которая
собирает его обычный заказ.

Включается владельцем: команда /price_morning on (значение в настройках
`price_morning_enabled`). Отказ клиента хранится как `price_optout:<id>`.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from .catalog import Catalog
from .config import Settings
from .db import Database
from .formatting import money
from .keyboards import morning_price_keyboard

log = logging.getLogger(__name__)

SNAPSHOT_KEY = "price_snapshot"
SENT_KEY = "last_price_morning"


def price_changes(
    previous: dict[str, float], catalog: Catalog
) -> list[tuple[str, float | None, float | None]]:
    """Список изменений: (название, старая цена, новая). None — позиция новая/удалена."""
    changes: list[tuple[str, float | None, float | None]] = []
    current = {
        str(item.id): (item.name, float(item.price or 0)) for item in catalog.items
    }
    for key, (name, price) in current.items():
        old = previous.get(key)
        if old is None:
            continue  # новые позиции в утреннюю сводку не тащим, чтобы не шуметь
        if abs(float(old) - price) > 0.004:
            changes.append((name, float(old), price))
    for key, old in previous.items():
        if key not in current:
            changes.append((f"снята позиция #{key}", float(old), None))
    return changes


def _snapshot(catalog: Catalog) -> dict[str, float]:
    return {str(item.id): float(item.price or 0) for item in catalog.items}


def _changes_block(changes: list[tuple[str, float | None, float | None]]) -> str:
    if not changes:
        return "Изменений с прошлого раза нет."
    lines = [f"Изменения: <b>{len(changes)}</b>"]
    for name, old, new in changes[:8]:
        if new is None:
            lines.append(f"• {name} — больше не поставляем")
            continue
        if old is None:
            lines.append(f"• {name} — {money(new)}")
            continue
        arrow = "▲" if new > old else "▼"
        percent = (new - old) / old * 100 if old else 0
        lines.append(
            f"• {name}: {money(old)} → {money(new)} {arrow}{abs(percent):.0f}%"
        )
    if len(changes) > 8:
        lines.append(f"…и ещё {len(changes) - 8}")
    return "\n".join(lines)


async def send_morning_prices(
    bot: Bot, db: Database, settings: Settings, catalog: Catalog
) -> int:
    """Рассылка «цены на сегодня». Возвращает число доставленных сообщений."""
    if await db.get_setting("price_morning_enabled", "0") != "1":
        return 0
    now = datetime.now(settings.timezone)
    try:
        hour = int(await db.get_setting("price_morning_hour", "8") or 8)
    except ValueError:
        hour = 8
    if now.hour < hour:
        return 0
    today = now.date().isoformat()
    if await db.get_setting(SENT_KEY) == today:
        return 0

    raw = await db.get_setting(SNAPSHOT_KEY, "") or ""
    try:
        previous: dict[str, float] = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        previous = {}
    changes = price_changes(previous, catalog)
    text = (
        f"☀️ Доброе утро! Цены на {now.strftime('%d.%m')}.\n"
        f"Позиций в прайсе: <b>{len(catalog.items)}</b>\n\n"
        f"{_changes_block(changes)}\n\n"
        "Нужно то же, что обычно? Одна кнопка — и заказ у нас."
    )

    recipients = await db.active_client_ids(90)
    sent = 0
    for user_id in recipients:
        if await db.get_setting(f"price_optout:{user_id}", "0") == "1":
            continue
        try:
            await bot.send_message(
                int(user_id),
                text,
                reply_markup=morning_price_keyboard(int(user_id)),
            )
            sent += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(int(getattr(exc, "retry_after", 5)) + 1)
            continue
        except TelegramForbiddenError:
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("Утренние цены не отправлены %s: %s", user_id, exc)
        await asyncio.sleep(0.05)

    await db.set_setting(SNAPSHOT_KEY, json.dumps(_snapshot(catalog)))
    await db.set_setting(SENT_KEY, today)
    log.info("Утренние цены отправлены %s клиентам, изменений: %s", sent, len(changes))
    return sent


async def morning_enabled(db: Database) -> bool:
    return await db.get_setting("price_morning_enabled", "0") == "1"
