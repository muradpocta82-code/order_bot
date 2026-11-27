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

from .pending import purge_pending_prices, take_pending_price  # noqa: E402
from .runtime import BOT_STARTED_AT, _last_error, record_error  # noqa: E402,F401

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
from .handlers_photos import (  # noqa: E402
    _handle_claim,
    _handle_claim_credit,
    _handle_hot_take,
)
from .notify import (
    ERR_ALERT_WINDOW,
    _edit_panel,
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

from .texts import (  # noqa: E402
    COURIER_HELP_TEXT,
    HELP_TEXT,
    MANAGER_HELP_TEXT,
    START_TEXT,
)


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


# I1: команды — отдельный роутер.
from .handlers_commands import router as _commands_router  # noqa: E402

router.include_router(_commands_router)

from .handlers_commands import (  # noqa: E402,F401
    _ai_usage_line,
    _courier_page,
    _orders_page,
    _remember_invite,
    cmd_cancel,
    cmd_deliveries,
    cmd_export,
    cmd_health,
    cmd_help,
    cmd_id,
    cmd_invite,
    cmd_last,
    cmd_order,
    cmd_orders,
    cmd_price,
    cmd_price_undo,
    cmd_start,
)


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


# I1: статусы заказа и списки доставок — отдельный роутер.
from .handlers_status import router as _status_router  # noqa: E402

router.include_router(_status_router)


# Панель доставщика — отдельный роутер.
from .handlers_courier import router as _courier_handlers  # noqa: E402

router.include_router(_courier_handlers)

# I1: панели менеджера и клиента — отдельный роутер.
from .handlers_panels import router as _panels_router  # noqa: E402

router.include_router(_panels_router)


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


# I1: оплата USDT в TON — отдельный роутер.
from .handlers_ton import router as _ton_router  # noqa: E402

router.include_router(_ton_router)

from .handlers_ton import poll_ton_payments  # noqa: E402,F401


# I1: довес, адрес, телефон и геопин — отдельный роутер.
from .handlers_intake import router as _intake_router  # noqa: E402

router.include_router(_intake_router)

from .handlers_intake import (  # noqa: E402,F401
    _handle_delivery_pin,
    _handle_weigh,
)

# I1: касса, сотрудники и смены — отдельный роутер.
from .handlers_shift import router as _shift_router  # noqa: E402

router.include_router(_shift_router)

from .handlers_shift import _handle_cash_amount  # noqa: E402,F401

# I1: фото, чеки и претензии — отдельный роутер.
from .handlers_photos import router as _photos_router  # noqa: E402

router.include_router(_photos_router)


# ---------------------------------------------------------------------------
# I5: правка цены прямо из чата
# ---------------------------------------------------------------------------


from .handlers_reports import router as _reports_router  # noqa: E402

router.include_router(_reports_router)

# Широкие обработчики подключаются последними.
from .handlers_prices import router as _prices_router  # noqa: E402

router.include_router(_prices_router)

# I1: оценки, опрос и курс валюты — отдельный роутер.
from .handlers_feedback import router as _feedback_router  # noqa: E402

router.include_router(_feedback_router)

from .handlers_tail import router as _tail_router  # noqa: E402

router.include_router(_tail_router)
