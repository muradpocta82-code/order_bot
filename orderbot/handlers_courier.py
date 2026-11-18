"""Панель доставщика: взял заказ, оплата, доставлено, сбой.

Вынесено из handlers.py. Роутер подключается после основных обработчиков,
поэтому порядок «конкретное раньше общего» сохраняется.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .config import Settings
from .db import Database
from .formatting import esc, format_order, money
from .keyboards import (
    client_order_keyboard,
    courier_back_keyboard,
    courier_panel_keyboard,
    courier_status_keyboard,
    manager_status_keyboard,
)
from .keyboards import fail_reason_keyboard, rating_keyboard, reason_label
from .formatting import format_client_order
from .notify import (
    _close_order_cards,
    _detach_card,
    _drop_card_buttons,
    _manager_chat,
    _report_failure,
    _send_order_card,
    _send_with_retry,
)
from .roles import _is_courier, _is_manager, _manager_ok
from .tonflow import send_ton_invoice

async def _courier_ok(callback: CallbackQuery, settings: Settings) -> bool:
    if callback.message is None:
        await callback.answer("Сообщение недоступно", show_alert=True)
        return False
    if not _is_courier(
        settings, callback.from_user.id, callback.message.chat.id
    ):
        await callback.answer("Недостаточно прав", show_alert=True)
        return False
    return True


def _courier_order_allowed(order: dict, courier_id: int) -> bool:
    assigned = order.get("courier_chat_id")
    return assigned is None or int(assigned) == int(courier_id)


async def _edit_courier_card(
    callback: CallbackQuery, order: dict, title: str, keyboard=None
) -> None:
    try:
        await callback.message.edit_text(
            format_order(order, title), reply_markup=keyboard
        )
    except TelegramBadRequest:
        pass


log = logging.getLogger(__name__)
router = Router(name="orderbot_courier")


@router.callback_query(F.data.startswith("cour:start:"))
async def cb_courier_start(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    if not await _courier_ok(callback, settings):
        return
    try:
        order_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    order = await db.get_order(order_id)
    if order is None or not _courier_order_allowed(order, callback.from_user.id):
        await callback.answer("Заказ не найден", show_alert=True)
        return
    if order.get("status") != "ready":
        await callback.answer("Заказ уже в доставке или закрыт", show_alert=True)
        return
    if not await db.mark_out_for_delivery(order_id):
        await callback.answer("Статус уже изменён", show_alert=True)
        return
    order = await db.get_order(order_id)
    assert order is not None
    await _edit_courier_card(
        callback,
        order,
        "🚚 Доставка",
        courier_status_keyboard(
            order_id, "delivering", str(order.get("payment_status") or "unpaid")
        ),
    )
    if order.get("chef_id"):
        try:
            await _send_with_retry(
                lambda: callback.bot.send_message(
                    int(order["chef_id"]),
                    f"🚚 Заказ №{order_id} в пути. Доставщик свяжется с вами.",
                )
            )
        except Exception:
            log.warning("Не удалось уведомить клиента о доставке №%s", order_id)
    await callback.answer("В доставке")


@router.callback_query(F.data.startswith("cour:pay:"))
async def cb_courier_pay(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    if not await _courier_ok(callback, settings):
        return
    try:
        _, _, order_raw, method = callback.data.split(":")
        order_id = int(order_raw)
    except (ValueError, AttributeError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    if method != "cash":
        await callback.answer("Доступна только оплата наличными", show_alert=True)
        return
    order = await db.get_order(order_id)
    if order is None or not _courier_order_allowed(order, callback.from_user.id):
        await callback.answer("Заказ не найден", show_alert=True)
        return
    if order.get("status") not in ("ready", "delivering"):
        await callback.answer("Заказ уже закрыт", show_alert=True)
        return
    if order.get("payment_status") == "paid":
        await callback.answer("Оплата уже отмечена", show_alert=True)
        return
    if method == "cash":
        await db.set_chat_state(callback.message.chat.id, "cashwait", str(order_id))
        await callback.message.answer(
            f"💵 Заказ №{order_id}. Сколько наличных получили? "
            f"Пришлите сумму числом (по заказу {money(order.get('total'))})."
        )
        await callback.answer("Жду сумму")
        return
    if not await db.mark_order_paid(order_id, method, float(order.get("total") or 0)):
        await callback.answer("Не удалось отметить оплату", show_alert=True)
        return
    order = await db.get_order(order_id)
    assert order is not None
    label = "наличными"
    await _edit_courier_card(
        callback,
        order,
        "🚚 Доставка",
        courier_status_keyboard(
            order_id, str(order.get("status")), "paid"
        ),
    )
    if order.get("chef_id"):
        try:
            await _send_with_retry(
                lambda: callback.bot.send_message(
                    int(order["chef_id"]),
                    f"💳 Заказ №{order_id}: оплата {label} получена "
                    f"({money(order.get('total'))}).",
                )
            )
        except Exception:
            log.warning("Не удалось уведомить клиента об оплате №%s", order_id)
    log.info(
        "Заказ №%s: оплата %s, сумма %s (доставщик id=%s)",
        order_id,
        method,
        order.get("total"),
        callback.from_user.id,
    )
    await callback.answer("Оплата зафиксирована")


@router.callback_query(F.data.startswith("cour:done:"))
async def cb_courier_done(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    if not await _courier_ok(callback, settings):
        return
    try:
        order_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    order = await db.get_order(order_id)
    if order is None or not _courier_order_allowed(order, callback.from_user.id):
        await callback.answer("Заказ не найден", show_alert=True)
        return
    if order.get("payment_status") != "paid":
        await callback.answer(
            "Сначала отметьте оплату наличными.", show_alert=True
        )
        return
    if (order.get("delivery_pin") or "").strip() and (
        await db.get_chat_state(order_id, "pinok") != "1"
    ):
        await db.set_chat_state(callback.message.chat.id, "pinwait", str(order_id))
        await callback.message.answer(
            "🔑 Спросите у клиента код получения (4 цифры) и пришлите его сюда."
        )
        await callback.answer("Нужен код")
        return
    if not await db.mark_order_delivered(order_id):
        await callback.answer("Заказ уже закрыт или оплата не отмечена", show_alert=True)
        return
    order = await db.get_order(order_id)
    assert order is not None
    await _edit_courier_card(callback, order, "✅ Доставлено", None)
    manager = _manager_chat(settings)
    await _drop_card_buttons(
        callback.bot, manager, order.get("manager_card_message_id")
    )
    await _drop_card_buttons(
        callback.bot, order.get("chef_id"), order.get("chef_card_message_id")
    )
    if order.get("chef_id"):
        try:
            await _send_with_retry(
                lambda: callback.bot.send_message(
                    int(order["chef_id"]),
                    format_client_order(order, "✅ Заказ доставлен")
                    + "\n\nСпасибо!",
                )
            )
            await callback.bot.send_message(
                int(order["chef_id"]),
                "Оцените заказ — это помогает нам готовить лучше:",
                reply_markup=rating_keyboard(order_id),
            )
        except Exception:
            log.warning("Не удалось уведомить клиента о доставке №%s", order_id)
    if manager:
        try:
            await callback.bot.send_message(
                manager, f"✅ Заказ №{order_id} доставлен и оплачен."
            )
        except Exception:
            log.debug("Не удалось уведомить упаковщика о доставке №%s", order_id)
    try:
        from .invoice import build_invoice
        from aiogram.types import FSInputFile

        pdf_path = settings.data_dir / f"invoice_{order_id}.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        build_invoice(order, pdf_path)
        for chat in (order.get("chef_id"), manager):
            if chat:
                await callback.bot.send_document(
                    int(chat),
                    FSInputFile(str(pdf_path)),
                    caption=f"Накладная №{order_id}",
                )
    except Exception:
        log.warning("Не удалось отправить накладную №%s", order_id, exc_info=True)
    log.info(
        "Заказ №%s: доставлен и оплачен (доставщик id=%s)",
        order_id,
        callback.from_user.id,
    )
    await callback.answer("Доставлено")


@router.callback_query(F.data.startswith("cour:fail:"))
async def cb_courier_fail(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    if not await _courier_ok(callback, settings):
        return
    try:
        order_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    order = await db.get_order(order_id)
    if order is None or not _courier_order_allowed(order, callback.from_user.id):
        await callback.answer("Заказ не найден", show_alert=True)
        return
    try:
        await callback.message.edit_reply_markup(
            reply_markup=fail_reason_keyboard(order_id)
        )
    except (TelegramBadRequest, AttributeError):
        pass
    await callback.answer("Выберите причину")
    return


@router.callback_query(F.data.startswith("failr:"))
async def cb_courier_fail_reason(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    """Недоставка с причиной: причина попадает в историю заказа."""
    if not await _courier_ok(callback, settings):
        return
    try:
        _, _tag, raw_order, code = callback.data.split(":")
        order_id = int(raw_order)
    except (IndexError, ValueError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    order = await db.get_order(order_id)
    if order is None or not _courier_order_allowed(order, callback.from_user.id):
        await callback.answer("Заказ не найден", show_alert=True)
        return
    if not await db.mark_order_failed(order_id):
        await callback.answer("Заказ уже закрыт", show_alert=True)
        return
    await db.log_event(
        order_id, "failed", {"reason": code, "label": reason_label(code)}, actor_id=callback.from_user.id
    )
    order = await db.get_order(order_id)
    assert order is not None
    await _edit_courier_card(callback, order, f"⚠️ Не доставлено: {reason_label(code)}", None)
    manager = _manager_chat(settings)
    await _drop_card_buttons(
        callback.bot, manager, order.get("manager_card_message_id")
    )
    if manager:
        try:
            await callback.bot.send_message(
                manager,
                f"⚠️ Заказ №{order_id} не доставлен. Причина: {reason_label(code)}.",
            )
        except Exception:
            log.debug("Не удалось уведомить упаковщика о сбое №%s", order_id)
    if order.get("chef_id"):
        try:
            await callback.bot.send_message(
                int(order["chef_id"]),
                f"⚠️ Заказ №{order_id} не удалось доставить. "
                "Упаковщик свяжется с вами.",
            )
        except Exception:
            log.warning("Не удалось уведомить клиента о сбое №%s", order_id)
    await callback.answer("Отмечено")


@router.message(Command("handover"))
async def cmd_handover(message: Message, db: Database, settings: Settings) -> None:
    """Сдача кассы: курьер жмёт команду и вводит сумму наличных за день."""
    if not (_is_courier(settings, message.from_user.id, message.chat.id) or _is_manager(
        settings, message.from_user.id, message.chat.id
    )):
        return
    day = datetime.now(settings.timezone).date().isoformat()
    report = await db.cash_report(day)
    await db.set_chat_state(message.chat.id, "cash_handover", day)
    await message.answer(
        f"💵 Сдача кассы за {day}.\n"
        f"По заказам наличными ожидается: <b>{money(report['expected'])}</b> "
        f"({len(report['orders'])} заказ(ов)).\n"
        "Пришлите сумму, которую сдаёте (числом). Отмена: /cancel"
    )
