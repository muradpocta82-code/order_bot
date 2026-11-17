"""Роли и доступ: кто менеджер, кто доставщик, какая панель.

Список сотрудников подтягивается из таблицы staff; владельцы из .env
(manager_chat_id / courier_chat_id) всегда считаются сотрудниками.
"""
from __future__ import annotations

from aiogram.types import CallbackQuery

from .config import Settings
from .db import Database
from .keyboards import (
    client_back_keyboard,
    courier_back_keyboard,
    manager_back_keyboard,
)


EXTRA_MANAGERS: set[int] = set()
EXTRA_COURIERS: set[int] = set()


async def load_staff_from_db(db: Database) -> None:
    """Подтягивает сотрудников из таблицы staff."""
    EXTRA_MANAGERS.clear()
    EXTRA_COURIERS.clear()
    try:
        rows = await db.list_staff()
    except Exception:
        return
    for row in rows:
        uid = int(row["user_id"])
        if row["role"] == "manager":
            EXTRA_MANAGERS.add(uid)
        elif row["role"] == "courier":
            EXTRA_COURIERS.add(uid)


def _is_manager(
    settings: Settings, user_id: int | None = None, chat_id: int | None = None
) -> bool:
    manager = settings.manager_chat_id
    if user_id is not None and user_id in EXTRA_MANAGERS:
        return True
    if manager is None:
        return False
    return (user_id is not None and user_id == manager) or (
        chat_id is not None and chat_id == manager
    )


def _is_courier(
    settings: Settings, user_id: int | None = None, chat_id: int | None = None
) -> bool:
    if user_id is not None and user_id in EXTRA_COURIERS:
        return True
    courier = settings.courier_chat_id
    if courier is None:
        return False
    return (user_id is not None and user_id == courier) or (
        chat_id is not None and chat_id == courier
    )


def _is_staff(
    settings: Settings, user_id: int | None = None, chat_id: int | None = None
) -> bool:
    return _is_manager(settings, user_id, chat_id) or _is_courier(
        settings, user_id, chat_id
    )


def _back_keyboard(
    settings: Settings, user_id: int | None = None, chat_id: int | None = None
):
    if _is_manager(settings, user_id, chat_id):
        return manager_back_keyboard()
    if _is_courier(settings, user_id, chat_id):
        return courier_back_keyboard()
    return client_back_keyboard()


async def _manager_ok(callback: CallbackQuery, settings: Settings) -> bool:
    if callback.message is None:
        await callback.answer("Сообщение недоступно", show_alert=True)
        return False
    if not _is_manager(settings, callback.from_user.id, callback.message.chat.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return False
    return True
