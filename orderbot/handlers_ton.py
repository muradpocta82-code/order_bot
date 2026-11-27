"""Оплата USDT в сети TON на месте доставки (I1).

Вынесено из handlers.py: кнопка «Прибыл — USDT» у доставщика (кнопка cour:arrive:)
и ручная отметка оплаты командой /pay. Сами платежи и сверка — в tonflow.py.
Роутер подключается там же, где был блок: после приёма заказа, до отчётов.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from .config import Settings
from .db import Database
from .formatting import money
from .handlers_courier import _courier_ok, _courier_order_allowed
from .roles import _is_manager
from .tonpay import nano_to_text, usdt_to_nano

log = logging.getLogger(__name__)

router = Router(name="orderbot_ton")


# ---------------------------------------------------------------------------
# Оплата USDT в сети TON на месте доставки
# ---------------------------------------------------------------------------
from .tonflow import (
    _usdt_paid_notifications,
    poll_ton_payments,
    send_ton_invoice,
    ton_client,
)

@router.callback_query(F.data.startswith("cour:arrive:"))
async def cb_courier_arrive(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    """Доставщик нажал «Прибыл» — выставляем счёт USDT."""
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
    if order.get("payment_status") == "paid":
        await callback.answer("Заказ уже оплачен", show_alert=True)
        return
    if order.get("status") != "delivering":
        await callback.answer("Сначала нажмите «Выехал к клиенту»", show_alert=True)
        return
    if ton_client(settings) is None:
        await callback.answer("Оплата USDT не настроена", show_alert=True)
        return
    if await send_ton_invoice(callback.bot, db, settings, order):
        await callback.answer("Счёт отправлен")
    else:
        await callback.answer("Не удалось выставить счёт", show_alert=True)


@router.message(Command("pay"))
async def cmd_manual_payment(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Ручной зачёт USDT-платежа: /pay <номер заказа> <tx hash>."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[0].isdigit():
        await message.answer(
            "Формат: <code>/pay 12 tx_hash</code>\n"
            "Хеш транзакции — из обозревателя TON."
        )
        return
    order_id, tx_hash = int(parts[0]), parts[1].strip()
    order = await db.get_order(order_id)
    if order is None:
        await message.answer("Заказ не найден.")
        return
    if order.get("payment_status") == "paid":
        await message.answer("Заказ уже оплачен.")
        return
    if len(tx_hash) < 20:
        await message.answer("Хеш выглядит слишком коротким.")
        return
    if await db.ton_tx_used(tx_hash):
        await message.answer("Этот платёж уже засчитан.")
        return
    amount_nano = usdt_to_nano(float(order.get("total") or 0))
    await db.credit_ton_payment(order_id, tx_hash, amount_nano)
    if not await db.mark_order_paid(order_id, "usdt", amount_nano / 1_000_000):
        await message.answer(
            "Платёж записан, но статус заказа не изменился: он уже закрыт или оплачен."
        )
        return
    fresh = await db.get_order(order_id) or order
    await message.answer(
        f"✅ Заказ №{order_id}: платёж засчитан вручную ({nano_to_text(amount_nano)} USDT)."
    )
    await _usdt_paid_notifications(
        message.bot, db, settings, fresh, amount_nano, late=True
    )
