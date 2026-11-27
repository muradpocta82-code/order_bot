"""Статусы заказа и списки доставок: кнопки ost: и cd: (I1).

Вынесено из handlers.py: упаковщик ведёт заказ по статусам (принят, собран,
передан доставщику), открывает список активных доставок и карточку заказа.
Роутер подключается в handlers.py перед панелью доставщика.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from .config import Settings
from .db import Database
from .formatting import esc, format_order, money
from .handlers_commands import _courier_page
from .handlers_courier import (
    _courier_ok,
    _courier_order_allowed,
    _edit_courier_card,
)
from .keyboards import (
    courier_panel_keyboard,
    courier_status_keyboard,
    manager_status_keyboard,
)
from .notify import (
    _close_order_cards,
    _courier_chat,
    _detach_card,
    _drop_card_buttons,
    _manager_chat,
    _send_order_card,
    _send_with_retry,
)
from .roles import _is_courier, _is_manager, _manager_ok

log = logging.getLogger(__name__)

router = Router(name="orderbot_status")


@router.callback_query(F.data.startswith("ost:"))
async def cb_order_status(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    try:
        _, order_raw, status = callback.data.split(":")
        order_id = int(order_raw)
    except (ValueError, AttributeError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    if status not in ("accepted", "ready", "cancelled"):
        await callback.answer(
            "Доставку подтверждает доставщик, а не упаковщик.",
            show_alert=True,
        )
        return
    if not await _manager_ok(callback, settings):
        return
    order = await db.get_order(order_id)
    if order is None:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    if order.get("status") in ("done", "cancelled", "failed"):
        await callback.answer(
            "Заказ уже закрыт. Статус закрытого заказа не меняется.",
            show_alert=True,
        )
        return

    chef_id = order.get("chef_id")
    manager = _manager_chat(settings)

    if status == "cancelled":
        if not await db.update_order_status(order_id, "cancelled"):
            await callback.answer("Статус уже установлен", show_alert=True)
            return
        log.info(
            "Заказ №%s: отменён упаковщиком id=%s",
            order_id,
            callback.from_user.id,
        )
        order["status"] = "cancelled"
        await _close_order_cards(
            callback.bot, order, f"❌ Заказ №{order_id} отменён", manager
        )
        await callback.answer("Отменил")
        return

    if status == "accepted":
        if order.get("status") not in ("new", "accepted"):
            await callback.answer("Заказ уже передан дальше", show_alert=True)
            return
        changed = await db.transition_order(
            order_id, ("new", "accepted"), "accepted"
        )
        if not changed and order.get("status") != "accepted":
            await callback.answer("Статус уже изменён", show_alert=True)
            return
        order["status"] = "accepted"
        try:
            await callback.message.edit_text(
                format_order(order, "📦 Заказ"),
                reply_markup=manager_status_keyboard(order_id, "accepted"),
            )
        except TelegramBadRequest:
            pass
        if chef_id and chef_id != manager:
            try:
                await _send_with_retry(
                    lambda: callback.bot.send_message(
                        chef_id, f"📌 Заказ №{order_id} принят в упаковку."
                    )
                )
            except Exception:
                log.warning("Не удалось уведомить клиента о заказе №%s", order_id)
        await callback.answer("Принял")
        return

    # status == "ready": упаковщик собрал заказ и передаёт доставщику.
    courier = _courier_chat(settings)
    if courier is None:
        await callback.answer(
            "COURIER_CHAT_ID не задан в .env — доставщика нет.", show_alert=True
        )
        return
    if order.get("status") == "ready" and order.get("courier_notified_at"):
        await callback.answer("Заказ уже у доставщика", show_alert=True)
        return
    if order.get("status") not in ("new", "accepted", "ready"):
        await callback.answer("Заказ уже нельзя передать доставщику", show_alert=True)
        return

    if not await db.mark_order_ready(order_id, courier):
        fresh = await db.get_order(order_id)
        if fresh is None or fresh.get("status") != "ready":
            await callback.answer("Статус уже изменён", show_alert=True)
            return
    order = await db.get_order(order_id)
    assert order is not None
    if order.get("chef_id"):
        profile = await db.get_user(int(order["chef_id"]))
        if profile and profile.get("venue"):
            order["venue"] = profile["venue"]
    try:
        card_id = await _send_order_card(
            callback.bot,
            courier,
            order,
            "🚚 Доставка",
            courier_status_keyboard(
                order_id, "ready", str(order.get("payment_status") or "unpaid")
            ),
        )
    except Exception:
        log.exception("Не удалось отправить заказ №%s доставщику", order_id)
        await callback.answer(
            "Заказ собран, но доставщик недоступен. Бот повторит отправку.",
            show_alert=True,
        )
        return
    try:
        await db.mark_courier_notified(order_id)
    except Exception:
        log.warning("Не удалось отметить отправку заказа №%s доставщику", order_id)
    if card_id:
        try:
            await db.set_order_cards(order_id, courier_card=card_id)
        except Exception:
            log.warning("Не удалось сохранить карточку доставки №%s", order_id)

    try:
        await callback.message.edit_text(
            format_order(order, "📦 Заказ"),
            reply_markup=manager_status_keyboard(order_id, "ready"),
        )
    except TelegramBadRequest:
        pass
    if chef_id:
        try:
            await _send_with_retry(
                lambda: callback.bot.send_message(
                    chef_id,
                    f"📦 Заказ №{order_id} собран и передан доставщику.",
                )
            )
        except Exception:
            log.warning("Не удалось уведомить клиента о сборке №%s", order_id)
    log.info(
        "Заказ №%s: собран, передан доставщику %s (упаковщик id=%s)",
        order_id,
        courier,
        callback.from_user.id,
    )
    await callback.answer("Передал доставщику")


@router.callback_query(F.data.startswith("cd:list:"))
async def cb_courier_list(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    if not await _courier_ok(callback, settings):
        return
    try:
        page = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        page = 0
    text, keyboard = await _courier_page(db, page)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("cd:view:"))
async def cb_courier_view(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    if not await _courier_ok(callback, settings):
        return
    try:
        _, _, order_raw, page_raw = callback.data.split(":")
        order_id = int(order_raw)
        page = int(page_raw)
    except (ValueError, AttributeError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    order = await db.get_order(order_id)
    if order is None:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    if not _courier_order_allowed(order, callback.from_user.id):
        await callback.answer("Этот заказ у другого доставщика", show_alert=True)
        return
    if order.get("status") not in ("ready", "delivering"):
        await callback.answer("Заказ уже закрыт", show_alert=True)
        return
    await _edit_courier_card(
        callback,
        order,
        "🚚 Доставка",
        courier_status_keyboard(
            order_id,
            str(order.get("status")),
            str(order.get("payment_status") or "unpaid"),
        ),
    )
    await callback.answer()
