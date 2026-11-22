"""Telegram-хендлеры: заказ, уточнения, подтверждение, менеджер."""
from __future__ import annotations

import asyncio
import random
import json
import logging
import time
import os
import re
import secrets
import shutil
from datetime import datetime, timedelta
from math import ceil
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, Message

from .ai import AIClient
from .catalog import Catalog, diff_catalogs, normalize
from .config import Settings
from .db import Database
from .tonpay import (
    TonPay,
    TonPayError,
    match_payment,
    nano_to_text,
    payment_link,
    qr_png,
    usdt_to_nano,
)
from .drafts import (
    all_resolved,
    apply_catalog,
    build_draft,
    draft_from_order,
    draft_from_usual,
    mark_not_in_catalog,
    mark_skipped,
    order_items_payload,
    resolve_item,
    set_item_qty,
)
from .extras import (
    correct_voice_text,
    find_order_item,
    items_summary,
    parse_edit,
    weekly_report,
)
from .stock import hidden_ids, resolve as resolve_stock
from .formatting import (
    edit_hint,
    esc,
    format_candidates_question,
    format_catalog,
    format_client_order,
    format_missing,
    format_order,
    format_orders_page,
    format_quality,
    format_qty_question,
    LOCAL_CURRENCY,
    money,
    set_local_rate,
    split_message,
)
from .locations import choice_text as location_choice_text
from .keyboards import (
    problem_keyboard,
    alt_keyboard,
    candidates_keyboard,
    client_back_keyboard,
    client_menu_keyboard,
    claim_actions_keyboard,
    client_reply_keyboard,
    broadcast_confirm_keyboard,
    client_order_keyboard,
    feedback_keyboard,
    contact_keyboard,
    location_keyboard,
    location_pick_keyboard,
    rating_keyboard,
    rating_reason_keyboard,
    rating_reason_label,
    weigh_confirm_keyboard,
    courier_back_keyboard,
    courier_panel_keyboard,
    courier_status_keyboard,
    date_shift_keyboard,
    edit_confirm_keyboard,
    manager_panel_keyboard,
    manager_back_keyboard,
    manager_status_keyboard,
    order_detail_keyboard,
    orders_list_keyboard,
    price_update_keyboard,
    price_undo_keyboard,
    qty_keyboard,
    retry_keyboard,
    slots_keyboard,
    unit_keyboard,
    user_access_keyboard,
)
from .nlu import ParsedOrder, detect_intent, parse_local, parse_number
from .slots import default_time as slot_default_time
from .slots import parse_slots, slot_labels
from .stt import Transcriber
from .batchq import batch_group, batch_keyboard, batch_text
from .sanity import DUP_WINDOW_MIN, is_duplicate, suspicious_items
from .units import canon_unit, format_qty, pack_size, plural, price_per_unit

log = logging.getLogger(__name__)
router = Router(name="orderbot")

BOT_STARTED_AT = time.time()
_last_error: dict[str, Any] = {"at": 0.0, "text": ""}
from .pending import purge_pending_prices, take_pending_price  # noqa: E402


def record_error(exc: BaseException) -> None:
    """Запоминает последнюю необработанную ошибку для /health."""
    _last_error["at"] = time.time()
    _last_error["text"] = type(exc).__name__

# Локальный парсер надёжен для типовых заказов. Если внешний AI завис,
# не держим повара в «Разбираю…» дольше этого времени.
AI_PARSE_TIMEOUT = 30.0

NO_ITEMS_TEXT = "Не понял заказ. Напишите, например: «2 кг филе, завтра к 10»."


from .handlers_courier import (
    _courier_ok,
    _courier_order_allowed,
    _edit_courier_card,
)
from .handlers_courier import (
    _courier_ok,
    _courier_order_allowed,
    _edit_courier_card,
)
from .notify import (
    ERR_ALERT_WINDOW,
    RETRY_TEXT,
    _close_order_cards,
    _courier_chat,
    _courier_panel_text,
    _detach_card,
    _drop_card_buttons,
    _manager_chat,
    _manager_panel_text,
    _remember_retry,
    _report_failure,
    _send_order_card,
    _send_with_retry,
    _take_retry,
)

from .texts import MANAGER_ONLY_TEXT, RATE_LIMIT_TEXT  # noqa: E402
from .survey import (  # noqa: E402
    claim_text as survey_claim_text,
    details_prompt as survey_details_prompt,
    problem_label as survey_problem_label,
    problem_prompt as survey_problem_prompt,
    thanks_text as survey_thanks,
)

MANAGER_HELP_TEXT = """👔 <b>Упаковщик</b>

/start — панель
/orders — входящие
/order 12 — заказ №12
/price — прайс
/missing — чего нет в прайсе
/lowstock — что заканчивается
/stock — списание остатков при доставке
/sub — правила замены при отсутствии
/income — приход: остатки и закупка
/pricenew — цены на завтра из закупки
/markup — наценки по категориям
/quality — качество разбора
/aliases — общий словарь: /aliases
/alias_add — добавить фразу: /alias_add с/к бекон = бекон
/discount — скидка клиенту: /discount 123456789 5
/cprice — особая цена: /cprice 123456789 помидоры 1.10
/venues — точки клиента: /venues 123456789
/venue_payer — плательщик точки: /venue_payer 123456789 Точка 987654321
/health — здоровье бота
/depot — координаты склада: /depot 41.311081, 69.240562
/travel — сколько минут курьер едет по городу: /travel 25
/slots — интервалы доставки (например /slots 9-12,12-15,15-18)
"""

COURIER_HELP_TEXT = """🚚 <b>Доставщик</b>

/start — панель доставок
/deliveries — активные доставки
/order 12 — карточка заказа
/price — прайс

В карточке: «Выехал» и «Наличные получены»,
затем «Доставлено». Без отметки оплаты доставку закрыть нельзя.
"""

START_TEXT = (
    "Здравствуйте! Напишите заказ как удобно: текстом, голосом, фото списка.\n"
    "Например: «2 кг филе, 5 упаковок моцареллы, завтра к 10:00».\n"
    "В ответ пришлю состав, сумму и слот — останется подтвердить."
)

HELP_TEXT = """Напишите заказ одним сообщением — сразу передам упаковщику.

Например: 2 кг филе, 5 упаковок моцареллы, завтра к 10:00.

/price — список товаров
/last — последний заказ
/cancel — отменить"""


# ---------------------------------------------------------------------------
# заведение и допуск новых поваров
# ---------------------------------------------------------------------------
from .onboarding import (
    _ask_address,
    _ask_phone,
    _ask_venue,
    _finish_profile,
    _notify_new_user,
    _profile_step,
    _user_access_gate,
    _valid_phone,
)

from .draftprep import (
    _apply_cutoff,
    _apply_stock,
    _apply_unit_choice,
    _apply_units,
    _next_workday,
    _suggest_similar,
)

from .roles import (
    EXTRA_COURIERS,
    EXTRA_MANAGERS,
    _back_keyboard,
    _is_courier,
    _is_manager,
    _is_staff,
    _manager_ok,
    load_staff_from_db,
)

async def _understand(
    text: str, ai: AIClient, settings: Settings, user_id: int | None = None
) -> ParsedOrder:
    now = datetime.now(settings.timezone)
    try:
        parsed = await asyncio.wait_for(
            ai.parse(text, now, user_id=user_id), timeout=AI_PARSE_TIMEOUT
        )
    except asyncio.TimeoutError:
        log.warning(
            "AI не ответил за %.0f с — разбираю локально", AI_PARSE_TIMEOUT
        )
        parsed = None
    except Exception:
        # Любой сбой AI не должен мешать принять заказ локальным парсером.
        log.exception("Ошибка AI-разбора — разбираю локально")
        parsed = None
    if parsed is not None and parsed.items:
        return parsed
    return parse_local(text, settings.timezone, now)


async def _intent_reply(
    kind: str,
    user_id: int,
    chat_id: int,
    db: Database,
    settings: Settings,
    catalog: Catalog,
):
    """Короткий ответ на не-заказ. Ничего не отправляет и не вызывает AI."""
    if kind == "greeting":
        usually = await db.usual_items(user_id)
        return START_TEXT, client_menu_keyboard(usually, catalog)
    if kind == "thanks":
        return (
            "Пожалуйста! Если нужно что-то ещё — просто напишите.",
            client_back_keyboard(),
        )
    if kind == "price":
        rows = resolve_stock(catalog, await db.stock_overrides())
        return (
            format_catalog(catalog, hidden_ids(rows)),
            _back_keyboard(settings, user_id, chat_id),
        )
    if kind == "help":
        return HELP_TEXT, client_back_keyboard()
    if kind in ("status", "cancel"):
        order = await db.last_order_for_user(user_id)
        if order is None:
            return "Заказов пока нет.", client_back_keyboard()
        if kind == "status":
            labels = {
                "new": "принят",
                "accepted": "собирается",
                "ready": "собран, ждёт доставщика",
                "delivering": "в доставке",
                "done": "доставлен",
                "cancelled": "отменён",
                "failed": "не доставлен",
            }
            status = labels.get(order.get("status"), "принят")
            text = (
                format_client_order(order, "Ваш заказ")
                + f"\n\nСтатус: {status}"
            )
            return text, client_order_keyboard(order["id"])
        if order.get("status") == "new":
            text = (
                format_client_order(order, "Последний заказ")
                + "\n\nЕсли передумали — нажмите «Отменить»."
            )
            return text, client_order_keyboard(order["id"])
        if order.get("status") == "accepted":
            return (
                "Заказ уже в работе, позвоните менеджеру",
                client_back_keyboard(),
            )
        return "Заказ уже закрыт.", client_back_keyboard()
    return None


