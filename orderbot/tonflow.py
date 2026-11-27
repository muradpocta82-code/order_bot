"""Оплата USDT в сети TON: счёт, QR и проверка входящих переводов.

Вынесено из handlers.py: счёт доставщику, уведомления об оплате и фоновый
опрос TonAPI каждые ton_poll_seconds секунд.
"""
from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile

from .config import Settings
from .db import Database
from .formatting import esc, format_order
from .keyboards import courier_status_keyboard
from .notify import _send_with_retry
from .route import order_point
from .tonpay import (
    TonPay,
    TonPayError,
    match_payment,
    nano_to_text,
    payment_link,
    qr_png,
    usdt_to_nano,
)

log = logging.getLogger(__name__)


def ton_client(settings: Settings) -> TonPay | None:
    if not (settings.tonapi_key and settings.ton_wallet and settings.usdt_jetton):
        return None
    return TonPay(settings.tonapi_key, settings.ton_wallet, settings.usdt_jetton)


async def send_ton_invoice(
    bot: Bot, db: Database, settings: Settings, order: dict[str, Any]
) -> bool:
    """Выставляет счёт USDT и присылает доставщику QR и ссылку."""
    client = ton_client(settings)
    order_id = int(order.get("id") or 0)
    chat_id = order.get("courier_chat_id") or settings.courier_chat_id
    if client is None or not order_id or not chat_id:
        return False
    amount_nano = usdt_to_nano(float(order.get("total") or 0))
    if amount_nano < 1000:
        log.warning("USDT: заказ №%s — сумма меньше 0.001 USDT", order_id)
        return False
    comment = f"order_{order_id}"
    await db.create_ton_payment(
        order_id, comment, amount_nano, settings.ton_window_minutes
    )
    link = payment_link(
        settings.ton_wallet, settings.usdt_jetton, amount_nano, comment
    )
    caption = (
        "🛰 <b>Оплата USDT (TON)</b>\n\n"
        f"Заказ №{order_id} — <b>{nano_to_text(amount_nano)} USDT</b>\n"
        f"Комментарий: <code>{comment}</code>\n\n"
        "Покажите QR клиенту или отправьте ссылку:\n"
        f"<code>{esc(link)}</code>\n\n"
        f"Окно оплаты — {settings.ton_window_minutes} минут."
    )
    try:
        await bot.send_photo(
            int(chat_id),
            BufferedInputFile(qr_png(link), filename=f"order_{order_id}.png"),
            caption=caption,
        )
    except Exception:
        log.exception("USDT: не удалось отправить QR по заказу №%s", order_id)
        try:
            await bot.send_message(int(chat_id), caption)
        except Exception:
            return False
    chef_id = order.get("chef_id")
    if chef_id and int(chef_id) != int(chat_id):
        client_caption = (
            "🛰 <b>Оплата заказа №%s</b>\n\n"
            "К оплате: <b>%s USDT</b> (сеть TON)\n"
            "Покажите этот QR доставщику или оплатите сами.\n"
            "Комментарий: <code>%s</code>\n\n"
            "<i>Для перевода нужен небольшой запас TON на комиссию. "
            "Если кошелёк не передаёт комментарий — оплата не определится "
            "автоматически, скажите доставщику.</i>"
        ) % (order_id, nano_to_text(amount_nano), comment)
        try:
            await bot.send_photo(
                int(chef_id),
                BufferedInputFile(qr_png(link), filename=f"order_{order_id}.png"),
                caption=client_caption,
            )
        except Exception:
            log.warning("USDT: не удалось отправить счёт клиенту №%s", order_id)
    log.info(
        "USDT: счёт по заказу №%s на %s (%s)",
        order_id,
        nano_to_text(amount_nano),
        comment,
    )
    return True


async def _usdt_paid_notifications(
    bot: Bot,
    db: Database,
    settings: Settings,
    order: dict[str, Any],
    amount_nano: int,
    late: bool = False,
) -> None:
    order_id = int(order.get("id") or 0)
    courier_chat = order.get("courier_chat_id") or settings.courier_chat_id
    if courier_chat:
        try:
            await _send_with_retry(
                lambda: bot.send_message(
                    int(courier_chat),
                    f"🛰 Оплачено {nano_to_text(amount_nano)} USDT по заказу №{order_id}."
                    + (" Платёж пришёл после истечения окна." if late else "")
                    + " Можно отдавать продукты.",
                )
            )
        except Exception:
            log.warning("Не удалось уведомить доставщика об оплате №%s", order_id)
    if order.get("chef_id"):
        try:
            await _send_with_retry(
                lambda: bot.send_message(
                    int(order["chef_id"]),
                    f"✅ Оплата по заказу №{order_id} получена. Спасибо!",
                )
            )
        except Exception:
            log.warning("Не удалось подтвердить оплату клиенту №%s", order_id)
    card_id = order.get("courier_card_message_id")
    if courier_chat and card_id:
        try:
            await bot.edit_message_text(
                chat_id=int(courier_chat),
                message_id=int(card_id),
                text=format_order(order, "🚚 Доставка"),
                reply_markup=courier_status_keyboard(
                    order_id,
                    str(order.get("status")),
                    "paid",
                    point=order_point(order),
                ),
            )
        except Exception:
            pass


async def poll_ton_payments(bot: Bot, db: Database, settings: Settings) -> None:
    """Один цикл проверки: истёкшие счета и новые входящие переводы."""
    client = ton_client(settings)
    if client is None:
        return
    for payment in await db.expire_ton_payments():
        order = await db.get_order(int(payment["order_id"])) or {}
        chat_id = order.get("courier_chat_id") or settings.courier_chat_id
        if not chat_id:
            continue
        try:
            await bot.send_message(
                int(chat_id),
                f"⌛ Время оплаты по заказу №{payment['order_id']} истекло. "
                "Нажмите «🛰 Прибыл — USDT», чтобы выставить счёт заново.",
            )
        except Exception:
            log.warning("Не удалось сообщить об истечении счёта №%s", payment["order_id"])

    pending = await db.list_open_ton_payments()
    if not pending:
        return
    try:
        transfers = await client.transfers()
    except TonPayError as exc:
        log.warning("USDT: %s", exc)
        return
    for payment in pending:
        transfer = match_payment(
            transfers,
            comment=str(payment["comment"]),
            required_nano=int(payment["amount_nano"]),
            wallet_raw=client.wallet_raw,
            jetton_raw=client.jetton_raw,
        )
        if transfer is None:
            continue
        if await db.ton_tx_used(transfer.tx_hash):
            continue
        order_id = int(payment["order_id"])
        amount_nano = int(payment["amount_nano"])
        try:
            await db.apply_ton_payment(
                int(payment["id"]), transfer.tx_hash, order_id, amount_nano / 1_000_000
            )
        except RuntimeError:
            log.info("USDT: счёт №%s уже обработан", payment["id"])
            continue
        except Exception:
            log.exception("USDT: не удалось зачесть платёж по заказу №%s", order_id)
            continue
        order = await db.get_order(order_id) or {}
        late = str(payment.get("status")) == "expired"
        log.info(
            "USDT: заказ №%s оплачен%s, tx=%s",
            order_id,
            " (поздний платёж)" if late else "",
            transfer.tx_hash,
        )
        await _usdt_paid_notifications(bot, db, settings, order, amount_nano, late=late)
