"""Заведение и допуск клиентов: профиль, заявка, уведомление менеджеру.

Новый повар сначала заполняет название заведения, телефон и адрес, затем
ждёт подтверждения менеджера; до подтверждения заказы не принимаются.
"""
from __future__ import annotations

import logging
import re

from aiogram.types import Message

from .config import Settings
from .db import Database
from .formatting import esc
from .keyboards import (
    client_menu_keyboard,
    contact_keyboard,
    location_keyboard,
    user_access_keyboard,
)
from .notify import _manager_chat
from .roles import _is_manager, _is_staff

log = logging.getLogger(__name__)


def _start_text() -> str:
    """Приветствие клиента живёт в handlers.py — импортируем по требованию."""
    from .handlers import START_TEXT

    return START_TEXT

async def _ask_venue(message: Message, db: Database) -> None:
    await db.set_chat_state(message.chat.id, "onboard", "venue")
    await message.answer("Как называется ваше заведение?")


async def _ask_phone(message: Message, db: Database) -> None:
    await db.set_chat_state(message.chat.id, "onboard", "phone")
    await message.answer(
        "📞 Телефон для доставщика? Пришлите кнопкой или напишите в формате +993 12 345678.",
        reply_markup=contact_keyboard(),
    )


async def _ask_address(message: Message, db: Database) -> None:
    await db.set_chat_state(message.chat.id, "onboard", "address")
    await message.answer(
        "📍 Адрес доставки: город, улица, дом, ориентир. Можно кнопкой геопозиции.",
        reply_markup=location_keyboard(),
    )


def _valid_phone(text: str) -> bool:
    return len(re.sub(r"\D", "", text)) >= 7 and bool(
        re.fullmatch(r"[+0-9()\-\s]{7,40}", text.strip())
    )


async def _finish_profile(
    message: Message,
    db: Database,
    settings: Settings,
    catalog: Catalog,
    user: dict,
) -> None:
    await db.set_chat_state(message.chat.id, "onboard", "")
    if user.get("access") == "approved":
        usually = await db.usual_items(message.from_user.id)
        await message.answer(
            f"Спасибо! Заведение: <b>{esc(user.get('venue') or '')}</b>.\n\n{_start_text()}",
            reply_markup=client_menu_keyboard(usually, catalog),
        )
        return
    await message.answer("Спасибо! Ждём подтверждения от менеджера.")
    if not await db.get_chat_state(user['user_id'], "acc_notified"):
        await db.set_chat_state(user['user_id'], "acc_notified", "1")
        await _notify_new_user(message.bot, db, settings, user)


async def _profile_step(
    message: Message, db: Database, user: dict, catalog: Catalog
) -> bool:
    """True — профиль не заполнен, бот уже задал следующий вопрос."""
    if not user.get("venue"):
        await _ask_venue(message, db)
        return True
    if not user.get("phone"):
        await _ask_phone(message, db)
        return True
    if not user.get("address"):
        await _ask_address(message, db)
        return True
    return False


async def _notify_new_user(
    bot: Bot, db: Database, settings: Settings, user: dict
) -> None:
    manager = _manager_chat(settings)
    if manager is None:
        return
    username = f" (@{user['username']})" if user.get("username") else ""
    text = (
        "🆕 <b>Новый клиент</b>\n"
        f"Имя: {esc(user.get('name') or '—')}{esc(username)}\n"
        f"Заведение: {esc(user.get('venue') or '—')}\n"
        f"Телефон: {esc(user.get('phone') or '—')}\n"
        f"Адрес: {esc(user.get('address') or '—')}"
    )
    try:
        await bot.send_message(
            manager, text, reply_markup=user_access_keyboard(int(user["user_id"]))
        )
    except Exception:
        log.warning("Не удалось отправить заявку на допуск менеджеру")


async def _user_access_gate(
    message: Message, db: Database, settings: Settings, catalog: Catalog
) -> bool:
    """True — сообщение уже обработано (профиль/ожидание/отказ)."""
    if _is_staff(settings, message.from_user.id, message.chat.id):
        return False
    if not (message.text or "").strip():
        return False
    user = await db.ensure_user(
        message.from_user.id,
        message.from_user.full_name,
        message.from_user.username,
    )
    onboard = await db.get_chat_state(message.chat.id, "onboard")
    text = (message.text or "").strip()
    if onboard == "venue":
        if len(text) < 2:
            await message.answer("Напишите название заведения одним сообщением.")
            return True
        await db.set_user_venue(message.from_user.id, text)
        user = await db.get_user(message.from_user.id) or user
        await _ask_phone(message, db)
        return True
    if onboard == "phone":
        if not _valid_phone(text):
            await message.answer(
                "Похоже, это не телефон. Напишите номер, например +993 12 345678."
            )
            return True
        await db.set_user_phone(message.from_user.id, text)
        user = await db.get_user(message.from_user.id) or user
        await _ask_address(message, db)
        return True
    if onboard == "address":
        if len(text) < 5:
            await message.answer(
                "Адрес слишком короткий. Напишите город, улицу и дом."
            )
            return True
        await db.set_user_address(message.from_user.id, text)
        user = await db.get_user(message.from_user.id) or user
        await _finish_profile(message, db, settings, catalog, user)
        return True

    if user.get("access") == "rejected":
        await message.answer("Доступ закрыт. Напишите менеджеру.")
        return True
    if await _profile_step(message, db, user, catalog):
        return True
    if user.get("access") == "pending":
        await message.answer(
            "Заявка принята. Менеджер откроет доступ в течение 2 часов "
            "(обычно быстрее). Если срочно — позвоните менеджеру."
        )
        if not await db.get_chat_state(user['user_id'], "acc_notified"):
            await db.set_chat_state(user['user_id'], "acc_notified", "1")
            await _notify_new_user(message.bot, db, settings, user)
        return True
    return False


# ---------------------------------------------------------------------------
# остатки, фасовки и отсечка доставки
# ---------------------------------------------------------------------------