def _format_price_diff(diff: dict[str, Any]) -> str:
    lines = [
        "📋 <b>Новый список</b>",
        "",
        f"Позиций: {diff['old_count']} → {diff['new_count']}",
        f"Изменились цены: {len(diff['price_changes'])}",
        f"Новые: {len(diff['added'])}",
        f"Убраны: {len(diff['removed'])}",
    ]
    if diff["price_changes"]:
        lines.append("")
        for previous, current in diff["price_changes"][:7]:
            lines.append(
                f"{esc(current.name)}: {money(previous.price)} → {money(current.price)}"
            )
    if diff["added"]:
        lines.append("")
        for item in diff["added"][:7]:
            lines.append(f"+ {esc(item.name)}")
    return "\n".join(lines)


async def _edit_panel(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup=None,
) -> None:
    """Меняет одно сообщение вместо отправки нового — чат не забивается."""
    try:
        await _send_with_retry(
            lambda: bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
        )
    except TelegramBadRequest:
        # Сообщение уже изменили или оно слишком старое — новый дубль не нужен.
        pass


_draft_locks: dict[str, asyncio.Lock] = {}


def prune_locks() -> int:
    """Убирает завершённые блокировки, чтобы словари не росли вечно."""
    removed = 0
    for mapping in (_draft_locks, _order_locks):
        for key, lock in list(mapping.items()):
            if not lock.locked() and not getattr(lock, "_waiters", None):
                mapping.pop(key, None)
                removed += 1
    return removed


async def _duplicate_order(
    message: Message, db: Database, draft: dict
) -> bool:
    """True — этот заказ клиент уже отправил только что: второй раз не создаём."""
    chef_id = int(message.from_user.id)
    recent = await db.recent_order(chef_id, DUP_WINDOW_MIN)
    if not recent:
        return False
    if not is_duplicate(draft.get("items") or [], recent.get("items") or []):
        return False
    total = float(recent.get("total") or 0)
    await message.answer(
        "⚠️ Похоже, вы уже отправили этот заказ — "
        f"№{recent['id']} на {money(total)}.\n"
        "Повторно не создаю, чтобы не задвоить доставку.\n"
        "Если нужен ещё такой же — измените количество или напишите менеджеру."
    )
    log.info("Дубль заказа от %s отклонён (похож на №%s)", chef_id, recent["id"])
    return True


async def _warn_quantity(
    message: Message, db: Database, draft: dict
) -> None:
    """Короткое уточнение, если количество выглядит опечаткой. Заказ не блокирует."""
    items = draft.get("items") or []
    if not items:
        return
    usual_max = await db.max_qty_per_item(int(message.from_user.id))
    bad = suspicious_items(items, usual_max)
    if not bad:
        return
    lines = ["🤔 <b>Проверьте количество</b>"]
    for item, usual in bad:
        name = item.get("name") or item.get("raw") or "позиция"
        unit = item.get("unit") or item.get("catalog_unit") or ""
        line = f"• {esc(name)}: <b>{format_qty(float(item.get('qty') or 0))} {esc(str(unit))}</b>"
        if usual:
            line += f" — обычно вы берёте {format_qty(usual)} {esc(str(unit))}"
        lines.append(line)
    lines.append("")
    lines.append(
        "Если всё верно — ничего делать не нужно, заказ уже у менеджера. "
        "Если ошиблись — /cancel и пришлите заново."
    )
    try:
        await message.answer("\n".join(lines))
    except Exception:
        log.debug("Не удалось отправить предупреждение о количестве", exc_info=True)


