"""Тихие часы и пакетные уведомления менеджеру (D9).

Ночью карточки заказов не летят по одной: заказ ставится в очередь
(в таблице settings, поэтому переживает перезапуск), а утром менеджер
получает одно сообщение «ночью пришло N заказов» с кнопкой «Открыть очередь».

Тревоги о сбоях тихие часы не уважают — они уходят сразу (см. notify._report_failure,
main._send_alarm), это отдельные пути.

Настройки (таблица settings):
    quiet_hours = "22-8"  — окно тишины; "off" — выключено
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import Settings
from .db import Database
from .formatting import esc, money

log = logging.getLogger(__name__)

QUIET_KEY = "quiet_hours"
QUEUE_KEY = "quiet_queue"
DEFAULT_QUIET = "22-8"
MAX_QUEUE = 200


def parse_window(raw: str | None) -> tuple[int, int] | None:
    """«22-8» → (22, 8). «off», пусто или мусор → None (тишина выключена)."""
    text = (raw or "").strip().lower().replace(" ", "")
    if text in ("", "off", "0", "выкл", "нет"):
        return None
    if "-" not in text:
        return None
    left, _, right = text.partition("-")
    try:
        start = int(left)
        end = int(right)
    except ValueError:
        return None
    if not (0 <= start <= 23 and 0 <= end <= 24):
        return None
    if start == end:
        return None
    return start, end


def window_text(window: tuple[int, int] | None) -> str:
    if window is None:
        return "выключены"
    return f"с {window[0]:02d}:00 до {window[1] % 24:02d}:00"


def in_window(now: datetime, window: tuple[int, int] | None) -> bool:
    """Попадает ли момент в окно тишины (окно может переходить через полночь)."""
    if window is None:
        return False
    start, end = window
    hour = now.hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


async def quiet_window(db: Database) -> tuple[int, int] | None:
    return parse_window(await db.get_setting(QUIET_KEY, DEFAULT_QUIET))


async def quiet_now(db: Database, settings: Settings) -> bool:
    return in_window(datetime.now(settings.timezone), await quiet_window(db))


async def load_queue(db: Database) -> list[dict]:
    raw = await db.get_setting(QUEUE_KEY, "")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("Очередь тихих часов испорчена, очищаю")
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


async def save_queue(db: Database, items: list[dict]) -> None:
    await db.set_setting(QUEUE_KEY, json.dumps(items[-MAX_QUEUE:], ensure_ascii=False))


async def queue_order(db: Database, order: dict) -> None:
    """Откладывает заказ до утра: одна строка вместо карточки."""
    items = await load_queue(db)
    order_id = int(order.get("id") or 0)
    if any(int(item.get("id") or 0) == order_id for item in items):
        return
    items.append(
        {
            "id": order_id,
            "name": str(order.get("chef_name") or order.get("chef_id") or ""),
            "slot": str(order.get("delivery_time") or ""),
            "date": str(order.get("delivery_date") or ""),
            "total": float(order.get("total") or 0),
            "ts": datetime.now(tz=None).timestamp(),
        }
    )
    await save_queue(db, items)
    log.info("Заказ №%s отложен до утра (тихие часы)", order_id)


def queue_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗂 Открыть очередь", callback_data="quiet:queue")]
        ]
    )


def digest_text(items: list[dict]) -> str:
    total = sum(float(item.get("total") or 0) for item in items)
    lines = [
        f"🌙 <b>Пока было тихо</b>: {len(items)} заказ(ов) на {money(total)}"
    ]
    for item in items[:15]:
        name = esc(item.get("name") or "клиент")
        slot = esc(item.get("slot") or "без слота")
        lines.append(f"   №{item.get('id')} {name} · {slot} · {money(float(item.get('total') or 0))}")
    if len(items) > 15:
        lines.append(f"   … и ещё {len(items) - 15}")
    lines.append("")
    lines.append("Карточки не дублирую — вся очередь внутри.")
    return "\n".join(lines)


async def flush_queue(bot: Bot, db: Database, settings: Settings) -> int:
    """Отправляет одно сводное сообщение, если тишина закончилась.

    Возвращает число отправленных заказов (0 — ничего не делали).
    """
    items = await load_queue(db)
    if not items:
        return 0
    if await quiet_now(db, settings):
        return 0
    manager = settings.manager_chat_id
    if manager is None:
        return 0
    try:
        await bot.send_message(
            manager, digest_text(items), reply_markup=queue_keyboard()
        )
    except Exception as exc:
        log.warning("Не удалось отправить утреннюю сводку тихих часов: %s", exc)
        return 0
    await save_queue(db, [])
    log.info("Сводка тихих часов отправлена: %s заказ(ов)", len(items))
    return len(items)


async def maybe_batch(
    db: Database, settings: Settings, chat_id: int, order: dict
) -> bool:
    """Нужно ли отложить карточку этого заказа до утра."""
    manager = settings.manager_chat_id
    if manager is None or int(chat_id) != int(manager):
        return False
    if not await quiet_now(db, settings):
        return False
    await queue_order(db, order)
    return True


_BOUND: tuple[Database, Settings] | None = None


def bind(db: Database, settings: Settings) -> None:
    """Запоминает БД и настройки, чтобы notify мог спрашивать без лишних аргументов."""
    global _BOUND
    _BOUND = (db, settings)


async def maybe_batch_bound(chat_id: int, order: dict) -> bool:
    if _BOUND is None:
        return False
    db, settings = _BOUND
    return await maybe_batch(db, settings, chat_id, order)
