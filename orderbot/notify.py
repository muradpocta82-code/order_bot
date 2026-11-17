"""Уведомления, карточки заказов и тревоги о сбоях.

Вынесено из handlers.py: отправка карточек с повторами, короткие тревоги
менеджеру, сохранение текста заказа для повтора, гашение карточек.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from .config import Settings
from .db import Database
from .formatting import format_order, money, split_message

log = logging.getLogger(__name__)


async def _manager_panel_text(db: Database) -> str:
    new_count = await db.count_orders(("new",))
    in_work = await db.count_orders(("accepted",))
    ready = await db.count_orders(("ready",))
    delivering = await db.count_orders(("delivering",))
    total = await db.count_orders()
    revenue = await db.sum_orders(("new", "accepted", "ready", "delivering", "done"))
    return (
        "👔 <b>Панель менеджера</b>\n"
        "<i>Приём заказов и упаковка</i>\n\n"
        f"🆕 Новые: <b>{new_count}</b>\n"
        f"🔧 В работе: <b>{in_work}</b>\n"
        f"📦 Собрано: <b>{ready}</b>\n"
        f"🚚 В доставке: <b>{delivering}</b>\n"
        f"📜 Всего заказов: <b>{total}</b>\n"
        f"💰 Сумма (без отмен): <b>{money(revenue)}</b>\n\n"
        "Выберите раздел:"
    )


async def _courier_panel_text(db: Database) -> str:
    ready = await db.count_orders(("ready",))
    delivering = await db.count_orders(("delivering",))
    done = await db.count_orders(("done",))
    return (
        "🚚 <b>Панель доставщика</b>\n\n"
        f"📦 Ждут отправки: <b>{ready}</b>\n"
        f"🛣 В доставке: <b>{delivering}</b>\n"
        f"✅ Доставлено: <b>{done}</b>\n\n"
        "Активные доставки:"
    )


def _manager_chat(settings: Settings) -> int | None:
    """Упаковщик (он же менеджер) задаётся только через MANAGER_CHAT_ID."""
    return settings.manager_chat_id


def _courier_chat(settings: Settings) -> int | None:
    """Доставщик задаётся только через COURIER_CHAT_ID."""
    return settings.courier_chat_id


async def _send_order_card(
    bot: Bot, chat_id: int, order: dict, title: str, reply_markup=None
) -> int | None:
    """Отправляет карточку заказа, разбивая её на части при переполнении.

    Возвращает message_id последней части (у неё кнопки).
    """
    chunks = split_message(format_order(order, title))
    last_id: int | None = None
    for index, chunk in enumerate(chunks):
        last = index == len(chunks) - 1
        sent = await _send_with_retry(
            lambda chunk=chunk, last=last: bot.send_message(
                chat_id,
                chunk,
                reply_markup=reply_markup if last else None,
            )
        )
        if last and sent is not None:
            last_id = sent.message_id
    return last_id


async def _send_with_retry(send, attempts: int = 3):
    """Повторяет отправку при обрыве сети/503, чтобы заказ не потерялся.

    send — функция без аргументов, возвращающая корутину.
    TelegramBadRequest (бот заблокирован, чат не найден) не повторяем.
    """
    delay = 1.0
    for attempt in range(1, attempts + 1):
        try:
            return await send()
        except TelegramRetryAfter as exc:
            if attempt >= attempts:
                raise
            await asyncio.sleep(min(float(exc.retry_after) + 0.5, 30.0))
        except (TelegramNetworkError, TelegramServerError, OSError, asyncio.TimeoutError):
            if attempt >= attempts:
                raise
            log.warning(
                "Telegram: отправка не удалась (попытка %s/%s), повтор через %.0f с",
                attempt,
                attempts,
                delay,
            )
            await asyncio.sleep(delay)
            delay *= 2


RETRY_TEXT = (
    "Не получилось обработать заказ. Текст сохранил — "
    "нажмите «Попробовать ещё раз» или пришлите заново."
)
ERR_ALERT_WINDOW = 600  # тревога менеджеру не чаще раза в 10 минут на тип


async def _report_failure(
    bot: Bot, db: Database, settings: Settings, handler: str, exc: BaseException
) -> None:
    """Короткая тревога менеджеру о сбое. Полный traceback — только в лог."""
    manager = _manager_chat(settings)
    if manager is None or bot is None:
        return
    kind = type(exc).__name__
    key = f"err_alert:{handler}:{kind}"
    raw = await db.get_setting(key)
    try:
        last = float(raw) if raw else 0.0
    except ValueError:
        last = 0.0
    if time.time() - last < ERR_ALERT_WINDOW:
        return
    stamp = datetime.now(settings.timezone).strftime("%d.%m %H:%M")
    try:
        await bot.send_message(
            manager,
            f"⚠️ Сбой в обработчике «{handler}»: {kind}, {stamp}.\n"
            "Подробности в журнале, заказ сохранён для повтора.",
        )
        await db.set_setting(key, str(time.time()))
    except Exception:
        log.warning("Не удалось отправить тревогу о сбое %s", kind)


async def _remember_retry(
    db: Database, chat_id: int, text: str, source: str, transcript: str | None
) -> None:
    """Сохраняет текст повара, чтобы заказ не пропал при сбое."""
    await db.set_chat_state(
        chat_id,
        "retry",
        json.dumps(
            {"text": text, "source": source, "transcript": transcript},
            ensure_ascii=False,
        ),
    )


async def _take_retry(db: Database, chat_id: int) -> dict | None:
    raw = await db.get_chat_state(chat_id, "retry")
    if not raw:
        return None
    await db.set_chat_state(chat_id, "retry", "")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if data.get("text") else None


async def _detach_card(
    bot: Bot, chat_id: int | None, message_id: int | None, replacement: str
) -> None:
    """Удаляет карточку; если Telegram не даёт — заменяет одной строкой."""
    if not chat_id or not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
        return
    except Exception:
        pass
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=replacement,
            reply_markup=None,
        )
    except Exception:
        log.debug("Карточка %s/%s уже недоступна", chat_id, message_id)


async def _drop_card_buttons(
    bot: Bot, chat_id: int | None, message_id: int | None
) -> None:
    if not chat_id or not message_id:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except Exception:
        log.debug("Не удалось убрать кнопки у %s/%s", chat_id, message_id)


async def _close_order_cards(
    bot: Bot, order: dict, replacement: str, manager_fallback: int | None = None
) -> None:
    """Гасит карточки заказа у повара и менеджера (отмена)."""
    chef = order.get("chef_id")
    manager = order.get("manager_chat_id") or manager_fallback
    courier = order.get("courier_chat_id")
    await _detach_card(bot, chef, order.get("chef_card_message_id"), replacement)
    await _detach_card(bot, manager, order.get("manager_card_message_id"), replacement)
    await _detach_card(bot, courier, order.get("courier_card_message_id"), replacement)
