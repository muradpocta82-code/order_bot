"""Панели менеджера и клиента: кнопки mp: и cl: (I1).

Вынесено из handlers.py. Панель менеджера (входящие, история, карточка заказа)
и меню клиента (прайс, последний заказ, помощь). Роутер подключается в
handlers.py после панели доставщика, порядок кнопок прежний.
"""
from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from .catalog import Catalog
from .config import Settings
from .db import Database
from .formatting import esc, format_catalog, format_client_order, format_order
from .handlers_commands import _courier_page, _orders_page
from .keyboards import (
    client_back_keyboard,
    client_menu_keyboard,
    client_order_keyboard,
    courier_back_keyboard,
    manager_back_keyboard,
    manager_panel_keyboard,
    manager_status_keyboard,
    order_detail_keyboard,
    orders_list_keyboard,
)
from .notify import _courier_panel_text, _edit_panel, _manager_panel_text
from .roles import _back_keyboard, _is_courier, _is_manager, _manager_ok
from .stock import hidden_ids, resolve as resolve_stock
from .texts import COURIER_HELP_TEXT, HELP_TEXT, MANAGER_HELP_TEXT, START_TEXT

log = logging.getLogger(__name__)

router = Router(name="orderbot_panels")


@router.callback_query(F.data == "mp:panel")
async def cb_manager_panel(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    if not await _manager_ok(callback, settings):
        return
    try:
        await callback.message.edit_text(
            await _manager_panel_text(db), reply_markup=manager_panel_keyboard()
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


async def _show_manager_list(
    callback: CallbackQuery, kind: str, page: int, db: Database, settings: Settings
) -> None:
    if not await _manager_ok(callback, settings):
        return
    text, keyboard = await _orders_page(db, kind, page)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("mp:inbox:"))
async def cb_manager_inbox(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    try:
        page = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        page = 0
    await _show_manager_list(callback, "inbox", page, db, settings)


@router.callback_query(F.data.startswith("mp:history:"))
async def cb_manager_history(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    try:
        page = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        page = 0
    await _show_manager_list(callback, "history", page, db, settings)


@router.callback_query(F.data.startswith("mp:view:"))
async def cb_manager_view(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    if not await _manager_ok(callback, settings):
        return
    try:
        _, _, order_raw, kind, page_raw = callback.data.split(":")
        order_id = int(order_raw)
        page = int(page_raw)
    except (ValueError, AttributeError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    if kind not in ("inbox", "history"):
        kind = "history"
    order = await db.get_order(order_id)
    if order is None:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    try:
        await callback.message.edit_text(
            format_order(order, "📦 Заказ"),
            reply_markup=order_detail_keyboard(
                order_id, kind, page, str(order.get("status"))
            ),
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data == "cl:price")
async def cb_client_price(
    callback: CallbackQuery, catalog: Catalog, settings: Settings, db: Database
) -> None:
    if callback.message is None:
        await callback.answer("Сообщение недоступно", show_alert=True)
        return
    keyboard = _back_keyboard(
        settings,
        callback.from_user.id,
        callback.message.chat.id,
    )
    rows = resolve_stock(catalog, await db.stock_overrides())
    await _edit_panel(
        callback.bot,
        callback.message.chat.id,
        callback.message.message_id,
        format_catalog(catalog, hidden_ids(rows)),
        keyboard,
    )
    await callback.answer()


@router.callback_query(F.data == "cl:last")
async def cb_client_last(callback: CallbackQuery, db: Database) -> None:
    if callback.message is None:
        await callback.answer("Сообщение недоступно", show_alert=True)
        return
    order = await db.last_order_for_user(callback.from_user.id)
    text = (
        "У вас пока нет заказов."
        if order is None
        else format_client_order(order, "📦 Последний заказ")
    )
    await _edit_panel(
        callback.bot,
        callback.message.chat.id,
        callback.message.message_id,
        text,
        client_back_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "cl:menu")
async def cb_client_menu(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    catalog: Catalog,
) -> None:
    if callback.message is None:
        await callback.answer("Сообщение недоступно", show_alert=True)
        return
    if _is_manager(
        settings,
        callback.from_user.id,
        callback.message.chat.id,
    ):
        text = await _manager_panel_text(db)
        keyboard = manager_panel_keyboard()
    elif _is_courier(
        settings,
        callback.from_user.id,
        callback.message.chat.id,
    ):
        list_text, keyboard = await _courier_page(db, 0)
        text = await _courier_panel_text(db) + "\n\n" + list_text
    else:
        text = START_TEXT
        usually = await db.usual_items(callback.from_user.id)
        keyboard = client_menu_keyboard(usually, catalog)
    await _edit_panel(
        callback.bot,
        callback.message.chat.id,
        callback.message.message_id,
        text,
        keyboard,
    )
    await callback.answer()


@router.callback_query(F.data == "cl:help")
async def cb_client_help(callback: CallbackQuery, settings: Settings) -> None:
    if callback.message is None:
        await callback.answer("Сообщение недоступно", show_alert=True)
        return
    if _is_manager(
        settings,
        callback.from_user.id,
        callback.message.chat.id,
    ):
        text = MANAGER_HELP_TEXT
        keyboard = manager_back_keyboard()
    elif _is_courier(
        settings,
        callback.from_user.id,
        callback.message.chat.id,
    ):
        text = COURIER_HELP_TEXT
        keyboard = courier_back_keyboard()
    else:
        text = HELP_TEXT
        keyboard = client_back_keyboard()
    await _edit_panel(
        callback.bot,
        callback.message.chat.id,
        callback.message.message_id,
        text,
        keyboard,
    )
    await callback.answer()
