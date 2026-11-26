"""Приём от клиента: довес на сборке, адрес, телефон и геопин (I1).

Вынесено из handlers.py. Довес (G1) — упаковщик меняет итог заказа, клиент
подтверждает; PIN при вручении (E9), кнопки «телефон» и «адрес», геопин
сохраняются в карточку заказа. Подключается в handlers.py перед отчётами.
"""
from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from .catalog import Catalog
from .config import Settings
from .db import Database
from .formatting import esc, format_order, money, parse_amount
from .keyboards import contact_keyboard, location_keyboard, weigh_confirm_keyboard
from .onboarding import _ask_address, _finish_profile
from .roles import _is_courier, _is_manager, _manager_ok

log = logging.getLogger(__name__)

router = Router(name="orderbot_intake")

_parse_amount = parse_amount


# ---------------------------------------------------------------------------
# G1: довес на сборке
# ---------------------------------------------------------------------------


async def _handle_weigh(
    message: Message, text: str, db: Database, settings: Settings
) -> bool:
    """Ловит число, если упаковщик только что нажал «Довес»."""
    chat_id = message.chat.id
    state = "weigh"
    raw_order = await db.get_chat_state(chat_id, state)
    if not raw_order:
        return False
    await db.set_chat_state(chat_id, state, "")
    order = await db.get_order(int(raw_order))
    if order is None:
        return False
    amount = _parse_amount(text)
    if amount is None:
        await message.answer(
            "Не понял сумму. Пришлите число, например <code>1250.50</code>."
        )
        await db.set_chat_state(chat_id, state, str(raw_order))
        return True
    old_total = float(order.get("total") or 0)
    if abs(amount - old_total) < 0.005:
        await message.answer("Сумма не изменилась — оставил как было.")
        return True
    prev_total = await db.update_order_total(int(order["id"]), amount)
    if prev_total is None:
        await message.answer(
            f"Заказ №{order['id']} уже закрыт — сумму не меняю."
        )
        return True
    order["total"] = amount
    log.info(
        "Довес: заказ №%s %.2f -> %.2f (упаковщик id=%s)",
        order["id"],
        old_total,
        amount,
        message.from_user.id,
    )
    await message.answer(
        f"⚖️ Итог заказа №{order['id']} обновлён: "
        f"{money(old_total)} → <b>{money(amount)}</b>. Клиент получил запрос на подтверждение."
    )
    chef_id = order.get("chef_id")
    if chef_id:
        try:
            await message.bot.send_message(
                int(chef_id),
                f"⚖️ <b>Итог заказа №{order['id']} изменился</b>\n\n"
                f"Было: {money(old_total)}\n"
                f"Стало: <b>{money(amount)}</b>\n\n"
                "Причина — фактический вес при сборке. Подтвердите, пожалуйста.",
                reply_markup=weigh_confirm_keyboard(int(order["id"])),
            )
        except Exception:
            log.warning("Довес: не удалось уведомить клиента №%s", order["id"])
    manager = order.get("manager_chat_id") or settings.manager_chat_id
    if manager:
        try:
            await message.bot.send_message(
                int(manager),
                f"⚖️ Заказ №{order['id']}: довес {money(old_total)} → {money(amount)}, "
                "ждём подтверждения клиента.",
            )
        except Exception:
            pass
    return True


