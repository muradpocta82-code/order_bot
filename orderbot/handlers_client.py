"""Заказ клиента: текст, голос, фото и уточнения черновика.

Вынесено из handlers.py. Роутер подключается там же, где раньше стояли
эти обработчики, поэтому порядок разбора сообщений не изменился.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message, User

from .ai import AIClient
from .catalog import Catalog
from .config import Settings
from .db import Database
from .draftprep import _apply_cutoff, _apply_stock, _apply_unit_choice, _apply_units
from .catalog import diff_catalogs, normalize
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
)
from .formatting import (
    esc,
    format_candidates_question,
    format_catalog,
    format_client_order,
    format_qty_question,
    money,
    split_message,
)
from .keyboards import (
    alt_keyboard,
    manager_panel_keyboard,
    price_update_keyboard,
    candidates_keyboard,
    client_back_keyboard,
    client_menu_keyboard,
    client_reply_keyboard,
    client_order_keyboard,
    edit_confirm_keyboard,
    qty_keyboard,
    retry_keyboard,
    slots_keyboard,
    unit_keyboard,
)
from .nlu import ParsedOrder, detect_intent, parse_local, parse_number
from .notify import (
    RETRY_TEXT,
    _close_order_cards,
    _detach_card,
    _drop_card_buttons,
    _manager_chat,
    _remember_retry,
    _report_failure,
    _take_retry,
)
from .onboarding import _user_access_gate
from .roles import _back_keyboard, _is_courier, _is_manager, _is_staff

# Часть функций осталась в handlers.py; берём их по требованию,
# чтобы не получилось кольцевого импорта при загрузке модуля.
def _from_handlers():
    from . import handlers

    return handlers


async def _advance_draft(*args, **kwargs):
    return await _from_handlers()._advance_draft(*args, **kwargs)


async def _edit_panel(*args, **kwargs):
    return await _from_handlers()._edit_panel(*args, **kwargs)


async def _intent_reply(*args, **kwargs):
    return await _from_handlers()._intent_reply(*args, **kwargs)


async def _process_order(*args, **kwargs):
    return await _from_handlers()._process_order(*args, **kwargs)


async def _handle_weigh(*args, **kwargs):
    return await _from_handlers()._handle_weigh(*args, **kwargs)


async def _handle_delivery_pin(*args, **kwargs):
    return await _from_handlers()._handle_delivery_pin(*args, **kwargs)


async def _handle_cash_amount(*args, **kwargs):
    return await _from_handlers()._handle_cash_amount(*args, **kwargs)


async def _handle_claim(*args, **kwargs):
    return await _from_handlers()._handle_claim(*args, **kwargs)


def _format_price_diff(*args, **kwargs):
    return _from_handlers()._format_price_diff(*args, **kwargs)
from .slots import parse_slots, slot_labels
from .state import pending_prices as _pending_prices
from .texts import MANAGER_ONLY_TEXT, RATE_LIMIT_TEXT
from .stt import Transcriber

log = logging.getLogger(__name__)
router = Router(name="orderbot_client")


@router.message(F.document)
async def on_document(
    message: Message, bot: Bot, settings: Settings, catalog: Catalog
) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Файл со списком может присылать только менеджер.")
        return
    document = message.document
    if document is None:
        return
    name = (document.file_name or "").lower()
    if not name.endswith(".xlsx"):
        await message.answer("Нужен файл .xlsx со списком товаров.")
        return

    token = secrets.token_hex(4)
    path = settings.data_dir / f"catalog_upload_{token}.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        file = await bot.get_file(document.file_id)
        await bot.download_file(file.file_path, path)
        new_catalog = Catalog.load(path)
    except Exception:
        log.exception("Не удалось прочитать новый прайс")
        path.unlink(missing_ok=True)
        await message.answer(
            "Не получилось прочитать файл. Нужны колонки «Название» и «Цена». "
            "Старый список не тронул."
        )
        return

    diff = diff_catalogs(catalog, new_catalog)
    _pending_prices[token] = {
        "catalog": new_catalog,
        "path": str(path),
        "created": time.time(),
    }
    await message.answer(
        _format_price_diff(diff), reply_markup=price_update_keyboard(token)
    )


@router.message(F.voice)
async def on_voice(
    message: Message,
    bot: Bot,
    db: Database,
    settings: Settings,
    catalog: Catalog,
    ai: AIClient,
    transcriber: Transcriber,
) -> None:
    voice = message.voice
    if voice is None:
        return
    if _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer(MANAGER_ONLY_TEXT, reply_markup=manager_panel_keyboard())
        return
    if await db.count_orders_since(message.from_user.id, 60) >= 20:
        await message.answer(RATE_LIMIT_TEXT)
        return
    await db.set_chat_state(message.chat.id, "qty_input", "")
    if voice.duration and voice.duration > 600:
        await message.answer("Голосовое длиннее 10 минут. Запишите короче.")
        return
    if voice.file_size and voice.file_size > settings.max_voice_mb * 1024 * 1024:
        await message.answer(
            f"Файл больше {settings.max_voice_mb} МБ. Запишите короче."
        )
        return

    status = await message.answer("🎧 Слушаю голосовое…")
    voice_dir = settings.data_dir / "voice"
    voice_dir.mkdir(parents=True, exist_ok=True)
    path = voice_dir / f"{voice.file_unique_id}-{message.message_id}.ogg"
    try:
        file = await bot.get_file(voice.file_id)
        await bot.download_file(file.file_path, destination=path)
        result = await transcriber.transcribe(path)
    except Exception:
        log.exception("Ошибка загрузки/распознавания голосового")
        await status.edit_text(
            "Не получилось разобрать голосовое. Напишите текстом."
        )
        return
    finally:
        path.unlink(missing_ok=True)

    if not result.text:
        await status.edit_text(
            "Не расслышал. Напишите заказ текстом — разберу его."
        )
        return

    # Коррекция распознавания по словарю прайса: «парамезан» → «пармезан».
    corrected = correct_voice_text(result.text, catalog)
    result.text = corrected

    # Голосовое не может быть ответом про заведение — только текстом.
    voice_user = await db.ensure_user(
        message.from_user.id,
        message.from_user.full_name,
        message.from_user.username,
    )
    if voice_user.get("access") == "rejected":
        await status.edit_text("Доступ закрыт. Напишите менеджеру.")
        return
    if (
        voice_user.get("access") != "approved"
        or not voice_user.get("venue")
        or not voice_user.get("phone")
        or not voice_user.get("address")
    ):
        await status.edit_text(
            "Сначала заполните профиль в /start: заведение, телефон и адрес. "
            "После подтверждения можно заказывать голосом."
        )
        return

    intent = detect_intent(result.text)
    if intent is not None:
        reply = await _intent_reply(
            intent, message.from_user.id, message.chat.id, db, settings, catalog
        )
        if reply is not None:
            await status.edit_text(reply[0], reply_markup=reply[1])
            return

    await status.edit_text(f"<i>{esc(result.text)}</i>\n\nЧитаю заказ…")
    await _process_order(
        message=message,
        text=result.text,
        source="voice",
        transcript=result.text,
        db=db,
        settings=settings,
        catalog=catalog,
        ai=ai,
        status_message=status,
    )


async def _handle_date_input(
    message: Message, db: Database, settings: Settings, catalog: Catalog
) -> bool:
    """Повар выбрал «Выбрать дату» и прислал дату текстом."""
    raw = await db.get_chat_state(message.chat.id, "date_input")
    if not raw:
        return False
    try:
        pending = json.loads(raw)
        order_id = int(pending["order_id"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        await db.set_chat_state(message.chat.id, "date_input", "")
        return False
    parsed = parse_local(message.text or "", settings.timezone)
    if parsed.delivery_date is None:
        await message.answer("Не понял дату. Напишите, например: 12.11 или «в пятницу».")
        return True
    await db.set_chat_state(message.chat.id, "date_input", "")
    order = await db.get_order(order_id)
    if order is None or order.get("status") != "new":
        await message.answer("Заказ уже нельзя перенести.")
        return True
    await db.update_order_delivery(
        order_id, parsed.delivery_date.isoformat(), parsed.delivery_time
    )
    await message.answer(
        f"Перенёс заказ №{order_id} на {parsed.delivery_date.strftime('%d.%m.%Y')}."
    )
    return True


def _item_from_catalog(catalog_item, qty: float | None, unit: str | None) -> dict:
    from .drafts import _base_item

    item = _base_item(catalog_item.name, qty, unit or catalog_item.unit)
    apply_catalog(item, catalog_item)
    item["status"] = "ok" if qty is not None else "needs_qty"
    item["score"] = 100.0
    # Позиции из БД хранят сумму в total; новая строка — та же схема.
    item["total"] = item.get("sum")
    return item


async def _handle_text_edit(
    message: Message, text: str, db: Database, settings: Settings, catalog: Catalog
) -> bool:
    """«добавь…», «убери…», «вместо … сделай …» — правка последнего заказа."""
    edit = parse_edit(text)
    if edit is None:
        return False
    order = await db.last_order_for_user(message.from_user.id)
    if order is None:
        await message.answer("Пока нет заказа, который можно поправить.")
        return True
    if order.get("status") == "accepted":
        await message.answer("Заказ уже в работе, позвоните менеджеру")
        return True
    if order.get("status") != "new":
        await message.answer("Заказ уже закрыт, менять нечего.")
        return True

    items = [dict(item) for item in order.get("items", []) if item.get("status") != "skipped"]
    action = edit.get("action")
    if action == "add":
        parsed = parse_local(edit.get("text", ""), settings.timezone)
        if not parsed.items:
            await message.answer("Не понял, что добавить. Напишите: «добавь молоко 5 л».")
            return True
        for parsed_item in parsed.items:
            match = catalog.match(parsed_item.raw, limit=1, min_score=55)
            if not match:
                await message.answer(f"Не нашёл в списке: «{esc(parsed_item.raw)}».")
                return True
            items.append(
                _item_from_catalog(match[0].item, parsed_item.qty, parsed_item.unit)
            )
    elif action == "remove":
        target = find_order_item(order, edit.get("text", ""), catalog)
        if target is None:
            await message.answer("Такой позиции в заказе нет.")
            return True
        items = [i for i in items if i.get("id") != target.get("id")]
    elif action == "replace":
        target = find_order_item(order, edit.get("old", ""), catalog)
        if target is None:
            await message.answer("Такой позиции в заказе нет.")
            return True
        parsed = parse_local(edit.get("new", ""), settings.timezone)
        if not parsed.items:
            await message.answer("Не понял новое количество.")
            return True
        catalog_item = catalog.get(target.get("catalog_id"))
        if catalog_item is not None:
            replacement = _item_from_catalog(
                catalog_item, parsed.items[0].qty, parsed.items[0].unit
            )
        else:
            replacement = dict(target)
            replacement["qty"] = parsed.items[0].qty
            replacement["unit"] = parsed.items[0].unit
        items = [
            replacement if i.get("id") == target.get("id") else i for i in items
        ]
    else:
        return False

    if not items:
        await message.answer("В заказе не осталось позиций. Отмените заказ и пришлите новый.")
        return True

    total = round(
        sum(
            float(i.get("total") if i.get("total") is not None else i.get("sum") or 0)
            for i in items
        ),
        2,
    )
    await db.set_chat_state(
        message.chat.id,
        "edit",
        json.dumps({"order_id": int(order["id"]), "items": items, "total": total}, ensure_ascii=False),
    )
    preview = (
        f"Заказ №{order['id']} будет так:\n\n"
        + items_summary(items)
        + f"\n\n💰 <b>Итого: ${total:,.2f}</b>"
    )
    await message.answer(preview, reply_markup=edit_confirm_keyboard())
    return True


async def _handle_client_button(
    message: Message, text: str, db: Database, settings: Settings, catalog: Catalog
) -> bool:
    """Постоянные кнопки клиента: «Мои заказы», «Как обычно», «Менеджер», «Прайс»."""
    if _is_manager(settings, message.from_user.id, message.chat.id) or _is_courier(
        settings, message.from_user.id, message.chat.id
    ):
        return False

    waiting = await db.get_chat_state(message.chat.id, "client_message")
    if waiting is not None and waiting != "":
        await db.clear_chat_state(message.chat.id, "client_message")
        manager = settings.manager_chat_id
        if manager:
            try:
                await message.bot.send_message(
                    int(manager),
                    f"💬 Сообщение от клиента "
                    f"{message.from_user.full_name}"
                    f" (id {message.from_user.id}):\n\n{text}",
                )
            except Exception:
                pass
        await message.answer("Передал менеджеру, он ответит здесь же.")
        return True

    if text == "📦 Мои заказы":
        orders = await db.orders_for_client(message.from_user.id, 5)
        if not orders:
            await message.answer(
                "Заказов пока нет. Напишите, что нужно привезти, — например: "
                "«2 кг помидоров, 1 кг лука, завтра»."
            )
            return True
        status_names = {
            "new": "ждёт сборки",
            "accepted": "собираем",
            "ready": "собран",
            "delivering": "в пути",
            "done": "доставлен",
            "cancelled": "отменён",
            "failed": "не доставлен",
        }
        lines = ["📦 <b>Ваши последние заказы</b>", ""]
        for order in orders:
            status = status_names.get(str(order.get("status")), str(order.get("status")))
            lines.append(
                f"№{order['id']} · {money(order.get('total'))} · {status}\n"
                f"   доставка {str(order.get('delivery_date') or '—')[:10]}"
            )
        lines.append("")
        lines.append("Повторить заказ можно кнопкой «🔁 Как обычно».")
        await message.answer("\n".join(lines))
        return True

    if text == "🔁 Как обычно":
        usual = await db.usual_items(message.from_user.id)
        if not usual:
            await message.answer(
                "Пока не знаю ваших обычных позиций — сделайте первый заказ, "
                "дальше смогу повторять его одной кнопкой."
            )
            return True
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Собрать как обычно",
                        callback_data=f"rday:yes:{message.from_user.id}",
                    )
                ],
                [
                    InlineKeyboardButton(text="✏️ Изменить", callback_data="cl:menu"),
                    InlineKeyboardButton(text="📖 Каталог", callback_data="cl:price"),
                ],
            ]
        )
        names = ", ".join(str(entry.get("name")) for entry in usual[:6])
        await message.answer(
            f"Повторим прошлый заказ? Обычно вы берёте: {names}.", reply_markup=keyboard
        )
        return True

    if text == "☎️ Менеджер":
        await db.set_chat_state(message.chat.id, "client_message", "1")
        await message.answer(
            "Напишите сообщение для менеджера одним текстом — он ответит здесь же."
        )
        return True

    if text == "📖 Прайс":
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Список товаров", callback_data="cl:price")]
            ]
        )
        await message.answer("Прайс с ценами:", reply_markup=keyboard)
        return True
    return False


async def _handle_handover(
    message: Message, text: str, db: Database, settings: Settings
) -> bool:
    """Курьер сдаёт кассу: сумма сравнивается с ожидаемой по наличным за день."""
    day = await db.get_chat_state(message.chat.id, "cash_handover")
    if not day:
        return False
    raw = text.replace(" ", "").replace(",", ".").replace("$", "").strip()
    try:
        amount = round(float(raw), 2)
    except ValueError:
        await message.answer(
            "Пришлите сумму числом, например 250.00 (или /cancel для отмены)."
        )
        return True
    await db.clear_chat_state(message.chat.id, "cash_handover")
    report = await db.cash_report(day)
    expected = float(report["expected"])
    delta = amount - expected
    await db.set_setting(f"cash_handover:{day}", f"{amount:.2f}")
    await db.set_setting(f"cash_handover_at:{day}", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines = [
        f"💵 Касса за {day} сдана: {money(amount)}",
        f"Ожидалось по заказам: {money(expected)}",
        f"Расхождение: {money(delta)}",
    ]
    if abs(delta) > 0.01:
        lines.append("⚠️ Есть расхождение — менеджер увидит его в /cash.")
    await message.answer("\n".join(lines))
    manager = settings.manager_chat_id
    if manager:
        try:
            await message.bot.send_message(
                int(manager),
                f"💵 Курьер сдал кассу за {day}: {money(amount)} "
                f"(ожидалось {money(expected)}, расхождение {money(delta)}).",
            )
        except Exception:
            pass
    return True


async def _handle_for_client(
    message: Message,
    text: str,
    db: Database,
    settings: Settings,
    catalog: Catalog,
    ai: AIClient,
) -> bool:
    """Заказ за клиента: менеджер принял звонок и вводит текст как этот клиент."""
    raw = await db.get_chat_state(message.chat.id, "for_client")
    if not raw or not str(raw).strip().isdigit():
        return False
    user_id = int(str(raw).strip())
    user = await db.get_user(user_id)
    await db.clear_chat_state(message.chat.id, "for_client")
    if user is None:
        await message.answer("Клиент не найден — режим «заказ за клиента» выключен.")
        return True
    fake_user = User(
        id=user_id,
        is_bot=False,
        first_name=(user.get("name") or "Клиент")[:64],
        username=user.get("username") or None,
    )
    proxy = message.model_copy(update={"from_user": fake_user})
    await message.answer(
        f"📞 Оформляю заказ за {user.get('name') or user_id}. "
        "Дальше как обычно: уточнения и подтверждение придут сюда."
    )
    await _process_order(
        message=proxy,
        text=text,
        source="manager",
        transcript=None,
        db=db,
        settings=settings,
        catalog=catalog,
        ai=ai,
    )
    return True


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(
    message: Message,
    db: Database,
    settings: Settings,
    catalog: Catalog,
    ai: AIClient,
) -> None:
    text = (message.text or "").strip()
    if await _handle_client_button(message, text, db, settings, catalog):
        return
    if await _handle_delivery_pin(message, text, db, settings):
        return
    if await _handle_handover(message, text, db, settings):
        return
    if await _handle_cash_amount(message, text, db, settings):
        return
    if await _handle_claim(message, text, db, settings):
        return
    if len(text) < 2:
        await message.answer("Напишите заказ, например: «2 кг куриного филе, завтра».")
        return
    if _is_manager(settings, message.from_user.id, message.chat.id):
        if await _handle_for_client(message, text, db, settings, catalog, ai):
            return
        if await _handle_weigh(message, text, db, settings):
            return
        await message.answer(MANAGER_ONLY_TEXT, reply_markup=manager_panel_keyboard())
        return
    if len(text) > 3000:
        await message.answer("Слишком длинно. Разбейте заказ на части.")
        return

    if await _user_access_gate(message, db, settings, catalog):
        return
    if await _handle_date_input(message, db, settings, catalog):
        return
    if await _handle_text_edit(message, text, db, settings, catalog):
        return

    intent = detect_intent(text)
    if intent is not None:
        reply = await _intent_reply(
            intent, message.from_user.id, message.chat.id, db, settings, catalog
        )
        if reply is not None:
            await message.answer(reply[0], reply_markup=reply[1])
            return

    if await db.count_orders_since(message.from_user.id, 60) >= 20:
        await message.answer(RATE_LIMIT_TEXT)
        return

    pending_raw = await db.get_chat_state(message.chat.id, "qty_input")
    if pending_raw and await _handle_qty_text(
        message, pending_raw, db, settings, catalog
    ):
        return

    await _process_order(
        message=message, text=text, source="text", transcript=None,
        db=db, settings=settings, catalog=catalog, ai=ai,
    )


@router.message(F.web_app_data)
async def on_webapp_order(
    message: Message,
    db: Database,
    settings: Settings,
    catalog: Catalog,
    ai: AIClient,
) -> None:
    """Заказ, собранный в мини-приложении каталога.

    Мини-приложение присылает строку вида «2 кг Куриное филе, 1 пакет Моцарелла»,
    дальше работает обычный разбор заказа — как будто повар написал текстом.
    """
    raw = (message.web_app_data.data or "").strip() if message.web_app_data else ""
    text = ""
    try:
        payload = json.loads(raw)
        items = str(payload.get("items") or "").strip()
        comment = str(payload.get("comment") or "").strip()
        text = ", ".join(part for part in (items, comment) if part)
    except (json.JSONDecodeError, AttributeError):
        text = raw
    if not text:
        await message.answer("Каталог прислал пустой заказ. Попробуйте ещё раз.")
        return
    if await _user_access_gate(message, db, settings, catalog):
        return
    if await db.count_orders_since(message.from_user.id, 60) >= 20:
        await message.answer(RATE_LIMIT_TEXT)
        return
    await _process_order(
        message=message,
        text=text,
        source="catalog",
        transcript=None,
        db=db,
        settings=settings,
        catalog=catalog,
        ai=ai,
    )


async def _handle_qty_text(
    message: Message,
    pending_raw: str,
    db: Database,
    settings: Settings,
    catalog: Catalog,
) -> bool:
    try:
        pending = json.loads(pending_raw)
    except json.JSONDecodeError:
        await db.set_chat_state(message.chat.id, "qty_input", "")
        return False
    draft = await db.get_draft(pending.get("draft_id", ""))
    if draft is None or draft.get("status") != "draft":
        await db.set_chat_state(message.chat.id, "qty_input", "")
        return False
    panel_id = int(pending["message_id"]) if pending.get("message_id") else None

    async def show_panel(text: str) -> None:
        if panel_id is None:
            await message.answer(text)
        else:
            await _edit_panel(message.bot, message.chat.id, panel_id, text)

    qty = parse_number(message.text or "")
    if qty is None:
        words = re.findall(r"[а-яa-z]+", (message.text or "").lower())
        if len(words) >= 2:
            # похоже на новый заказ, а не на ответ про количество
            await db.set_chat_state(message.chat.id, "qty_input", "")
            return False
        await show_panel("Напишите количество цифрой, например 7.")
        return True
    if qty <= 0 or qty > 100000:
        await show_panel("Количество должно быть больше нуля.")
        return True
    item = set_item_qty(draft, int(pending["idx"]), qty, catalog)
    await db.set_chat_state(message.chat.id, "qty_input", "")
    if item is None:
        await show_panel("Не нашёл эту позицию. Пришлите заказ заново.")
        return True
    await db.save_draft(draft)
    if panel_id is None:
        panel = await message.answer("Секунду…")
        panel_id = panel.message_id
    await _advance_draft(
        message.bot,
        db,
        settings,
        catalog,
        draft,
        message.chat.id,
        panel_id,
    )
    return True


# ---------------------------------------------------------------------------
# callbacks
# ---------------------------------------------------------------------------


async def _load_draft_for_callback(
    callback: CallbackQuery, draft_id: str, db: Database
) -> dict | None:
    draft = await db.get_draft(draft_id)
    if draft is None:
        await callback.answer("Заказ не найден", show_alert=True)
        return None
    if callback.message is None or draft["chat_id"] != callback.message.chat.id:
        await callback.answer("Это не ваш заказ", show_alert=True)
        return None
    if draft.get("user_id") != callback.from_user.id:
        await callback.answer(
            "Это не ваш заказ", show_alert=True
        )
        return None
    if draft.get("status") != "draft":
        await callback.answer("Этот заказ уже отправлен", show_alert=True)
        return None
    return draft


@router.callback_query(F.data.startswith("pick:"))
async def cb_pick(
    callback: CallbackQuery, db: Database, settings: Settings, catalog: Catalog
) -> None:
    try:
        _, draft_id, idx_raw, cid_raw = callback.data.split(":")
        idx, cid = int(idx_raw), int(cid_raw)
    except (ValueError, AttributeError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    draft = await _load_draft_for_callback(callback, draft_id, db)
    if draft is None:
        return
    catalog_item = catalog.get(cid)
    if catalog_item is None:
        await callback.answer("Такого нет в списке", show_alert=True)
        return
    item = resolve_item(draft, idx, catalog_item)
    if item is None:
        await callback.answer("Не нашёл эту позицию", show_alert=True)
        return
    await db.save_alias(
        normalize(item.get("raw") or ""), catalog_item.id, callback.from_user.id
    )
    await db.save_draft(draft)
    await callback.answer("Хорошо")
    await _advance_draft(
        callback.bot,
        db,
        settings,
        catalog,
        draft,
        callback.message.chat.id,
        callback.message.message_id,
    )


@router.callback_query(F.data.startswith("none:"))
async def cb_none(
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
    item = mark_not_in_catalog(draft, idx)
    if item is None:
        await callback.answer("Не нашёл эту позицию", show_alert=True)
        return
    await db.record_missing(item.get("raw") or "")
    await db.save_draft(draft)
    await callback.answer("Хорошо")
    await _advance_draft(
        callback.bot,
        db,
        settings,
        catalog,
        draft,
        callback.message.chat.id,
        callback.message.message_id,
    )


@router.callback_query(F.data.startswith("skip:"))
async def cb_skip(
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
    item = mark_skipped(draft, idx)
    if item is None:
        await callback.answer("Не нашёл эту позицию", show_alert=True)
        return
    await db.save_draft(draft)
    await callback.answer("Хорошо")
    await _advance_draft(
        callback.bot,
        db,
        settings,
        catalog,
        draft,
        callback.message.chat.id,
        callback.message.message_id,
    )


@router.callback_query(F.data.startswith("qty:"))
async def cb_qty(
    callback: CallbackQuery, db: Database, settings: Settings, catalog: Catalog
) -> None:
    try:
        _, draft_id, idx_raw, qty_raw = callback.data.split(":")
        idx, qty = int(idx_raw), float(qty_raw)
    except (ValueError, AttributeError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    draft = await _load_draft_for_callback(callback, draft_id, db)
    if draft is None:
        return
    item = set_item_qty(draft, idx, qty, catalog)
    if item is None:
        await callback.answer("Не нашёл эту позицию", show_alert=True)
        return
    await db.save_draft(draft)
    await callback.answer("Хорошо")
    await _advance_draft(
        callback.bot,
        db,
        settings,
        catalog,
        draft,
        callback.message.chat.id,
        callback.message.message_id,
    )


@router.callback_query(F.data.startswith("qtytext:"))
async def cb_qtytext(callback: CallbackQuery, db: Database) -> None:
    try:
        _, draft_id, idx_raw = callback.data.split(":")
        idx = int(idx_raw)
    except (ValueError, AttributeError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    draft = await _load_draft_for_callback(callback, draft_id, db)
    if draft is None:
        return
    await db.set_setting(
        f"qty_input:{draft['chat_id']}",
        json.dumps(
            {
                "draft_id": draft_id,
                "idx": idx,
                "message_id": callback.message.message_id if callback.message else None,
            }
        ),
    )
    await _edit_panel(
        callback.bot,
        callback.message.chat.id,
        callback.message.message_id,
        "Напишите количество цифрой.",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm:"))
async def cb_confirm(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    catalog: Catalog,
) -> None:
    # Старые кнопки «Подтвердить» из уже отправленных сообщений.
    draft_id = callback.data.split(":", 1)[1]
    draft = await _load_draft_for_callback(callback, draft_id, db)
    if draft is None:
        return
    if not all_resolved(draft):
        await callback.answer("Сначала ответьте на вопросы", show_alert=True)
        return
    await callback.answer("Отправляю")
    await _advance_draft(
        callback.bot,
        db,
        settings,
        catalog,
        draft,
        callback.message.chat.id,
        callback.message.message_id,
    )


@router.callback_query(F.data.startswith("dcancel:"))
async def cb_cancel(callback: CallbackQuery, db: Database) -> None:
    draft_id = callback.data.split(":", 1)[1]
    draft = await _load_draft_for_callback(callback, draft_id, db)
    if draft is None:
        return
    draft["status"] = "cancelled"
    await db.save_draft(draft)
    try:
        await callback.message.edit_text("Заказ отменён.", reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.answer("Отменил")


@router.callback_query(F.data == "retry:order")
async def cb_retry_order(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    catalog: Catalog,
    ai: AIClient,
) -> None:
    if callback.message is None:
        await callback.answer("Сообщение недоступно", show_alert=True)
        return
    data = await _take_retry(db, callback.message.chat.id)
    if data is None:
        await callback.answer("Уже нечего повторять", show_alert=True)
        return
    await callback.answer("Повторяю")
    fake = Message(
        message_id=callback.message.message_id,
        date=datetime.now(settings.timezone),
        chat=callback.message.chat,
        from_user=callback.from_user,
        text=data["text"],
    ).as_(callback.bot)
    await _process_order(
        fake,
        text=data["text"],
        source="text",
        transcript=data.get("transcript"),
        db=db,
        settings=settings,
        catalog=catalog,
        ai=ai,
    )


@router.callback_query(F.data.startswith("usual:"))
async def cb_usual(
    callback: CallbackQuery, db: Database, settings: Settings, catalog: Catalog
) -> None:
    if callback.message is None:
        await callback.answer("Сообщение недоступно", show_alert=True)
        return
    try:
        catalog_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    item = catalog.get(catalog_id)
    if item is None:
        await callback.answer("Такого нет в списке", show_alert=True)
        return
    usual = await db.usual_for_item(callback.from_user.id, catalog_id)
    if usual is None:
        await callback.answer("Пока нет истории по этой позиции", show_alert=True)
        return
    tomorrow = datetime.now(settings.timezone).date() + timedelta(days=1)
    await db.cancel_active_drafts(callback.message.chat.id)
    draft = draft_from_usual(
        item,
        usual["qty"],
        usual["unit"],
        callback.message.chat.id,
        callback.from_user.id,
        callback.from_user.full_name,
        callback.from_user.username,
        tomorrow,
    )
    await db.create_draft(draft)
    await callback.answer("Собираю")
    panel = await callback.message.answer("Собираю заказ…")
    await _advance_draft(
        callback.bot, db, settings, catalog, draft, panel.chat.id, panel.message_id
    )


@router.callback_query(F.data.startswith("repeat:"))
async def cb_repeat(
    callback: CallbackQuery, db: Database, settings: Settings, catalog: Catalog
) -> None:
    if callback.message is None:
        await callback.answer("Сообщение недоступно", show_alert=True)
        return
    try:
        order_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    order = await db.get_order(order_id)
    if order is None or order.get("chef_id") != callback.from_user.id:
        await callback.answer("Это не ваш заказ", show_alert=True)
        return
    tomorrow = datetime.now(settings.timezone).date() + timedelta(days=1)
    await db.cancel_active_drafts(callback.message.chat.id)
    draft = draft_from_order(
        order,
        callback.message.chat.id,
        callback.from_user.id,
        callback.from_user.full_name,
        callback.from_user.username,
        tomorrow,
    )
    await db.create_draft(draft)
    await callback.answer("Повторяю")
    panel = await callback.message.answer("Повторяю заказ…")
    await _advance_draft(
        callback.bot, db, settings, catalog, draft, panel.chat.id, panel.message_id
    )


@router.callback_query(F.data.startswith("ccancel:"))
async def cb_client_cancel(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    if callback.message is None:
        await callback.answer("Сообщение недоступно", show_alert=True)
        return
    try:
        order_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    order = await db.get_order(order_id)
    if order is None or order.get("chef_id") != callback.from_user.id:
        await callback.answer("Это не ваш заказ", show_alert=True)
        return
    if order.get("status") != "new":
        text = (
            "Заказ уже в работе, позвоните менеджеру"
            if order.get("status") == "accepted"
            else "Заказ уже закрыт."
        )
        await callback.answer(text, show_alert=True)
        return
    if not await db.update_order_status(order_id, "cancelled"):
        await callback.answer("Заказ уже закрыт.", show_alert=True)
        return
    order["status"] = "cancelled"
    manager = _manager_chat(settings)
    await _close_order_cards(
        callback.bot, order, f"❌ Заказ №{order_id} отменён", manager
    )
    if manager is not None:
        try:
            await callback.bot.send_message(
                manager, f"Повар отменил заказ №{order_id}"
            )
        except Exception:
            log.warning("Не удалось сообщить менеджеру об отмене заказа №%s", order_id)
    try:
        await callback.bot.send_message(
            callback.message.chat.id, f"Заказ №{order_id} отменён"
        )
    except Exception:
        log.debug("Не удалось отправить короткое сообщение об отмене", exc_info=True)
    await callback.answer("Отменил")


@router.callback_query(F.data.startswith("priceoff:"))
async def cb_price_off(callback: CallbackQuery, db: Database) -> None:
    """Клиент отказался от утренних сообщений с ценами."""
    if callback.message is None:
        await callback.answer("Сообщение недоступно", show_alert=True)
        return
    await db.set_setting(f"price_optout:{callback.from_user.id}", "1")
    try:
        await callback.message.edit_text(
            "Хорошо, больше не присылаю цены по утрам. "
            "Прайс всегда доступен по команде /price.",
            reply_markup=None,
        )
    except TelegramBadRequest:
        pass
    await callback.answer("Отключено")


@router.message(Command("report"))
async def cmd_client_report(message: Message, db: Database, settings: Settings) -> None:
    """Кабинет клиента: сколько потратил и что чаще всего берёт."""
    if _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Менеджеру — /analytics, /margin, /ops.")
        return
    data = await db.client_usage(message.from_user.id, 30)
    if not data["orders"]:
        await message.answer(
            "За последние 30 дней заказов не было. Напишите, что нужно привезти."
        )
        return
    lines = ["📊 <b>Ваши закупки за 30 дней</b>", ""]
    lines.append(f"Заказов: <b>{data['orders']}</b> на <b>{money(data['sum'])}</b>")
    if data["orders"]:
        lines.append(f"Средний заказ: {money(data['sum'] / data['orders'])}")
    lines.append("")
    lines.append("<b>Что чаще всего брали</b>")
    for row in data["items"][:8]:
        lines.append(
            f"• {esc(str(row.get('name')))} — {row['q']:g} {esc(str(row.get('unit') or ''))} "
            f"на {money(row['s'])}"
        )
    lines.append("")
    lines.append("Повторить прошлый заказ: кнопка «🔁 Как обычно» или /start.")
    await message.answer("\n".join(lines))
