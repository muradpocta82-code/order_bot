"""Оценки, опрос после доставки и курс местной валюты (I1).

Вынесено из handlers.py: обработчики не связаны с потоком заказа, поэтому
живут отдельным роутером. Подключается в handlers.py перед handlers_tail.py.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from .config import Settings
from .db import Database
from .formatting import LOCAL_CURRENCY, esc, set_local_rate
from .keyboards import (
    claim_actions_keyboard,
    problem_keyboard,
    rating_reason_keyboard,
    rating_reason_label,
)
from .roles import _is_manager
from .survey import (
    claim_text as survey_claim_text,
    details_prompt as survey_details_prompt,
    problem_label as survey_problem_label,
    problem_prompt as survey_problem_prompt,
    thanks_text as survey_thanks,
)

log = logging.getLogger(__name__)

router = Router(name="orderbot_feedback")

@router.callback_query(F.data.startswith("rrate:"))
async def cb_rate_reason(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    """Причина низкой оценки: пишется в историю заказа и уходит упаковщику."""
    try:
        _, _tag, raw_order, code = callback.data.split(":")
        order_id = int(raw_order)
    except (IndexError, ValueError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    label = rating_reason_label(code)
    await db.log_event(
        order_id,
        "rating_reason",
        {"reason": code, "label": label},
        actor_id=callback.from_user.id,
    )
    try:
        await callback.message.edit_text(f"Спасибо, передали: {label}.")
    except TelegramBadRequest:
        pass
    manager = settings.manager_chat_id
    if manager:
        try:
            await callback.bot.send_message(
                int(manager),
                f"⚠️ Заказ №{order_id}: клиент недоволен — {label}. "
                "Стоит позвонить и разобраться.",
            )
        except Exception:
            pass
    await callback.answer("Записали")

@router.callback_query(F.data.startswith("rate:"))
async def cb_rate(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    try:
        _, raw_order, raw_value = callback.data.split(":")
        order_id, value = int(raw_order), int(raw_value)
    except (IndexError, ValueError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    if not 1 <= value <= 5:
        await callback.answer("Оценка от 1 до 5", show_alert=True)
        return
    await db.set_order_rating(order_id, value)
    if value <= 3:
        try:
            await callback.message.edit_text(
                f"Спасибо за оценку: {'⭐' * value}. Что было не так?",
                reply_markup=rating_reason_keyboard(order_id),
            )
        except TelegramBadRequest:
            pass
        await callback.answer()
        return
    try:
        await callback.message.edit_text(f"Спасибо за оценку: {'⭐' * value}")
    except TelegramBadRequest:
        pass
    manager = settings.manager_chat_id
    if manager:
        try:
            await callback.bot.send_message(
                int(manager), f"⭐ Заказ №{order_id}: оценка клиента {value}/5"
            )
        except Exception:
            log.debug("Не удалось отправить оценку менеджеру")
    await callback.answer("Спасибо!")

@router.message(Command("rate"))
async def cmd_rate(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Курс местной валюты: /rate 3.5 — или /rate 0, чтобы выключить."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    raw = (command.args or "").replace(",", ".").strip()
    if not raw:
        current = await db.usd_rate()
        lines = [
            f"Курс: <b>{current:g}</b> {LOCAL_CURRENCY} за 1 USD."
            if current > 0
            else "Курс не задан: суммы в сумах не показываются.",
            f"Изменить: <code>/rate 12650</code>, выключить: <code>/rate 0</code>.",
            "Курс заказа фиксируется при подтверждении — старые суммы не меняются.",
        ]
        history = await db.rate_history(5)
        if history:
            lines.append("")
            lines.append("<b>Последние смены</b>")
            for row in history:
                stamp = str(row.get("ts"))[:16]
                lines.append(f"• {stamp} — {float(row.get('rate') or 0):g}")
        await message.answer("\n".join(lines))
        return
    try:
        rate = float(raw)
    except ValueError:
        await message.answer("Нужно число, например <code>/rate 3.5</code>.")
        return
    if rate < 0 or rate > 100000:
        await message.answer("Курс вне разумных пределов.")
        return
    await db.set_usd_rate(rate)
    set_local_rate(rate, LOCAL_CURRENCY)
    await message.answer(
        "Курс выключен." if rate <= 0 else f"Курс обновлён: 1 USD = {rate} {LOCAL_CURRENCY}."
    )

@router.callback_query(F.data.startswith("prob:"))
async def cb_problem(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    try:
        order_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    await db.set_chat_state(callback.message.chat.id, "claim", str(order_id))
    await callback.answer()
    await callback.message.answer(
        "⚠️ Опишите проблему одним сообщением: что не так с заказом. "
        "Мы свяжемся и решим."
    )

@router.callback_query(F.data.startswith("srv:"))
async def cb_survey(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    """Ответ на опрос через сутки (C5)."""
    try:
        _, action, raw_id = callback.data.split(":")
        order_id = int(raw_id)
    except (ValueError, AttributeError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    order = await db.get_order(order_id)
    if order is None:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    if str(order.get("chef_id") or "") != str(callback.from_user.id):
        await callback.answer("Это опрос другого клиента", show_alert=True)
        return
    if action == "ok":
        await callback.answer()
        try:
            await callback.message.edit_text(survey_thanks(), reply_markup=None)
        except TelegramBadRequest:
            pass
        await db.set_setting(f"survey_ok:{order_id}", "1")
        return
    await db.set_chat_state(callback.message.chat.id, "claim_problem", str(order_id))
    await callback.answer()
    try:
        await callback.message.edit_text(
            survey_problem_prompt(order_id), reply_markup=problem_keyboard(order_id)
        )
    except TelegramBadRequest:
        await callback.message.answer(
            survey_problem_prompt(order_id), reply_markup=problem_keyboard(order_id)
        )

@router.callback_query(F.data.startswith("srvp:"))
async def cb_survey_problem(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    """Клиент выбрал тип проблемы — создаём претензию и просим детали (C5)."""
    try:
        _, raw_id, code = callback.data.split(":", 2)
        order_id = int(raw_id)
    except (ValueError, AttributeError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    order = await db.get_order(order_id)
    if order is None or str(order.get("chef_id") or "") != str(callback.from_user.id):
        await callback.answer("Заказ не найден", show_alert=True)
        return
    label = survey_problem_label(code)
    claim_id = await db.add_claim(
        order_id, callback.from_user.id, survey_claim_text(label, "")
    )
    await db.set_chat_state(callback.message.chat.id, "claim", str(order_id))
    await db.set_chat_state(callback.message.chat.id, "claim_id", str(claim_id))
    await callback.answer()
    try:
        await callback.message.edit_text(survey_details_prompt(label), reply_markup=None)
    except TelegramBadRequest:
        await callback.message.answer(survey_details_prompt(label))
    manager = order.get("manager_chat_id") or settings.manager_chat_id
    if manager:
        try:
            await callback.bot.send_message(
                int(manager),
                f"⚠️ Претензия №{claim_id} по заказу №{order_id} "
                f"от {esc(order.get('chef_name') or '')}: {label}. Ждём описание.",
                reply_markup=claim_actions_keyboard(claim_id),
            )
        except Exception:
            pass
