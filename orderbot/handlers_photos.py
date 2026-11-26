"""Фото, чеки и претензии (I1).

Вынесено из handlers.py: чтение чека по фото (G3), решение менеджера по чеку,
прикрепление фото к карточке, горячее предложение, претензии и кредит-нота
по претензии, заказ по фото списка. Подключается в handlers.py на прежнем
месте — после кассы, перед отчётами.
"""
from __future__ import annotations

import logging
from io import BytesIO

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from .ai import AIClient
from .catalog import Catalog
from .config import Settings
from .db import Database
from .formatting import esc, money
from .handlers_courier import _courier_ok, _courier_order_allowed
from .keyboards import claim_actions_keyboard, receipt_confirm_keyboard
from .receipts import parse_receipt
from .receipts import short_text as receipts_short
from .receipts import verdict as receipts_verdict
from .roles import _is_manager, _is_staff, _manager_ok
from .tuning import threshold as receipts_tolerance

log = logging.getLogger(__name__)

router = Router(name="orderbot_photos")

# ---------------------------------------------------------------------------
# H4: фото подтверждения (ответом на карточку заказа)
# ---------------------------------------------------------------------------
async def _read_receipt_photo(bot, photo, ai) -> dict | None:
    """Скачивает фото и просит модель прочитать чек (G3)."""
    try:
        buffer = BytesIO()
        await bot.download(photo, destination=buffer)
        data = await ai.read_receipt(buffer.getvalue(), "image/jpeg")
    except Exception:
        log.info("Не удалось прочитать чек по фото", exc_info=True)
        return None
    if not data:
        return None
    return parse_receipt(data)


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
            manager = order.get("manager_chat_id") or settings.manager_chat_id
            total = float(order.get("total") or 0)
            # G3: пробуем прочитать сумму чека и сверить с заказом.
            receipt = await _read_receipt_photo(message.bot, photo, ai)
            tolerance = 1.0
            try:
                tolerance = await receipts_tolerance(db, "receipt_tolerance_pct")
            except Exception:
                tolerance = 1.0
            check_verdict = (
                receipts_verdict(receipt, total, tolerance) if receipt else "unknown"
            )
            if receipt:
                await db.save_receipt_check(
                    int(order["id"]),
                    message.from_user.id,
                    receipt,
                    check_verdict,
                )
            if check_verdict == "match":
                amount = float(receipt["amount"])
                await db.mark_order_paid(int(order["id"]), "transfer", amount)
                if manager:
                    try:
                        await message.bot.send_photo(
                            int(manager),
                            photo.file_id,
                            caption=(
                                f"🏦 Чек по заказу №{order['id']} — "
                                f"перевод зафиксирован ({receipts_short(receipt, total)})."
                            ),
                        )
                    except Exception:
                        pass
                await message.answer(
                    f"🏦 Чек принят: {money(amount)} — заказ №{order['id']} оплачен."
                )
                return
            if check_verdict == "mismatch":
                caption = (
                    f"⚠️ Чек по заказу №{order['id']} не сходится с суммой заказа.\n"
                    f"{receipts_short(receipt, total)}\n"
                    "Отметить оплату по чеку?"
                )
                if manager:
                    try:
                        await message.bot.send_photo(
                            int(manager),
                            photo.file_id,
                            caption=caption,
                            reply_markup=receipt_confirm_keyboard(int(order["id"])),
                        )
                    except Exception:
                        pass
                await message.answer(
                    "Чек отправил менеджеру: сумма не сходится с заказом. "
                    "Оплата будет отмечена после подтверждения."
                )
                return
            # Сумма не распознана — прежнее поведение: доверяем доставщику.
            await db.mark_order_paid(int(order["id"]), "transfer", total)
            if manager:
                try:
                    await message.bot.send_photo(
                        int(manager),
                        photo.file_id,
                        caption=(
                            f"🏦 Чек по заказу №{order['id']} — перевод зафиксирован "
                            "(сумма не распознана, отмечено по сумме заказа)."
                        ),
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


@router.callback_query(F.data.startswith("rcpt:"))
async def cb_receipt_decision(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    """Менеджер подтверждает оплату по чеку с другой суммой (G3)."""
    if not await _manager_ok(callback, settings):
        return
    try:
        _, action, raw_order = callback.data.split(":")
        order_id = int(raw_order)
    except (IndexError, ValueError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    order = await db.get_order(order_id)
    if order is None:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    if action == "ok":
        check = await db.latest_receipt_check(order_id)
        amount = float((check or {}).get("amount") or order.get("total") or 0)
        await db.mark_order_paid(order_id, "transfer", amount)
        text = f"✅ Оплата по чеку отмечена: {money(amount)} (заказ №{order_id})."
    else:
        text = (
            f"❌ Чек по заказу №{order_id} отклонён — оплата не отмечена. "
            "Доставщику нужно проверить перевод."
        )
    try:
        await callback.message.edit_caption(caption=text)
    except Exception:
        try:
            await callback.message.edit_text(text)
        except Exception:
            pass
    await callback.answer("Записал")


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
    from .handlers import _process_order

    await _process_order(message, text, "photo", None, db, settings, catalog, ai)