@router.callback_query(F.data.startswith("pf:"))
async def cb_parse_feedback(callback: CallbackQuery, db: Database) -> None:
    """Оценка распознавания голоса/фото (E8)."""
    try:
        _, raw_order, verdict = callback.data.split(":")
        order_id = int(raw_order)
    except (IndexError, ValueError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    order = await db.get_order(order_id)
    if order is None:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    if int(order.get("chef_id") or 0) != int(callback.from_user.id):
        await callback.answer("Это не ваш заказ", show_alert=True)
        return
    verdict = "fix" if verdict == "fix" else "ok"
    await db.record_parse_feedback(
        order_id,
        int(callback.from_user.id),
        str(order.get("parse_provider") or order.get("source") or ""),
        verdict,
    )
    if verdict == "ok":
        text = f"Спасибо! Заказ №{order_id} понят верно."
    else:
        text = (
            f"Заказ №{order_id}. Напишите, что поправить, одним сообщением: "
            "«убери помидоры», «вместо 2 кг — 3 кг филе»."
        )
    try:
        await callback.message.edit_text(text)
    except Exception:
        pass
    await callback.answer("Записал")


@router.callback_query(F.data.startswith("loc:"))
async def cb_pick_location(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    catalog: Catalog,
) -> None:
    """Выбор точки при заказе (C11)."""
    try:
        _, draft_id, raw_location = callback.data.split(":")
        location_id = int(raw_location)
    except (IndexError, ValueError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    draft = await db.get_draft(draft_id)
    if draft is None or str(draft.get("status")) != "draft":
        await callback.answer("Заказ уже обработан", show_alert=True)
        return
    if int(draft.get("user_id") or 0) != int(callback.from_user.id):
        await callback.answer("Это не ваш заказ", show_alert=True)
        return
    spot = await db.get_location(location_id) if location_id else None
    if location_id and (
        spot is None or int(spot.get("client_id") or 0) != int(callback.from_user.id)
    ):
        await callback.answer("Точка не найдена", show_alert=True)
        return
    if spot is None:
        draft["location_id"] = None
        draft["location_name"] = None
        draft["location_asked"] = True
        await db.save_draft(draft)
        await callback.answer("Без точки")
    else:
        draft["location_id"] = int(spot["id"])
        draft["location_name"] = spot.get("name")
        if spot.get("address"):
            draft["delivery_address"] = spot["address"]
        draft["location_asked"] = True
        await db.save_draft(draft)
        await callback.answer(f"Точка: {spot.get('name')}")
    try:
        await callback.message.edit_text(
            f"🏠 Точка: <b>{esc(str(draft.get('location_name') or 'без точки'))}</b>"
        )
    except Exception:
        pass
    await _advance_draft(
        callback.bot,
        db,
        settings,
        catalog,
        draft,
        callback.message.chat.id,
        callback.message.message_id,
    )


async def _advance_draft(
    bot: Bot,
    db: Database,
    settings: Settings,
    catalog: Catalog,
    draft: dict,
    chat_id: int,
    message_id: int,
) -> bool:
    """Один вопрос за раз в одном сообщении. Когда всё ясно — сразу менеджеру."""
    lock = _draft_locks.setdefault(draft["id"], asyncio.Lock())
    async with lock:
        fresh = await db.get_draft(draft["id"])
        if fresh is None or fresh.get("status") != "draft":
            return False

        # C11: у сети несколько точек — спрашиваем точку до остальных вопросов
        if not fresh.get("location_id") and not fresh.get("location_asked"):
            client_locations = await db.client_locations(
                int(fresh.get("user_id") or 0)
            )
            if len(client_locations) == 1:
                spot = client_locations[0]
                fresh["location_id"] = int(spot["id"])
                fresh["location_name"] = spot.get("name")
                if spot.get("address"):
                    fresh["delivery_address"] = spot["address"]
                await db.save_draft(fresh)
            elif len(client_locations) > 1:
                fresh["location_asked"] = True
                await db.save_draft(fresh)
                await _edit_panel(
                    bot,
                    chat_id,
                    message_id,
                    location_choice_text(),
                    location_pick_keyboard(fresh["id"], client_locations),
                )
                return True

        # E5: несколько однотипных уточнений — одним сообщением
        pending = batch_group(fresh.get("items") or [])
        if pending:
            fresh["questions"] = int(fresh.get("questions") or 0) + 1
            await db.save_draft(fresh)
            await db.log_question("пакет", "batch")
            await _edit_panel(
                bot,
                chat_id,
                message_id,
                batch_text(pending),
                batch_keyboard(fresh["id"], pending),
            )
            return True

        for item in fresh.get("items", []):
            if item.get("status") == "unavailable":
                fresh["questions"] = int(fresh.get("questions") or 0) + 1
                await db.save_draft(fresh)
                await db.log_question(
                    item.get("name") or item.get("raw") or "", "stock"
                )
                suggestions = await _suggest_similar(catalog, item)
                name = item.get("name") or item.get("raw") or "позиция"
                text = f"«{esc(name)}» сегодня нет."
                text += (
                    " Можно заменить на похожее:"
                    if suggestions
                    else " Уберите её из заказа."
                )
                await _edit_panel(
                    bot,
                    chat_id,
                    message_id,
                    text,
                    alt_keyboard(fresh["id"], item["idx"], suggestions),
                )
                return True
            if item.get("status") == "needs_unit":
                fresh["questions"] = int(fresh.get("questions") or 0) + 1
                await db.save_draft(fresh)
                await db.log_question(
                    item.get("name") or item.get("raw") or "", "unit"
                )
                base = item.get("catalog_unit") or "шт"
                size = pack_size(item.get("pack"), base)
                qty = float(item.get("qty") or 1)
                text = f"<b>{esc(item.get('name') or '')}</b> продаётся "
                text += plural(base, 2)
                if size:
                    text += f" по {format_qty(size)} {base}"
                text += ". Сколько?"
                await _edit_panel(
                    bot,
                    chat_id,
                    message_id,
                    text,
                    unit_keyboard(
                        fresh["id"],
                        item["idx"],
                        base,
                        item.get("pack"),
                        qty,
                        item.get("unit") or base,
                    ),
                )
                return True
            if item.get("status") == "needs_choice":
                fresh["questions"] = int(fresh.get("questions") or 0) + 1
                await db.save_draft(fresh)
                await db.log_question(item.get("raw") or "", "choice")
                await _edit_panel(
                    bot,
                    chat_id,
                    message_id,
                    format_candidates_question(item),
                    candidates_keyboard(
                        fresh["id"],
                        item["idx"],
                        catalog,
                        item.get("candidates") or [],
                    ),
                )
                return True
            if item.get("status") == "needs_qty":
                fresh["questions"] = int(fresh.get("questions") or 0) + 1
                await db.save_draft(fresh)
                await db.log_question(
                    item.get("name") or item.get("raw") or "", "qty"
                )
                await _edit_panel(
                    bot,
                    chat_id,
                    message_id,
                    format_qty_question(item),
                    qty_keyboard(
                        fresh["id"],
                        item["idx"],
                        item.get("final_unit") or item.get("unit"),
                    ),
                )
                return True

        items = order_items_payload(fresh)
        if not items:
            fresh["status"] = "cancelled"
            await db.save_draft(fresh)
            await _edit_panel(
                bot,
                chat_id,
                message_id,
                "Вы пропустили все позиции. Пришлите заказ ещё раз.",
                client_back_keyboard(),
            )
            return True

        min_total = float(settings.min_order_usd or 0)
        if min_total and float(fresh.get("total") or 0) < min_total:
            await db.save_draft(fresh)
            await _edit_panel(
                bot,
                chat_id,
                message_id,
                f"Минимальная сумма заказа — {money(min_total)}. "
                f"Сейчас в заказе {money(fresh.get('total'))}. Добавьте позиции.",
                client_back_keyboard(),
            )
            return True
        user = await db.get_user(int(fresh.get("user_id") or 0))
        if user is None or not user.get("phone") or not user.get("address"):
            fresh["status"] = "draft"
            await db.save_draft(fresh)
            await _edit_panel(
                bot,
                chat_id,
                message_id,
                "Сначала заполните профиль: телефон и адрес доставки.\n"
                "Нажмите /start и ответьте на вопросы.",
                client_back_keyboard(),
            )
            return True
        if not fresh.get("delivery_time"):
            slots, busy = await _slots_with_capacity(db, fresh.get("delivery_date"))
            labels = [
                (f"🚫 {label} — занято" if start in busy else label)
                for (start, _), label in zip(slots, slot_labels(slots))
            ]
            await db.save_draft(fresh)
            await _edit_panel(
                bot,
                chat_id,
                message_id,
                "Когда удобно привезти? Выберите интервал — упаковщик будет знать, "
                "к какому времени собирать заказ.",
                slots_keyboard(fresh["id"], labels),
            )
            return True

        fresh["delivery_address"] = user.get("address")
        fresh["delivery_phone"] = user.get("phone")
        fresh["payment_method"] = "cash"

        if not await db.claim_draft(fresh["id"]):
            return False
        fresh["status"] = "confirming"
        manager = _manager_chat(settings)
        try:
            order_id = await db.create_order(
                fresh, items, manager, draft_id=fresh["id"]
            )
        except Exception:
            await db.release_draft(fresh["id"])
            log.exception("Не удалось сохранить заказ из черновика %s", fresh["id"])
            await _edit_panel(
                bot,
                chat_id,
                message_id,
                "Не получилось сохранить заказ. Пришлите его ещё раз.",
                client_back_keyboard(),
            )
            return True

        log.info(
            "Заказ №%s: позиций=%s, сумма=%s, повар=%s (id=%s)",
            order_id,
            len(items),
            money(sum(float(item.get("total") or 0) for item in items)),
            fresh.get("user_name"),
            fresh.get("user_id"),
        )
        fresh["status"] = "confirmed"
        try:
            await db.save_draft(fresh)
        except Exception:
            log.exception("Не удалось обновить черновик %s", fresh["id"])

        order = await db.get_order(order_id)
        assert order is not None
        if user and user.get("venue"):
            order["venue"] = user["venue"]

        manager_sent = False
        # D4: если на смене есть упаковщики — заказ идёт тому, кто дольше ждал
        recipient = manager
        assigned = await db.pick_packer()
        if assigned and manager is not None and assigned != manager:
            recipient = assigned
            try:
                await db.assign_order(order_id, assigned)
            except Exception:
                log.warning("Не удалось назначить заказ №%s", order_id, exc_info=True)
                recipient = manager
        if recipient is not None:
            card_id: int | None = None
            try:
                card_id = await _send_order_card(
                    bot,
                    recipient,
                    order,
                    "🆕 Новый заказ",
                    manager_status_keyboard(order_id, "new"),
                )
            except Exception:
                log.exception("Не удалось отправить заказ №%s менеджеру", order_id)
            else:
                manager_sent = True
                try:
                    await db.mark_order_notified(order_id)
                except Exception:
                    log.exception("Не удалось отметить заказ №%s доставленным", order_id)
                if card_id:
                    try:
                        await db.set_order_cards(order_id, manager_card=card_id)
                    except Exception:
                        log.warning("Не удалось сохранить id карточки заказа №%s", order_id)
                log.info("Заказ №%s отправлен упаковщику %s", order_id, recipient)

        if manager is None:
            text = format_client_order(order, "📦 Заказ")
            text += "\n\n<i>Упаковщик пока не подключён.</i>"
        elif manager_sent:
            text = format_client_order(order, "✅ Заказ отправлен")
        else:
            text = format_client_order(order, "⚠️ Заказ сохранён")
            text += "\n\n<i>Передам упаковщику, как только появится связь.</i>"
        try:
            text += "\n" + edit_hint(
                await db.get_setting("cutoff_time", ""), order.get("delivery_date")
            )
        except Exception:
            log.debug("Не удалось добавить подсказку об изменении заказа")
        await _edit_panel(
            bot, chat_id, message_id, text, client_order_keyboard(order_id)
        )
        # E9: если фото-списки распознаются плохо — просим голосом.
        if str(fresh.get("source") or "") == "photo":
            try:
                photo = await db.photo_quality(30)
                if photo["total"] >= 5 and photo["fix_pct"] >= 30:
                    await bot.send_message(
                        chat_id,
                        "📷 Фото иногда распознаётся хуже: если что-то не так — "
                        "пришлите тот же список голосом, я поправлю.",
                    )
            except Exception:
                log.debug("Не удалось показать подсказку про фото")

        # C12: без геопина окно прибытия грубое — предлагаем один раз.
        chef_id = int(fresh.get("user_id") or 0)
        if chef_id and await db.user_coords(chef_id) is None:
            if not await db.get_chat_state(chef_id, "geo_hint"):
                await db.set_chat_state(chef_id, "geo_hint", "1")
                try:
                    await bot.send_message(
                        chef_id,
                        "📍 Пришлите геопин (скрепка → «Геопозиция») — "
                        "курьер назовёт более точное время. Это разово.",
                    )
                except Exception:
                    log.debug("Не удалось предложить геопин")

        # E8: для голоса и фото спрашиваем, верно ли распознан заказ.
        if str(fresh.get("source") or "") in ("voice", "photo"):
            try:
                await bot.send_message(
                    chat_id,
                    "🎤 Как я понял заказ? Если ошибся — поправьте.",
                    reply_markup=feedback_keyboard(order_id),
                )
            except Exception:
                log.debug("Не удалось спросить про качество распознавания")
        try:
            await db.set_order_cards(order_id, chef_card=message_id)
        except Exception:
            log.warning("Не удалось сохранить карточку повара заказа №%s", order_id)
        note = fresh.get("date_note")
        if note:
            try:
                await bot.send_message(
                    chat_id, esc(note), reply_markup=date_shift_keyboard(order_id)
                )
            except Exception:
                log.warning("Не удалось отправить пометку о переносе даты")
        return True


_order_locks: dict[int, asyncio.Lock] = {}


async def _process_order(message: Message, *args, **kwargs) -> None:
    """Один чат — один заказ одновременно.

    Иначе два быстрых сообщения разбираются параллельно, и более старое
    может завершиться позже, отменив новый черновик.
    """
    lock = _order_locks.setdefault(message.chat.id, asyncio.Lock())
    async with lock:
        await _process_order_serial(message, *args, **kwargs)


def _schedule_shadow_parse(
    db: Database,
    ai: AIClient,
    settings: Settings,
    text: str,
    chef_id: int,
    main_items: list,
    order_id: int | None,
) -> None:
    """Теневой разбор части заказов: только сравнение и запись (E10)."""
    from .shadow import compare_items, should_sample

    share = float(getattr(settings, "shadow_prompt_pct", 0) or 0)
    if share <= 0 or not text:
        return
    if not should_sample(share, random.random()):
        return

    async def _run() -> None:
        from datetime import datetime as _dt

        try:
            parsed = await ai.shadow_parse(text, _dt.now(settings.timezone))
            if parsed is None:
                return
            shadow_items = [
                {"catalog_id": item.catalog_id, "qty": item.qty}
                for item in parsed.items
            ]
            result = compare_items(main_items, shadow_items)
            await db.add_shadow_parse(
                order_id, chef_id, str(getattr(parsed, "provider", "")), result
            )
            log.info(
                "Теневой разбор: совпадение %.0f%% (%s из %s)",
                float(result["match_pct"]),
                result["matched"],
                result["items_main"],
            )
        except Exception:
            log.info("Теневой разбор не записался", exc_info=True)

    try:
        asyncio.create_task(_run())
    except Exception:
        log.debug("Не удалось запустить теневой разбор", exc_info=True)


async def _process_order_serial(
    message: Message,
    text: str,
    source: str,
    transcript: str | None,
    db: Database,
    settings: Settings,
    catalog: Catalog,
    ai: AIClient,
    status_message: Message | None = None,
) -> None:
    bot = message.bot
    started = time.monotonic()
    status = status_message or await message.answer("Секунду, читаю заказ…")
    if transcript:
        try:
            await status.edit_text(
                f"🎤 <i>Услышал:</i> {esc(transcript)}\n\nСобираю заказ…"
            )
        except Exception:
            log.debug("Не удалось показать расшифровку", exc_info=True)
    try:
        parsed = await _understand(
            text, ai, settings, user_id=message.from_user.id
        )
        if not parsed.items:
            await status.edit_text(NO_ITEMS_TEXT)
            return

        await db.cancel_active_drafts(message.chat.id)
        aliases = await db.get_aliases(message.from_user.id)
        draft = build_draft(
            parsed=parsed,
            catalog=catalog,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            user_name=message.from_user.full_name,
            username=message.from_user.username,
            source=source,
            transcript=transcript,
            accept_threshold=settings.fuzzy_accept,
            candidate_threshold=settings.fuzzy_candidate,
            aliases=aliases,
        )
        await _apply_cutoff(draft, db, settings)
        await _apply_stock(draft, db, catalog)
        await _apply_units(draft, db, catalog)
        if await _duplicate_order(message, db, draft):
            await status.edit_text(
                "Такой заказ уже есть — новый не создаю. Подробности выше."
            )
            return
        await db.create_draft(draft)
        _schedule_shadow_parse(
            db=db,
            ai=ai,
            settings=settings,
            text=text,
            chef_id=message.from_user.id,
            main_items=draft.get("items") or [],
            order_id=None,
        )
        await _warn_quantity(message, db, draft)
        await db.log_parse(
            chef_id=message.from_user.id,
            chat_id=message.chat.id,
            source=source,
            path=str(getattr(parsed, "provider", "") or draft.get("provider") or "nlu"),
            items=len(draft.get("items") or []),
            questions=int(draft.get("questions") or 0),
            latency_ms=int((time.monotonic() - started) * 1000),
            text_len=len(text or ""),
        )
        await _advance_draft(
            bot,
            db,
            settings,
            catalog,
            draft,
            status.chat.id,
            status.message_id,
        )
    except Exception as exc:
        log.exception("Не удалось обработать заказ — сохраняю текст для повтора")
        try:
            await _remember_retry(db, message.chat.id, text, source, transcript)
        except Exception:
            log.warning("Не удалось сохранить текст заказа для повтора")
        try:
            await status.edit_text(RETRY_TEXT, reply_markup=retry_keyboard())
        except Exception:
            try:
                await message.answer(RETRY_TEXT, reply_markup=retry_keyboard())
            except Exception:
                log.debug("Не удалось показать кнопку повтора", exc_info=True)
        await _report_failure(bot, db, settings, "order", exc)


# ---------------------------------------------------------------------------
# команды
# ---------------------------------------------------------------------------


async def _orders_page(db: Database, kind: str, page: int):
    statuses = (
        ("new", "accepted", "ready", "delivering") if kind == "inbox" else None
    )
    total = await db.count_orders(statuses)
    pages = max(1, ceil(total / 10))
    page = max(0, min(page, pages - 1))
    orders = await db.list_orders_page(limit=10, offset=page * 10, statuses=statuses)
    title = "📥 <b>Входящие заказы</b>" if kind == "inbox" else "📜 <b>История заказов</b>"
    return (
        format_orders_page(orders, page, pages, total, title),
        orders_list_keyboard(orders, kind, page, pages),
    )


async def _courier_page(db: Database, page: int):
    statuses = ("ready", "delivering")
    total = await db.count_orders(statuses)
    pages = max(1, ceil(total / 10))
    page = max(0, min(page, pages - 1))
    orders = await db.list_orders_page(limit=10, offset=page * 10, statuses=statuses)
    return (
        format_orders_page(orders, page, pages, total, "🚚 <b>Активные доставки</b>"),
        courier_panel_keyboard(orders, page, pages),
    )


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    db: Database,
    settings: Settings,
    catalog: Catalog,
    command: CommandObject | None = None,
) -> None:
    await db.set_chat_state(message.chat.id, "qty_input", "")
    await _remember_invite(message, db, settings, command)
    if _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer(
            await _manager_panel_text(db), reply_markup=manager_panel_keyboard()
        )
        return
    if _is_courier(settings, message.from_user.id, message.chat.id):
        text, keyboard = await _courier_page(db, 0)
        await message.answer(
            await _courier_panel_text(db) + "\n\n" + text,
            reply_markup=keyboard,
        )
        return
    user = await db.ensure_user(
        message.from_user.id,
        message.from_user.full_name,
        message.from_user.username,
    )
    if user.get("access") == "rejected":
        await message.answer("Доступ закрыт. Напишите менеджеру.")
        return
    if await _profile_step(message, db, user, catalog):
        return
    if user.get("access") == "pending":
        await message.answer("Ждём подтверждения от менеджера.")
        return
    usually = await db.usual_items(message.from_user.id)
    await message.answer(
        f"Здравствуйте! Заведение: <b>{esc(user['venue'])}</b>.\n\n{START_TEXT}",
        reply_markup=client_menu_keyboard(usually, catalog),
    )
    await message.answer(
        "Кнопки внизу помогут быстрее: «Мои заказы», «Как обычно», «Менеджер».",
        reply_markup=client_reply_keyboard(),
    )


async def _remember_invite(
    message: Message,
    db: Database,
    settings: Settings,
    command: CommandObject | None,
) -> None:
    """Ссылка-приглашение: /start ref_<id> запоминает, кто привёл клиента."""
    raw = ((command.args if command else None) or "").strip()
    if not raw.lower().startswith("ref_"):
        return
    referrer = raw[4:].strip()
    if not referrer.lstrip("-").isdigit():
        return
    user_id = int(message.from_user.id)
    if int(referrer) == user_id:
        return
    existing = await db.get_setting(f"ref:{user_id}", "")
    if existing:
        return
    await db.set_setting(f"ref:{user_id}", referrer)
    await db.set_setting(
        f"ref_at:{user_id}", datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    manager = settings.manager_chat_id
    if manager:
        try:
            await message.bot.send_message(
                int(manager),
                f"🤝 Новый клиент пришёл по приглашению {referrer}: "
                f"{esc(message.from_user.full_name)} (id {user_id}).",
            )
        except Exception:
            pass


@router.message(Command("invite"))
async def cmd_invite(message: Message, db: Database, settings: Settings) -> None:
    """Ссылка-приглашение для клиента: привёл друга — видно менеджеру."""
    if _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Ссылки клиентов смотрите командой /refs.")
        return
    me = await message.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
    await message.answer(
        "🤝 Пригласите коллегу из другого заведения:\n"
        f"{link}\n\n"
        "По этой ссылке он сразу попадёт к нам, а мы увидим, кто привёл."
    )


@router.message(Command("help"))
async def cmd_help(message: Message, settings: Settings) -> None:
    if _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer(
            MANAGER_HELP_TEXT, reply_markup=manager_back_keyboard()
        )
        return
    if _is_courier(settings, message.from_user.id, message.chat.id):
        await message.answer(
            COURIER_HELP_TEXT, reply_markup=courier_back_keyboard()
        )
        return
    await message.answer(HELP_TEXT, reply_markup=client_back_keyboard())


@router.message(Command("id"))
async def cmd_id(message: Message, settings: Settings) -> None:
    await message.answer(
        f"Ваш чат: <code>{message.chat.id}</code>\n"
        f"Пользователь: <code>{message.from_user.id}</code>",
        reply_markup=_back_keyboard(settings, message.from_user.id, message.chat.id),
    )


@router.message(Command("price"))
async def cmd_price(
    message: Message, catalog: Catalog, settings: Settings, db: Database
) -> None:
    rows = resolve_stock(catalog, await db.stock_overrides())
    text = format_catalog(catalog, hidden_ids(rows))
    keyboard = _back_keyboard(settings, message.from_user.id, message.chat.id)
    if len(text) <= 4000:
        await message.answer(text, reply_markup=keyboard)
        return
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3900:
            await message.answer(chunk, reply_markup=keyboard)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await message.answer(chunk, reply_markup=keyboard)


@router.message(Command("orders"))
async def cmd_orders(message: Message, db: Database, settings: Settings) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    text, keyboard = await _orders_page(db, "inbox", 0)
    await message.answer(text, reply_markup=keyboard)


@router.message(Command("deliveries"))
async def cmd_deliveries(
    message: Message, db: Database, settings: Settings
) -> None:
    if not _is_courier(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только доставщику.")
        return
    text, keyboard = await _courier_page(db, 0)
    await message.answer(text, reply_markup=keyboard)


@router.message(Command("export"))
async def cmd_export(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    raw = (command.args or "").strip()
    days = int(raw) if raw.isdigit() and 1 <= int(raw) <= 365 else 7
    now = datetime.now()
    start = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    # +1 секунда: заказ, созданный в эту же секунду, тоже попадает в отчёт.
    end = (now + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    orders = await db.orders_between(start, end)
    if not orders:
        await message.answer(f"За последние {days} дней заказов нет.")
        return
    venues = await db.venue_map(
        [int(order["chef_id"]) for order in orders if order.get("chef_id")]
    )
    try:
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Заказы"
        sheet.append(["Дата", "Заведение", "Позиции", "Сумма", "Статус"])
        for order in orders:
            venue = venues.get(int(order.get("chef_id") or 0)) or order.get("chef_name") or ""
            positions = "; ".join(
                f"{item.get('name')} — {format_qty(item.get('qty'))} "
                f"{plural(item.get('unit'), item.get('qty'))}"
                for item in order.get("items", [])
            )
            sheet.append(
                [
                    order.get("created_at"),
                    venue,
                    positions,
                    float(order.get("total") or 0),
                    order.get("status"),
                ]
            )
        exports = settings.data_dir / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        path = exports / f"orders-{now.strftime('%Y%m%d-%H%M%S')}.xlsx"
        workbook.save(path)
    except Exception as exc:
        log.exception("Не удалось собрать Excel")
        await message.answer(f"Не получилось собрать файл: {type(exc).__name__}")
        return
    try:
        await message.bot.send_document(
            message.chat.id,
            FSInputFile(path),
            caption=f"Заказы за {days} дн. · {len(orders)} шт.",
        )
    finally:
        path.unlink(missing_ok=True)


@router.message(Command("price_undo"))
async def cmd_price_undo(
    message: Message, db: Database, settings: Settings, catalog: Catalog
) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    versions = await db.list_price_versions(5)
    if not versions:
        await message.answer("Сохранённых версий прайса пока нет.")
        return
    version = versions[0]
    path = Path(version["path"])
    if not path.exists():
        await message.answer("Файл последней версии не найден.")
        return
    try:
        restored = Catalog.load(path)
    except Exception:
        await message.answer("Файл версии повреждён.")
        return
    diff = diff_catalogs(restored, catalog)
    lines = [
        f"↩️ <b>Вернуть прайс от {esc(version['created_at'])}</b>",
        "",
        f"Позиций: {len(catalog.items)} → {len(restored.items)}",
        f"Изменились цены: {len(diff['price_changes'])}",
        f"Появятся: {len(diff['added'])}",
        f"Исчезнут: {len(diff['removed'])}",
    ]
    for previous, current in diff["price_changes"][:5]:
        lines.append(f"{esc(current.name)}: {money(previous.price)} → {money(current.price)}")
    await message.answer(
        "\n".join(lines), reply_markup=price_undo_keyboard(int(version["id"]))
    )


async def _ai_usage_line(db: Database, ai: AIClient, settings: Settings) -> str:
    """Строка «ИИ за сутки: N вызовов, ~$X» для /health (E4)."""
    try:
        from .aicost import estimate, usage_line

        today = datetime.now(settings.timezone).date().isoformat()
        rows = await db.llm_usage_day(today)
        calls = sum(int(row.get("calls") or 0) for row in rows)
        cost = sum(float(row.get("est_cost") or 0) for row in rows)
        calls = max(calls, sum(int(v) for v in ai.calls_today.values()))
        cost = max(cost, estimate(calls, getattr(settings, "llm_cost_per_call", 0.002)))
        budget = int(getattr(settings, "llm_daily_budget", 0) or 0)
        return "\n" + usage_line(calls, cost, budget) + (
            f"\nЗаблокировано по бюджету: {ai.budget_blocked}" if ai.budget_blocked else ""
        )
    except Exception:
        return ""


@router.message(Command("health"))
async def cmd_health(
    message: Message, db: Database, settings: Settings, ai: AIClient
) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    uptime = int(time.time() - BOT_STARTED_AT)
    days, rest = divmod(uptime, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days} д")
    if hours or days:
        parts.append(f"{hours} ч")
    parts.append(f"{minutes} мин")
    labels = {
        "not_configured": "не задан",
        "ok": "работает",
        "lazy": "проверю при запросе",
        "quota": "лимит",
        "degraded": "сбои",
        "auth_failed": "ключ отклонён",
        "unreachable": "недоступен",
        "unknown": "не проверен",
    }
    pending = len(await db.list_unnotified_orders(500))
    pending_courier = len(await db.list_unnotified_delivery_orders(500))
    if _last_error["at"]:
        error_text = (
            time.strftime("%d.%m %H:%M", time.localtime(_last_error["at"]))
            + " — "
            + str(_last_error["text"])
        )
    else:
        error_text = "нет"
    size = await db.size_bytes()
    try:
        db_ms = await db.ping()
        db_line = f"БД: {'PostgreSQL' if db.is_postgres else 'SQLite'}, ответ {db_ms} мс\n"
    except Exception:
        db_line = f"БД: {'PostgreSQL' if db.is_postgres else 'SQLite'}, не отвечает\n"
    text = (
        "🩺 <b>Здоровье</b>\n\n"
        + db_line
        + f"Аптайм: {', '.join(parts)}\n"
        f"DeepSeek: {labels.get(ai.deepseek_status, ai.deepseek_status)}\n"
        f"Gemini: {labels.get(ai.gemini_status, ai.gemini_status)}\n"
        f"Ждут упаковщика: {pending}\n"
        f"Ждут доставщика: {pending_courier}\n"
        f"Последняя ошибка: {error_text}\n"
        f"Размер БД: {size / 1024 / 1024:.1f} МБ\n"
        f"Вызовов AI за сутки: DeepSeek {ai.calls_today.get('deepseek', 0)}, "
        f"Gemini {ai.calls_today.get('gemini', 0)}"
        + await _ai_usage_line(db, ai, settings)
    )
    await message.answer(text, reply_markup=manager_back_keyboard())


@router.message(Command("order"))
async def cmd_order(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Укажите номер: <code>/order 12</code>")
        return
    order = await db.get_order(int(command.args.strip()))
    if order is None:
        await message.answer("Заказ не найден.")
        return
    is_manager = _is_manager(settings, message.from_user.id, message.chat.id)
    is_courier = _is_courier(settings, message.from_user.id, message.chat.id)
    is_owner = order.get("chef_id") == message.from_user.id
    if not (is_manager or is_courier or is_owner):
        await message.answer("Нет доступа к этому заказу.")
        return
    if is_manager:
        keyboard = manager_status_keyboard(order["id"], order.get("status"))
    elif is_courier:
        keyboard = courier_status_keyboard(
            order["id"],
            str(order.get("status")),
            str(order.get("payment_status") or "unpaid"),
        )
    else:
        keyboard = client_order_keyboard(order["id"])
    await message.answer(format_order(order, "📦 Заказ"), reply_markup=keyboard)


@router.message(Command("last"))
async def cmd_last(message: Message, db: Database) -> None:
    order = await db.last_order_for_user(message.from_user.id)
    if order is None:
        await message.answer(
            "У вас пока нет заказов.", reply_markup=client_back_keyboard()
        )
        return
    await message.answer(
        format_client_order(order, "Последний заказ"),
        reply_markup=client_order_keyboard(order["id"]),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, db: Database, settings: Settings) -> None:
    await db.set_chat_state(message.chat.id, "qty_input", "")
    count = await db.cancel_active_drafts(message.chat.id)
    keyboard = _back_keyboard(settings, message.from_user.id, message.chat.id)
    if count:
        await message.answer(
            "Отменил. Можно прислать новый заказ.",
            reply_markup=keyboard,
        )
    else:
        await message.answer("Нечего отменять.", reply_markup=keyboard)


# ---------------------------------------------------------------------------
# приём заказа
# ---------------------------------------------------------------------------


# Заказ клиента — отдельный роутер.
from .handlers_client import (  # noqa: E402
    _item_from_catalog,
    _load_draft_for_callback,
)
from .handlers_client import router as _client_router  # noqa: E402

router.include_router(_client_router)

from .handlers_settings import router as _settings_router  # noqa: E402

router.include_router(_settings_router)

from .handlers_stock import router as _stock_router  # noqa: E402

router.include_router(_stock_router)

@router.callback_query(F.data.startswith("price:apply:"))
async def cb_price_apply(
    callback: CallbackQuery, db: Database, settings: Settings, catalog: Catalog
) -> None:
    if not await _manager_ok(callback, settings):
        return
    token = callback.data.split(":", 2)[2]
    pending = await take_pending_price(db, token)
    if pending is None:
        await callback.answer("Обновление устарело", show_alert=True)
        return
    path = Path(str(pending["path"]))
    try:
        new_catalog = Catalog.load(path)
    except Exception:
        log.warning("Не удалось прочитать подтверждённый прайс %s", path)
        path.unlink(missing_ok=True)
        await callback.answer("Файл прайса не читается", show_alert=True)
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    versions_dir = settings.data_dir / "price_versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    if settings.catalog_path.exists():
        archive = versions_dir / f"catalog-{stamp}.xlsx"
        try:
            shutil.copy2(settings.catalog_path, archive)
            await db.add_price_version(str(archive), "предыдущая версия", len(catalog.items))
            for old_path in await db.prune_price_versions(5):
                try:
                    Path(old_path).unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError:
            log.warning("Не удалось сохранить версию прайса")
        backup_dir = settings.data_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings.catalog_path, backup_dir / f"catalog-{stamp}.xlsx")
    temp = settings.catalog_path.with_suffix(settings.catalog_path.suffix + ".tmp")
    shutil.copy2(path, temp)
    os.replace(temp, settings.catalog_path)
    catalog.items = new_catalog.items
    catalog.by_id = new_catalog.by_id
    catalog._phrases = new_catalog._phrases
    path.unlink(missing_ok=True)
    log.info("Прайс обновлён вручную: %d позиций", len(catalog.items))
    try:
        await callback.message.edit_text(
            f"Список обновлён: {len(catalog.items)} позиций.\n"
            "Вернуть прежний: /price_undo",
            reply_markup=manager_back_keyboard(),
        )
    except TelegramBadRequest:
        pass
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("price:cancel:"))
async def cb_price_cancel(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    if not await _manager_ok(callback, settings):
        return
    token = callback.data.split(":", 2)[2]
    pending = await take_pending_price(db, token)
    if pending is not None:
        Path(str(pending["path"])).unlink(missing_ok=True)
    try:
        await callback.message.edit_text(
            "Оставил прежний список.", reply_markup=manager_back_keyboard()
        )
    except TelegramBadRequest:
        pass
    await callback.answer("Отменил")


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


# Панель доставщика — отдельный роутер.
from .handlers_courier import router as _courier_handlers  # noqa: E402

router.include_router(_courier_handlers)

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


@router.callback_query(F.data.startswith("editok:"))
async def cb_edit_ok(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    if callback.message is None:
        await callback.answer("Сообщение недоступно", show_alert=True)
        return
    choice = callback.data.split(":", 1)[1]
    raw = await db.get_chat_state(callback.message.chat.id, "edit")
    await db.set_chat_state(callback.message.chat.id, "edit", "")
    if choice != "yes" or not raw:
        try:
            await callback.message.edit_text("Оставил как было.", reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.answer("Хорошо")
        return
    try:
        data = json.loads(raw)
        order_id = int(data["order_id"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        await callback.answer("Правка устарела", show_alert=True)
        return
    order = await db.get_order(order_id)
    if order is None or order.get("status") != "new":
        await callback.answer("Заказ уже нельзя поправить", show_alert=True)
        return
    await db.replace_order_items(order_id, data["items"], data["total"])
    updated = await db.get_order(order_id)
    assert updated is not None
    manager = order.get("manager_chat_id") or _manager_chat(settings)
    if manager and order.get("manager_card_message_id"):
        try:
            await callback.bot.edit_message_text(
                chat_id=manager,
                message_id=int(order["manager_card_message_id"]),
                text=format_order(updated, "📦 Заказ (изменён)"),
                reply_markup=manager_status_keyboard(order_id, "new"),
            )
        except Exception:
            log.warning("Не удалось обновить карточку заказа №%s", order_id)
    if manager:
        try:
            await callback.bot.send_message(
                manager, f"✏️ Повар изменил заказ №{order_id}"
            )
        except Exception:
            log.warning("Не удалось уведомить менеджера об изменении заказа")
    summary = items_summary(data["items"])
    text = f"✅ Заказ №{order_id} обновлён\n\n{summary}\n\n💰 <b>Итого: ${data['total']:,.2f}</b>"
    try:
        await callback.message.edit_text(text, reply_markup=client_order_keyboard(order_id))
    except TelegramBadRequest:
        pass
    await callback.answer("Обновил")


@router.callback_query(F.data.startswith("unit:"))
async def cb_unit(
    callback: CallbackQuery, db: Database, settings: Settings, catalog: Catalog
) -> None:
    try:
        _, draft_id, idx_raw, code = callback.data.split(":")
        idx = int(idx_raw)
    except (ValueError, AttributeError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    draft = await _load_draft_for_callback(callback, draft_id, db)
    if draft is None:
        return
    item = next((i for i in draft["items"] if i["idx"] == idx), None)
    if item is None:
        await callback.answer("Не нашёл позицию", show_alert=True)
        return
    _apply_unit_choice(item, code, catalog)
    await db.save_draft(draft)
    if item.get("catalog_id") is not None:
        await db.set_chat_state(
            draft["user_id"], f"unit_habit:{item['catalog_id']}", code
        )
    await callback.answer("Запомнил")
    await _advance_draft(
        callback.bot,
        db,
        settings,
        catalog,
        draft,
        callback.message.chat.id,
        callback.message.message_id,
    )


async def _slots_with_capacity(
    db: Database, day: str | None
) -> tuple[list[tuple[str, str]], set[str]]:
    """Интервалы доставки и занятые слоты (лимит — настройка slot_capacity).

    Список не сокращается, чтобы индексы кнопок не съезжали: занятый слот
    показывается отдельной пометкой и не принимается при нажатии.
    """
    slots = parse_slots(await db.get_setting("delivery_slots"))
    try:
        capacity = int(await db.get_setting("slot_capacity", "0") or 0)
    except ValueError:
        capacity = 0
    if capacity <= 0:
        return slots, set()
    target = (day or datetime.now().date().isoformat())[:10]
    counts: dict[str, int] = {}
    for order in await db.orders_for_date(target):
        if str(order.get("status")) == "cancelled":
            continue
        key = str(order.get("delivery_time") or "")[:5]
        counts[key] = counts.get(key, 0) + 1
    busy = {start for start, _end in slots if counts.get(start, 0) >= capacity}
    return slots, busy


@router.callback_query(F.data.startswith("slot:"))
async def cb_slot(
    callback: CallbackQuery, db: Database, settings: Settings, catalog: Catalog
) -> None:
    try:
        _, draft_id, idx_raw = callback.data.split(":")
        idx = int(idx_raw)
    except (ValueError, AttributeError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    draft = await _load_draft_for_callback(callback, draft_id, db)
    if draft is None:
        return
    slots, busy = await _slots_with_capacity(db, draft.get("delivery_date"))
    if not 0 <= idx < len(slots):
        await callback.answer("Этот интервал больше недоступен", show_alert=True)
        return
    start, end = slots[idx]
    if start in busy:
        await callback.answer(
            "На этот интервал уже много заказов. Выберите другой.", show_alert=True
        )
        return
    draft["delivery_time"] = start
    draft["delivery_slot"] = f"{start}-{end}"
    await db.save_draft(draft)
    await callback.answer(f"Доставим {start}–{end}")
    await _advance_draft(
        callback.bot,
        db,
        settings,
        catalog,
        draft,
        callback.message.chat.id,
        callback.message.message_id,
    )


@router.callback_query(F.data.startswith("alt:"))
async def cb_alt(
    callback: CallbackQuery, db: Database, settings: Settings, catalog: Catalog
) -> None:
    try:
        _, draft_id, idx_raw, cid_raw = callback.data.split(":")
        idx, catalog_id = int(idx_raw), int(cid_raw)
    except (ValueError, AttributeError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    draft = await _load_draft_for_callback(callback, draft_id, db)
    if draft is None:
        return
    catalog_item = catalog.get(catalog_id)
    if catalog_item is None:
        await callback.answer("Такого нет в списке", show_alert=True)
        return
    item = next((i for i in draft["items"] if i["idx"] == idx), None)
    if item is None:
        await callback.answer("Не нашёл позицию", show_alert=True)
        return
    qty = item.get("qty")
    replacement = _item_from_catalog(catalog_item, qty, item.get("unit"))
    replacement["idx"] = idx
    draft["items"] = [
        replacement if i["idx"] == idx else i for i in draft["items"]
    ]
    await db.save_draft(draft)
    await callback.answer("Заменил")
    await _advance_draft(
        callback.bot,
        db,
        settings,
        catalog,
        draft,
        callback.message.chat.id,
        callback.message.message_id,
    )


@router.callback_query(F.data.startswith("shift:"))
async def cb_shift(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    try:
        _, action, order_raw = callback.data.split(":")
        order_id = int(order_raw)
    except (ValueError, AttributeError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    if action == "yes":
        await callback.answer("Хорошо")
        return
    await db.set_setting(
        f"date_input:{callback.message.chat.id}",
        json.dumps({"order_id": order_id}),
    )
    try:
        await callback.message.edit_text(
            "Напишите дату, например: 12.11 или «в пятницу»."
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("acc:"))
async def cb_access(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    if not await _manager_ok(callback, settings):
        return
    try:
        _, action, user_raw = callback.data.split(":")
        user_id = int(user_raw)
    except (ValueError, AttributeError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    access = "approved" if action in ("ok", "ok7") else "rejected"
    await db.set_user_access(user_id, access)
    credit_note = ""
    if action == "ok7":
        await db.set_credit_days(user_id, 7)
        credit_note = " Отсрочка платежа: 7 дней."
    try:
        await callback.bot.send_message(
            user_id,
            "✅ Доступ подтверждён." + credit_note + " Можете присылать заказы."
            if access == "approved"
            else "Доступ отклонён. Напишите менеджеру.",
        )
    except Exception:
        log.warning("Не удалось сообщить повару %s о доступе", user_id)
    try:
        await callback.message.edit_text(
            f"Доступ {'подтверждён' if access == 'approved' else 'отклонён'}."
        )
    except TelegramBadRequest:
        pass
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("rday:"))
async def cb_reminder(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    catalog: Catalog,
) -> None:
    if callback.message is None:
        await callback.answer("Сообщение недоступно", show_alert=True)
        return
    try:
        _, action, _user_raw = callback.data.split(":")
    except (ValueError, AttributeError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    if action == "off":
        await db.set_chat_state(callback.from_user.id, "remind_off", "1")
        try:
            await callback.message.edit_text("Больше не напоминаю.", reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.answer("Хорошо")
        return
    if action == "no":
        try:
            await callback.message.edit_text("Хорошо, жду заказ.", reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.answer()
        return
    usual = await db.usual_items(callback.from_user.id)
    if not usual:
        await callback.answer("Нет истории заказов", show_alert=True)
        return
    tomorrow = datetime.now(settings.timezone).date() + timedelta(days=1)
    from .drafts import _base_item

    items = []
    for entry in usual:
        catalog_item = catalog.get(entry["catalog_id"])
        if catalog_item is None:
            continue
        item = _base_item(catalog_item.name, entry["qty"], entry["unit"])
        apply_catalog(item, catalog_item)
        item["status"] = "ok"
        items.append(item)
    if not items:
        await callback.answer("Нет знакомых позиций", show_alert=True)
        return
    for index, item in enumerate(items):
        item["idx"] = index
    draft = {
        "id": secrets.token_hex(4),
        "chat_id": callback.message.chat.id,
        "user_id": callback.from_user.id,
        "user_name": callback.from_user.full_name,
        "username": callback.from_user.username,
        "raw_text": None,
        "transcript": None,
        "source": "reminder",
        "provider": "reminder",
        "delivery_date": tomorrow.isoformat(),
        "delivery_time": None,
        "comment": "",
        "items": items,
        "summary_sent": False,
        "questions": 0,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "draft",
    }
    await db.cancel_active_drafts(callback.message.chat.id)
    await db.create_draft(draft)
    try:
        await callback.message.edit_text("Собираю как обычно…", reply_markup=None)
    except TelegramBadRequest:
        pass
    panel = await callback.message.answer("Собираю как обычно…")
    await callback.answer()
    await _advance_draft(
        callback.bot, db, settings, catalog, draft, panel.chat.id, panel.message_id
    )


@router.callback_query(F.data.startswith("pundo:"))
async def cb_price_undo(
    callback: CallbackQuery, db: Database, settings: Settings, catalog: Catalog
) -> None:
    if not await _manager_ok(callback, settings):
        return
    value = callback.data.split(":", 1)[1]
    if value == "no":
        try:
            await callback.message.edit_text("Оставил текущий прайс.", reply_markup=manager_back_keyboard())
        except TelegramBadRequest:
            pass
        await callback.answer("Отмена")
        return
    try:
        version_id = int(value)
    except ValueError:
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    version = await db.get_price_version(version_id)
    if version is None or not Path(version["path"]).exists():
        await callback.answer("Файл версии не найден", show_alert=True)
        return
    try:
        restored = Catalog.load(version["path"])
    except Exception:
        await callback.answer("Файл версии повреждён", show_alert=True)
        return
    diff = diff_catalogs(catalog, restored)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = settings.data_dir / "price_versions"
    backup_dir.mkdir(parents=True, exist_ok=True)
    current_copy = backup_dir / f"catalog-{stamp}.xlsx"
    try:
        shutil.copy2(settings.catalog_path, current_copy)
    except OSError:
        current_copy = Path(version["path"])
    temp = settings.catalog_path.with_suffix(settings.catalog_path.suffix + ".tmp")
    shutil.copy2(version["path"], temp)
    os.replace(temp, settings.catalog_path)
    catalog.items = restored.items
    catalog.by_id = restored.by_id
    catalog._phrases = restored._phrases
    # Сохраняем текущий (до отката) прайс, чтобы откат тоже можно было отменить.
    await db.add_price_version(str(current_copy), "предыдущая версия", diff["old_count"])
    removed_files = await db.prune_price_versions(5)
    for path in removed_files:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
    text = (
        "↩️ Прайс возвращён к версии от "
        f"{esc(version['created_at'])}\n"
        f"Позиций: {diff['new_count']}"
    )
    try:
        await callback.message.edit_text(text, reply_markup=manager_back_keyboard())
    except TelegramBadRequest:
        pass
    await callback.answer("Вернул")


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


# ---------------------------------------------------------------------------
# G1: довес на сборке
# ---------------------------------------------------------------------------
def _parse_amount(text: str) -> float | None:
    raw = (text or "").replace(",", ".").replace(" ", "").replace("$", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return None
    if value <= 0 or value > 1_000_000:
        return None
    return round(value, 2)


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


# ---------------------------------------------------------------------------
# H5: кассовая сверка — курьер вводит фактическую сумму наличных
# ---------------------------------------------------------------------------
async def _handle_cash_amount(
    message: Message, text: str, db: Database, settings: Settings
) -> bool:
    chat_id = message.chat.id
    state = "cashwait"
    raw_order = await db.get_chat_state(chat_id, state)
    if not raw_order:
        return False
    if not _is_courier(settings, message.from_user.id, message.chat.id):
        return False
    order = await db.get_order(int(raw_order))
    if order is None:
        await db.set_chat_state(chat_id, state, "")
        return False
    amount = _parse_amount(text)
    if amount is None:
        await message.answer(
            "Не понял сумму. Пришлите число, например <code>450</code>."
        )
        return True
    await db.set_chat_state(chat_id, state, "")
    if not await db.mark_order_paid(int(order["id"]), "cash", amount):
        await message.answer("Не удалось отметить оплату: заказ уже закрыт.")
        return True
    expected = float(order.get("total") or 0)
    fresh = await db.get_order(int(order["id"])) or order
    fresh["payment_status"] = "paid"
    fresh["paid_amount"] = amount
    if fresh.get("chef_id"):
        try:
            await message.bot.send_message(
                int(fresh["chef_id"]),
                f"💵 Оплата по заказу №{order['id']} получена: {money(amount)}. Спасибо!",
            )
        except Exception:
            log.debug("Не удалось подтвердить оплату клиенту")
    courier_chat = fresh.get("courier_chat_id")
    card_id = fresh.get("courier_card_message_id")
    if courier_chat and card_id:
        try:
            await message.bot.edit_message_text(
                chat_id=int(courier_chat),
                message_id=int(card_id),
                text=format_order(fresh, "🚚 Доставка"),
                reply_markup=courier_status_keyboard(int(order["id"]), "delivering", "paid"),
            )
        except Exception:
            log.debug("Не удалось обновить карточку доставщика")
    if abs(amount - expected) > 0.01:
        manager = order.get("manager_chat_id") or settings.manager_chat_id
        if manager:
            try:
                await message.bot.send_message(
                    int(manager),
                    f"⚠️ Касса: заказ №{order['id']} — ожидалось {money(expected)}, "
                    f"курьер получил {money(amount)} (расхождение {money(amount - expected)}).",
                )
            except Exception:
                pass
        await message.answer(
            f"💵 Записал {money(amount)}. Расхождение с заказом: {money(amount - expected)}."
        )
    else:
        await message.answer(f"💵 Записал {money(amount)}. Сходится.")
    return True


@router.message(Command("cash"))
async def cmd_cash(message: Message, db: Database, settings: Settings) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    from datetime import date as _date

    day = _date.today().isoformat()
    report = await db.cash_report(day)
    lines = [f"💵 <b>Касса за {day}</b>", ""]
    lines.append(f"Заказов наличными: <b>{len(report['orders'])}</b>")
    lines.append(f"Ожидалось: <b>{money(report['expected'])}</b>")
    lines.append(f"Получено: <b>{money(report['received'])}</b>")
    delta = report["received"] - report["expected"]
    lines.append(f"Расхождение: <b>{money(delta)}</b>")
    handed = await db.get_setting(f"cash_handover:{day}", "")
    if handed:
        when = await db.get_setting(f"cash_handover_at:{day}", "")
        try:
            diff = float(handed) - float(report["expected"])
        except ValueError:
            diff = 0.0
        lines.append(
            f"Сдано курьером: <b>{money(float(handed))}</b>"
            + (f" ({when[11:16]})" if when else "")
            + f" · расхождение {money(diff)}"
        )
    else:
        lines.append("<i>Курьер ещё не сдал кассу (/handover)</i>")
    if report["diff"]:
        lines.append("")
        lines.append("<b>Расхождения по заказам</b>")
        for row in report["diff"]:
            lines.append(
                f"• №{row['id']} {esc(row.get('chef_name') or '')}: "
                f"{money(row.get('total'))} → {money(row.get('paid_amount'))}"
            )
    await message.answer("\n".join(lines))


@router.message(Command("staff"))
async def cmd_staff(message: Message, db: Database, settings: Settings) -> None:
    """Список сотрудников."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    rows = await db.list_staff()
    if not rows:
        await message.answer(
            "Сотрудников в таблице нет. Основные роли берутся из .env.\n"
            "Добавить: <code>/staff_add 123456789 courier Имя</code>"
        )
        return
    lines = ["👥 <b>Сотрудники</b>", ""]
    for row in rows:
        role = "упаковщик" if row["role"] == "manager" else "курьер"
        shift = "🟢 на смене" if row.get("on_shift") else "🌙 не на смене"
        lines.append(
            f"• {esc(row.get('name') or '')} — {role}, id <code>{row['user_id']}</code> · {shift}"
        )
    lines.append("")
    lines.append("Смена: сотрудник сам пишет /shift on или /shift off.")
    lines.append("Удалить: <code>/staff_del id</code>")
    await message.answer("\n".join(lines))


@router.message(Command("shift"))
async def cmd_shift(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Смена сотрудника: /shift on|off (D4)."""
    user_id = message.from_user.id
    rows = [row for row in await db.list_staff() if int(row["user_id"]) == user_id]
    if not rows and not _is_manager(settings, user_id, message.chat.id):
        await message.answer("Вы не в списке сотрудников. Смену ставит менеджер.")
        return
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off", "вкл", "выкл", "1", "0"):
        current = "на смене" if rows and rows[0].get("on_shift") else "не на смене"
        await message.answer(
            f"Вы {current}.\n"
            "Встать: <code>/shift on</code>, уйти: <code>/shift off</code>.\n"
            "На смене упаковщики получают новые заказы по кругу; вне смены — только в /queue."
        )
        return
    on_shift = arg in ("on", "вкл", "1")
    if not rows:
        await message.answer("Для начала менеджер должен добавить вас в сотрудники.")
        return
    await db.set_staff_shift(user_id, on_shift)
    await message.answer(
        "✅ Вы на смене: новые заказы будут приходить вам."
        if on_shift
        else "🌙 Вы не на смене: заказы останутся в /queue, карточки не придут."
    )


@router.message(Command("staff_add"))
async def cmd_staff_add(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    parts = (command.args or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[0].lstrip("-").isdigit() or parts[1] not in ("manager", "courier"):
        await message.answer(
            "Формат: <code>/staff_add 123456789 courier Имя</code>\n"
            "Роль: manager (упаковщик) или courier (доставщик)."
        )
        return
    user_id = int(parts[0])
    role = parts[1]
    name = parts[2] if len(parts) > 2 else ""
    await db.add_staff(user_id, role, name)
    await load_staff_from_db(db)
    await message.answer(f"✅ Добавлен: {role}, id <code>{user_id}</code>.")


@router.message(Command("staff_del"))
async def cmd_staff_del(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    raw = (command.args or "").strip()
    if not raw.lstrip("-").isdigit():
        await message.answer("Формат: <code>/staff_del 123456789</code>")
        return
    removed = await db.remove_staff(int(raw))
    await load_staff_from_db(db)
    await message.answer("Удалён." if removed else "Такого сотрудника нет.")


# ---------------------------------------------------------------------------
# H4: фото подтверждения (ответом на карточку заказа)
# ---------------------------------------------------------------------------
@router.message(F.photo)
async def on_photo(
    message: Message,
    db: Database,
    settings: Settings,
    catalog: Catalog,
    ai: AIClient,
) -> None:
    if not _is_staff(settings, message.from_user.id, message.chat.id):
        await _photo_order(message, db, settings, catalog, ai)
        return
    receipt_order = await db.get_chat_state(message.chat.id, "receipt")
    if receipt_order:
        await db.set_chat_state(message.chat.id, "receipt", "")
        order = await db.get_order(int(receipt_order))
        if order is not None:
            photo = message.photo[-1]
            await db.set_order_photo(int(order["id"]), photo.file_id)
            await db.mark_order_paid(
                int(order["id"]), "transfer", float(order.get("total") or 0)
            )
            fresh = await db.get_order(int(order["id"])) or order
            manager = order.get("manager_chat_id") or settings.manager_chat_id
            if manager:
                try:
                    await message.bot.send_photo(
                        int(manager),
                        photo.file_id,
                        caption=f"🏦 Чек по заказу №{order['id']} — перевод зафиксирован.",
                    )
                except Exception:
                    pass
            await message.answer(
                f"🏦 Чек принят, заказ №{order['id']} отмечен оплаченным."
            )
            return
    reply = message.reply_to_message
    if reply is None:
        await message.answer(
            "Ответьте этим фото на карточку заказа — тогда пойму, к какому заказу его приложить."
        )
        return
    order = await db.order_by_card(reply.message_id)
    if order is None:
        await message.answer("Не нашёл заказ по этой карточке.")
        return
    photo = message.photo[-1]
    await db.set_order_photo(int(order["id"]), photo.file_id)
    await message.answer(f"📷 Фото приложено к заказу №{order['id']}.")
    manager = order.get("manager_chat_id") or settings.manager_chat_id
    if manager and int(manager) != message.chat.id:
        try:
            await message.bot.send_photo(
                int(manager),
                photo.file_id,
                caption=f"📷 Фото по заказу №{order['id']}",
            )
        except Exception:
            pass


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


@router.callback_query(F.data.startswith("cour:transfer:"))
async def cb_courier_transfer(
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
    if order.get("payment_status") == "paid":
        await callback.answer("Заказ уже оплачен", show_alert=True)
        return
    await db.set_chat_state(callback.message.chat.id, "receipt", str(order_id))
    await callback.message.answer(
        f"🏦 Заказ №{order_id} на {money(order.get('total'))}. "
        "Пришлите фото чека — отмечу оплату переводом."
    )
    await callback.answer("Жду фото чека")


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


async def _handle_hot_take(
    message: Message, text: str, db: Database, settings: Settings, catalog: Catalog
) -> bool:
    """Клиент написал количество после «Взять» в горячем предложении (B14)."""
    raw = await db.get_chat_state(message.chat.id, "hot_take")
    if not raw:
        return False
    await db.set_chat_state(message.chat.id, "hot_take", "")
    try:
        catalog_id, price_raw = raw.split(":", 1)
        item = catalog.get(int(catalog_id))
        price = float(price_raw)
    except (ValueError, AttributeError):
        return False
    if item is None:
        return False
    manager = settings.manager_chat_id
    if manager:
        try:
            await message.bot.send_message(
                int(manager),
                f"🔥 {message.from_user.full_name} (id {message.from_user.id}) "
                f"хочет «{esc(item.name)}» по {money(price)}: {esc(text[:200])}\n"
                "Оформить: /for " + str(message.from_user.id) + " " + esc(text[:200]),
            )
        except Exception:
            pass
    await message.answer(
        "Передал менеджеру: он подтвердит количество и доставку. "
        "Если нужно ещё что-то — просто напишите заказ."
    )
    return True


async def _handle_claim(
    message: Message, text: str, db: Database, settings: Settings
) -> bool:
    chat_id = message.chat.id
    state = "claim"
    raw_order = await db.get_chat_state(chat_id, state)
    if not raw_order:
        return False
    await db.set_chat_state(chat_id, state, "")
    order = await db.get_order(int(raw_order))
    if order is None:
        return False
    pending_claim = await db.get_chat_state(chat_id, "claim_id")
    photo = bool(message.photo)
    body = str(text or "").strip() or ("фото приложено" if photo else "")
    if pending_claim and str(pending_claim).isdigit():
        # Претензия уже создана из опроса — дописываем детали (C5).
        await db.set_chat_state(chat_id, "claim_id", "")
        claim_id = int(pending_claim)
        await db.append_claim_text(claim_id, body)
        await message.answer(
            f"Записал в обращение №{claim_id}. Менеджер свяжется с вами."
        )
        manager = order.get("manager_chat_id") or settings.manager_chat_id
        if manager:
            try:
                caption = (
                    f"⚠️ Дополнение к претензии №{claim_id} по заказу "
                    f"№{order['id']}: {body[:600]}"
                )
                if photo:
                    await message.bot.send_photo(
                        int(manager),
                        message.photo[-1].file_id,
                        caption=caption,
                        reply_markup=claim_actions_keyboard(claim_id),
                    )
                else:
                    await message.bot.send_message(
                        int(manager),
                        caption,
                        reply_markup=claim_actions_keyboard(claim_id),
                    )
            except Exception:
                pass
        return True
    claim_id = await db.add_claim(
        int(order["id"]), message.from_user.id, body
    )
    await message.answer(
        f"Принял обращение №{claim_id} по заказу №{order['id']}. "
        "Менеджер свяжется с вами."
    )
    manager = order.get("manager_chat_id") or settings.manager_chat_id
    if manager:
        caption = (
            f"⚠️ Претензия №{claim_id} по заказу №{order['id']} "
            f"от {esc(order.get('chef_name') or '')}:\n{esc(body[:800])}"
        )
        try:
            if photo:
                await message.bot.send_photo(
                    int(manager),
                    message.photo[-1].file_id,
                    caption=caption,
                    reply_markup=claim_actions_keyboard(claim_id),
                )
            else:
                await message.bot.send_message(
                    int(manager), caption, reply_markup=claim_actions_keyboard(claim_id)
                )
        except Exception:
            pass
    log.info("Претензия №%s по заказу №%s", claim_id, order["id"])
    return True


async def _handle_claim_credit(
    message: Message, text: str, db: Database, settings: Settings
) -> bool:
    """Сумма кредит-ноты по претензии (C5-остаток).

    Вызывается после кнопки «🧾 Кредит-нота»: менеджер пишет сумму, нота
    создаётся к заказу претензии, остаток уходит в следующий счёт.
    """
    chat_id = message.chat.id
    raw = await db.get_chat_state(chat_id, "claim_credit")
    if not raw or not str(raw).isdigit():
        return False
    if not _is_manager(settings, message.from_user.id, chat_id):
        return False
    claim_id = int(raw)
    claim = await db.get_claim(claim_id)
    if claim is None:
        await db.set_chat_state(chat_id, "claim_credit", "")
        await message.answer("Претензия не найдена.")
        return True
    cleaned = str(text or "").replace(",", ".").replace("$", "").strip()
    try:
        amount = round(float(cleaned), 2)
    except ValueError:
        await message.answer(
            "Нужна сумма числом, например <code>25</code>.\n"
            f"Кредит-нота по претензии №{claim_id} не создана."
        )
        return True
    if amount <= 0:
        await message.answer("Сумма должна быть больше нуля.")
        return True
    order = await db.get_order(int(claim["order_id"]))
    if order is None:
        await db.set_chat_state(chat_id, "claim_credit", "")
        await message.answer("Заказ претензии не найден.")
        return True
    await db.set_chat_state(chat_id, "claim_credit", "")
    client_id = int(order.get("chef_id") or 0)
    before = await db.client_book_debt(client_id)
    result = await db.add_credit_note(
        client_id,
        amount,
        f"претензия №{claim_id}",
        order_id=int(order["id"]),
    )
    await db.close_claim(claim_id, "credit")
    await db.log_event(
        int(order["id"]),
        "claim",
        {"claim_id": claim_id, "decision": "credit", "amount": amount},
        actor_id=message.from_user.id,
    )
    after = await db.client_book_debt(client_id)
    lines = [
        f"🧾 Кредит-нота на {money(amount)} по претензии №{claim_id} "
        f"(заказ №{order['id']}) записана."
    ]
    if result["applied"]:
        lines.append(
            "Зачтено: "
            + ", ".join(
                f"№{target} — {money(part)}" for target, part in result["applied"]
            )
        )
    if result["left"] > 0:
        lines.append(f"💤 Остаток авансом: {money(result['left'])}")
    lines.append(
        f"Долг клиента по книге: {money(before['open'])} → <b>{money(after['open'])}</b>"
    )
    await message.answer("\n".join(lines))
    if claim.get("user_id"):
        try:
            await message.bot.send_message(
                int(claim["user_id"]),
                f"По обращению №{claim_id} (заказ №{order['id']}): оформили кредит-ноту "
                f"на {money(amount)} — сумма зачтена в счёте.",
            )
        except Exception:
            pass
    log.info("Кредит-нота %s по претензии %s", amount, claim_id)
    return True


# Отчёты и деньги — отдельный роутер.
async def _photo_order(
    message: Message,
    db: Database,
    settings: Settings,
    catalog: Catalog,
    ai: AIClient,
) -> None:
    """Клиент прислал фото списка — распознаём и разбираем как заказ."""
    from io import BytesIO

    photo = message.photo[-1]
    status = await message.answer("📷 Смотрю фото…")
    buffer = BytesIO()
    try:
        await message.bot.download(photo, destination=buffer)
    except Exception:
        await status.edit_text("Не удалось скачать фото. Пришлите ещё раз.")
        return
    data = buffer.getvalue()
    if len(data) > 8 * 1024 * 1024:
        await status.edit_text("Фото слишком большое. Пришлите поменьше.")
        return
    text = await ai.recognize_image(data, "image/jpeg")
    if not text:
        await status.edit_text(
            "Не получилось распознать список. Напишите заказ текстом или голосом."
        )
        return
    if message.caption:
        text = f"{text}\n{message.caption}"
    await status.edit_text(
        f"📷 <i>Прочитал:</i> {esc(text[:400])}\n\nСобираю заказ…"
    )
    await _process_order(message, text, "photo", None, db, settings, catalog, ai)


# ---------------------------------------------------------------------------
# I5: правка цены прямо из чата
# ---------------------------------------------------------------------------


from .handlers_reports import router as _reports_router  # noqa: E402

router.include_router(_reports_router)

# Широкие обработчики подключаются последними.
from .handlers_prices import router as _prices_router  # noqa: E402

router.include_router(_prices_router)

from .handlers_tail import router as _tail_router  # noqa: E402

router.include_router(_tail_router)