@router.callback_query(F.data.startswith("wgh:"))
async def cb_weigh(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    if not await _manager_ok(callback, settings):
        return
    try:
        order_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    order = await db.get_order(order_id)
    if order is None:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    if order.get("status") in ("done", "cancelled", "failed"):
        await callback.answer("Заказ уже закрыт", show_alert=True)
        return
    await db.set_chat_state(callback.message.chat.id, "weigh", str(order_id))
    await callback.message.answer(
        f"⚖️ Заказ №{order_id}. Текущий итог: {money(order.get('total'))}.\n"
        "Пришлите фактическую сумму числом, например <code>1250.50</code>."
    )
    await callback.answer("Жду сумму")


@router.callback_query(F.data.startswith("wacc:"))
async def cb_weigh_accept(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    order_id = int(callback.data.split(":")[1])
    order = await db.get_order(order_id)
    if order is None:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    manager = order.get("manager_chat_id") or settings.manager_chat_id
    if manager:
        try:
            await callback.bot.send_message(
                int(manager),
                f"✅ Клиент подтвердил довес по заказу №{order_id} "
                f"({money(order.get('total'))}).",
            )
        except Exception:
            pass
    await callback.answer("Спасибо, подтверждено")
    await callback.message.answer("✅ Спасибо, итог подтверждён.")


@router.callback_query(F.data.startswith("wdis:"))
async def cb_weigh_dispute(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    order_id = int(callback.data.split(":")[1])
    order = await db.get_order(order_id)
    if order is None:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    manager = order.get("manager_chat_id") or settings.manager_chat_id
    if manager:
        try:
            await callback.bot.send_message(
                int(manager),
                f"⚠️ Клиент НЕ согласен с довесом по заказу №{order_id} "
                f"({money(order.get('total'))}). Свяжитесь с клиентом.",
            )
        except Exception:
            pass
    await callback.answer("Передал упаковщику")
    await callback.message.answer(
        "Понял, передал упаковщику. Он свяжется с вами."
    )


# ---------------------------------------------------------------------------
# E9: PIN при вручении, E10: оценка, E6: телефон и адрес кнопками
# ---------------------------------------------------------------------------
async def _handle_delivery_pin(
    message: Message, text: str, db: Database, settings: Settings
) -> bool:
    """Курьер присылает код, который называет клиент."""
    chat_id = message.chat.id
    state = "pinwait"
    raw_order = await db.get_chat_state(chat_id, state)
    if not raw_order:
        return False
    if not _is_courier(settings, message.from_user.id, message.chat.id):
        return False
    order = await db.get_order(int(raw_order))
    if order is None:
        await db.set_chat_state(chat_id, state, "")
        return False
    digits = re.sub(r"\D", "", text or "")
    if digits != str(order.get("delivery_pin") or ""):
        await message.answer(
            "Код не совпал. Спросите у клиента ещё раз — или нажмите «Доставлено» заново."
        )
        return True
    await db.set_chat_state(chat_id, state, "")
    await db.set_chat_state(order['id'], "pinok", "1")
    await message.answer("✅ Код верный. Нажмите «Доставлено».")
    return True


@router.message(F.contact)
async def on_contact(
    message: Message, db: Database, settings: Settings, catalog: Catalog
) -> None:
    state = await db.get_chat_state(message.chat.id, "onboard")
    if state != "phone" or message.contact is None:
        return
    if message.contact.user_id and message.contact.user_id != message.from_user.id:
        await message.answer("Нужен ваш собственный номер — пришлите свою карточку.")
        return
    phone = message.contact.phone_number
    await db.ensure_user(
        message.from_user.id, message.from_user.full_name, message.from_user.username
    )
    await db.set_user_phone(message.from_user.id, phone)
    await message.answer(f"📞 Записал: {phone}")
    await _ask_address(message, db)


@router.message(F.location)
async def on_location(
    message: Message, db: Database, settings: Settings, catalog: Catalog
) -> None:
    if message.location is None:
        return
    state = await db.get_chat_state(message.chat.id, "onboard")
    address = f"📍 {message.location.latitude:.6f}, {message.location.longitude:.6f}"
    await db.ensure_user(
        message.from_user.id, message.from_user.full_name, message.from_user.username
    )
    await db.set_user_address(message.from_user.id, address)
    await db.set_user_coords(
        message.from_user.id,
        message.location.latitude,
        message.location.longitude,
    )
    # Одна точка — геопин уточняет и её: курьер поедет по координатам.
    spots = await db.client_locations(message.from_user.id)
    if len(spots) == 1:
        await db.set_location_coords(
            int(spots[0]["id"]),
            message.location.latitude,
            message.location.longitude,
        )
    if state != "address":
        await message.answer(
            "📍 Геопин записан: теперь окно прибытия курьера будет точнее."
        )
        return
    user = await db.get_user(message.from_user.id) or {}
    await _finish_profile(message, db, settings, catalog, user)

