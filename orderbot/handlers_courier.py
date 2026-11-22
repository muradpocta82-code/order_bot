"""Панель доставщика: взял заказ, оплата, доставлено, сбой.

Вынесено из handlers.py. Роутер подключается после основных обработчиков,
поэтому порядок «конкретное раньше общего» сохраняется.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from .catalog import Catalog
from .config import Settings
from .eta import SPREAD_MIN as eta_spread
from .eta import client_text as eta_client_text
from .eta import travel_minutes as eta_travel
from .eta import order_minutes as eta_order_minutes
from .eta import window_text as eta_window
from .stock import consume_order
from .db import Database
from .formatting import esc, format_order, money
from .route import maps_link, plan as route_plan, total_distance_km
from .keyboards import (
    failed_move_keyboard,
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


@router.callback_query(F.data.startswith("cour:card:"))
async def cb_courier_card(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    """«Назад» из причин недоставки и переноса — вернуть карточку заказа (A11)."""
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
    status = str(order.get("status") or "ready")
    title = "✅ Доставлено" if status == "done" else "🚚 Доставка"
    await _edit_courier_card(
        callback,
        order,
        title,
        courier_status_keyboard(
            order_id, status, str(order.get("payment_status") or "unpaid")
        ),
    )
    await callback.answer()


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
            minutes = eta_travel(await db.get_setting("courier_travel_min", ""))
            depot = _depot_coords(await db.get_setting("depot_point", ""))
            minutes = eta_order_minutes(
                order,
                depot,
                minutes,
                _speed(await db.get_setting("courier_speed_kmh", "")),
            )
            now = datetime.now(settings.timezone)
            message_text = eta_client_text(
                order_id, now, minutes, str(order.get("delivery_time") or "")
            )
            await _send_with_retry(
                lambda: callback.bot.send_message(int(order["chef_id"]), message_text)
            )
        except Exception:
            log.warning("Не удалось уведомить клиента о доставке №%s", order_id)
    await callback.answer("В доставке")


def _depot_coords(raw: str | None) -> tuple[float, float] | None:
    """Координаты склада из настройки «41.31, 69.24»."""
    from .route import parse_coords

    return parse_coords(f"📍 {raw}" if raw else None)


def _speed(raw: str | None) -> float:
    """Скорость курьера по городу, км/ч (по умолчанию 25)."""
    try:
        value = float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return 25.0
    return max(5.0, min(80.0, value))


@router.message(Command("depot"))
async def cmd_depot(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Координаты склада для окна прибытия (C12): /depot 41.311081, 69.240562."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    raw = (command.args or "").strip()
    if not raw:
        point = _depot_coords(await db.get_setting("depot_point", ""))
        speed = _speed(await db.get_setting("courier_speed_kmh", ""))
        if point:
            await message.answer(
                f"🏭 Склад: <code>{point[0]:.6f}, {point[1]:.6f}</code>, "
                f"скорость курьера {speed:g} км/ч.\n"
                "Изменить: <code>/depot 41.311081, 69.240562</code>"
            )
        else:
            await message.answer(
                "Координаты склада не заданы. Формат:\n"
                "<code>/depot 41.311081, 69.240562</code>"
            )
        return
    point = _depot_coords(raw)
    if point is None:
        await message.answer(
            "Не понял координаты. Формат: <code>/depot 41.311081, 69.240562</code>"
        )
        return
    await db.set_setting("depot_point", f"{point[0]:.6f}, {point[1]:.6f}")
    await message.answer(
        f"🏭 Склад сохранён: <code>{point[0]:.6f}, {point[1]:.6f}</code>.\n"
        "Теперь окно прибытия считается по расстоянию, если у клиента есть геопин."
    )


@router.message(Command("travel"))
async def cmd_travel(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Сколько минут курьер обычно едет до клиента (C12): /travel 25."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    raw = (command.args or "").strip()
    if not raw:
        minutes = eta_travel(await db.get_setting("courier_travel_min", ""))
        await message.answer(
            f"⏱ Обычное время в пути: {minutes} мин (разброс ±{eta_spread()} мин).\n"
            "Изменить: <code>/travel 25</code>"
        )
        return
    minutes = eta_travel(raw)
    await db.set_setting("courier_travel_min", str(minutes))
    await message.answer(
        f"⏱ Время в пути: {minutes} мин. Клиент при «Выехал» увидит: "
        f"«курьер будет {eta_window(datetime.now(settings.timezone), minutes)}»."
    )


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
    callback: CallbackQuery, db: Database, settings: Settings, catalog: Catalog
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
    try:
        consumed, stock_notes = await consume_order(db, catalog, order)
        if consumed:
            log.info(
                "Остатки по заказу №%s списаны: %s поз. (%s)",
                order_id,
                consumed,
                "; ".join(stock_notes[:4]),
            )
    except Exception as exc:
        log.warning("Не удалось списать остатки по заказу №%s: %s", order_id, exc)
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
        recipients: list[int] = []
        for chat in (order.get("chef_id"), manager):
            if chat:
                recipients.append(int(chat))
        # C11: у точки может быть свой плательщик — тогда накладная идёт ему.
        spot_payer = await db.location_payer(order.get("location_id"))
        if spot_payer:
            recipients.append(int(spot_payer))
        elif order.get("chef_id"):
            try:
                recipients.extend(await db.payers_for(int(order["chef_id"])))
            except Exception as exc:
                log.info("Не нашёл плательщиков заказа №%s: %s", order_id, exc)
        for chat in list(dict.fromkeys(recipients)):
            await callback.bot.send_document(
                chat,
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


def _move_dates() -> list[tuple[str, str]]:
    """Три ближайших дня для переноса: завтра, послезавтра, через два дня."""
    from datetime import datetime, timedelta

    today = datetime.now().date()
    labels = ("завтра", "послезавтра", "+2 дня")
    return [
        (label, (today + timedelta(days=offset)).isoformat())
        for offset, label in zip((1, 2, 3), labels)
    ]


@router.callback_query(F.data.startswith("move:"))
async def cb_move_order(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    """Перенос недоставленного заказа на другую дату (D7)."""
    try:
        _, raw_order, new_date = callback.data.split(":")
        order_id = int(raw_order)
    except (IndexError, ValueError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    order = await db.get_order(order_id)
    if order is None:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    manager = _manager_chat(settings)
    allowed = callback.from_user.id == (settings.courier_chat_id or 0) or (
        manager is not None and callback.from_user.id == manager
    )
    if not allowed:
        await callback.answer("Переносить может курьер или менеджер", show_alert=True)
        return
    if not await db.reschedule_order(order_id, new_date):
        await callback.answer("Заказ уже закрыт — перенести нельзя", show_alert=True)
        return
    await db.log_event(
        order_id,
        "rescheduled",
        {"date": new_date, "was": order.get("delivery_date"), "status": order.get("status")},
        actor_id=callback.from_user.id,
    )
    fresh = await db.get_order(order_id)
    pretty = new_date[8:10] + "." + new_date[5:7]
    if fresh is not None:
        await _edit_courier_card(callback, fresh, f"📅 Перенесён на {pretty}", None)
    await callback.answer(f"Перенесён на {pretty}")
    if manager:
        try:
            await callback.bot.send_message(
                manager,
                f"📅 Заказ №{order_id} перенесён на {pretty} и вернулся в очередь.",
            )
        except Exception:
            log.debug("Не удалось уведомить менеджера о переносе №%s", order_id)
    if fresh is not None and fresh.get("chef_id"):
        try:
            await callback.bot.send_message(
                int(fresh["chef_id"]),
                f"📅 Заказ №{order_id} перенесён на {pretty}.",
            )
        except Exception:
            log.debug("Не удалось уведомить клиента о переносе №%s", order_id)


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
    await _edit_courier_card(
        callback,
        order,
        f"⚠️ Не доставлено: {reason_label(code)}\n"
        "<i>Можно сразу перенести на другой день.</i>",
        failed_move_keyboard(order_id, _move_dates()),
    )
    manager = _manager_chat(settings)
    await _drop_card_buttons(
        callback.bot, manager, order.get("manager_card_message_id")
    )
    if manager:
        try:
            await callback.bot.send_message(
                manager,
                f"⚠️ Заказ №{order_id} не доставлен. Причина: {reason_label(code)}.",
                reply_markup=failed_move_keyboard(order_id, _move_dates()),
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


@router.message(Command("route"))
async def cmd_route(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Маршрут на день: порядок объезда и ссылка в карты (D5)."""
    courier_id = settings.courier_chat_id
    is_courier = courier_id is not None and message.from_user.id == courier_id
    if not is_courier and not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    from datetime import datetime

    raw = (command.args or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        day = raw
    elif re.fullmatch(r"\d{2}\.\d{2}", raw):
        now = datetime.now(settings.timezone)
        day = f"{now.year}-{raw[3:5]}-{raw[0:2]}"
    else:
        day = datetime.now(settings.timezone).date().isoformat()

    orders = [
        order
        for order in await db.orders_for_date(day)
        if str(order.get("status")) in ("ready", "delivering")
    ]
    if not orders:
        await message.answer(f"📭 На {day[8:10]}.{day[5:7]} доставок нет.")
        return

    planned = route_plan(orders)
    lines = [f"🗺 <b>Маршрут на {day[8:10]}.{day[5:7]}</b>", ""]
    missing = 0
    for number, order in enumerate(planned, start=1):
        name = order.get("chef_name") or order.get("chef_id") or "клиент"
        slot = order.get("delivery_time") or "без слота"
        address = str(order.get("delivery_address") or "")
        if not address.strip().startswith("📍"):
            address = (address or "адрес не указан")[:60]
            missing += 1
        lines.append(f"{number}. №{order.get('id')} {esc(str(name))} · {esc(str(slot))}")
        lines.append(f"    {esc(address)}")
    distance = total_distance_km(planned)
    lines.append("")
    lines.append(f"Точек: <b>{len(planned)}</b> · по прямой <b>{distance:.1f} км</b>")
    if missing:
        lines.append(
            f"⚠️ У {missing} точек нет геопина — порядок для них примерный. "
            "Попросите повара отправить геопозицию."
        )
    link = maps_link(planned)
    if link:
        lines.append("")
        lines.append(f'<a href="{link}">Весь маршрут в Яндекс.Картах</a>')
    await message.answer("\n".join(lines), disable_web_page_preview=True)
