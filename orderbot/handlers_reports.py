"""Отчёты, долги, рассылка и цены: команды менеджера.

Вынесено из handlers.py: аналитика, маржа, долги и расчёты, рассылка своим
клиентам, точечная правка цены позиции.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from .catalog import Catalog
from .config import Settings
from .db import Database
from .formatting import esc, format_orders_page, money
from .keyboards import (
    broadcast_confirm_keyboard,
    manager_back_keyboard,
    manager_panel_keyboard,
    price_undo_keyboard,
    price_update_keyboard,
)
from .notify import _detach_card, _send_order_card
from .roles import _is_manager, _manager_ok
from .summary import today_summary
from .handlers import _process_order  # noqa: E402
from .tonflow import send_ton_invoice

log = logging.getLogger(__name__)
router = Router(name="orderbot_reports")


@router.message(Command("analytics"))
async def cmd_analytics(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    days = 30
    raw = (command.args or "").strip()
    if raw.isdigit():
        days = max(1, min(365, int(raw)))
    data = await db.analytics(days)
    lines = [f"📊 <b>Аналитика за {data['days']} дней</b>", ""]
    lines.append(f"Заказов: <b>{data['orders']}</b>")
    lines.append(f"Выручка: <b>{money(data['revenue'])}</b>")
    lines.append(f"Средний чек: <b>{money(data['avg'])}</b>")
    if data["clients"]:
        lines.append("")
        lines.append("<b>Топ клиентов</b>")
        for row in data["clients"]:
            lines.append(
                f"• {esc(row.get('chef_name') or row['chef_id'])} — "
                f"{money(row['s'])} ({row['n']} зак.)"
            )
    if data["items"]:
        lines.append("")
        lines.append("<b>Топ позиций</b>")
        for row in data["items"]:
            lines.append(f"• {esc(row.get('name') or '')} — {row['q']:g}")
    await message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("credit:"))
async def cb_credit(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    """Отдать заказ в долг."""
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
    if not await db.mark_order_credit(order_id, float(order.get("total") or 0)):
        await callback.answer("Нельзя: заказ уже оплачен или закрыт", show_alert=True)
        return
    await callback.answer("Записал в долг")
    if order.get("chef_id"):
        try:
            await callback.bot.send_message(
                int(order["chef_id"]),
                f"📝 Заказ №{order_id} записан в долг на {money(order.get('total'))}.",
            )
        except Exception:
            pass


@router.message(Command("debt"))
async def cmd_debt(message: Message, db: Database, settings: Settings) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    rows = await db.debt_list()
    if not rows:
        await message.answer("Долгов нет.")
        return
    lines = ["📝 <b>Долги клиентов</b>", ""]
    total = 0.0
    for row in rows:
        total += float(row["s"])
        lines.append(
            f"• {esc(row.get('chef_name') or row['chef_id'])} — "
            f"{money(row['s'])} ({row['n']} зак., с {str(row.get('since'))[:10]})\n"
            f"  id: <code>{row['chef_id']}</code>"
        )
    lines.append("")
    lines.append(f"Итого: <b>{money(total)}</b>")
    lines.append("Закрыть долг: <code>/settle ID</code>")
    await message.answer("\n".join(lines))


@router.message(Command("settle"))
async def cmd_settle(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    raw = (command.args or "").strip()
    if not raw.lstrip("-").isdigit():
        await message.answer("Формат: <code>/settle 123456789</code>")
        return
    count, amount = await db.settle_debt(int(raw))
    if not count:
        await message.answer("У этого клиента долгов нет.")
        return
    await message.answer(
        f"✅ Закрыто заказов: {count} на {money(amount)}. Клиент стал чистым."
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Рассылка своим клиентам: только по подтверждению."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    text = (command.args or "").strip()
    if len(text) < 5:
        await message.answer(
            "Формат: <code>/broadcast текст сообщения</code>\n"
            "Уйдёт клиентам, которые заказывали за последние 60 дней."
        )
        return
    await db.set_chat_state(message.chat.id, "broadcast", text[:3500])
    ids = await db.active_client_ids(60)
    await message.answer(
        f"📤 Отправить это <b>{len(ids)}</b> клиентам?\n\n{esc(text[:500])}",
        reply_markup=broadcast_confirm_keyboard(),
    )


@router.callback_query(F.data == "bc:cancel")
async def cb_broadcast_cancel(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    await db.set_chat_state(callback.message.chat.id, "broadcast", "")
    await callback.message.edit_text("Рассылка отменена.")
    await callback.answer()


@router.callback_query(F.data == "bc:send")
async def cb_broadcast_send(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    if not _is_manager(settings, callback.from_user.id, callback.message.chat.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    text = await db.get_chat_state(callback.message.chat.id, "broadcast")
    if not text:
        await callback.answer("Текст не найден", show_alert=True)
        return
    await db.set_chat_state(callback.message.chat.id, "broadcast", "")
    ids = await db.active_client_ids(60)
    sent = failed = 0
    await callback.message.edit_text(f"Отправляю… 0/{len(ids)}")
    for index, chat_id in enumerate(ids, start=1):
        try:
            await callback.bot.send_message(int(chat_id), text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
        if index % 20 == 0:
            try:
                await callback.message.edit_text(f"Отправляю… {index}/{len(ids)}")
            except Exception:
                pass
    await callback.message.edit_text(
        f"📤 Готово. Доставлено: {sent}, не дошло: {failed}."
    )
    await callback.answer("Рассылка завершена")


def _write_price_to_file(path, catalog_id: int, price: float) -> bool:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(path)
        ws = wb.worksheets[0]
        header = [str(cell.value or "").strip().lower() for cell in ws[1]]
        price_col = next(
            (i + 1 for i, title in enumerate(header) if title.startswith("цена")), None
        )
        id_col = next(
            (i + 1 for i, title in enumerate(header) if title in ("id", "код")), None
        )
        if not price_col or not id_col:
            return False
        for row in range(2, ws.max_row + 1):
            value = ws.cell(row=row, column=id_col).value
            if isinstance(value, (int, float)) and int(value) == int(catalog_id):
                cell = ws.cell(row=row, column=price_col)
                cell.value = float(price)
                cell.number_format = '$#,##0.00'
                wb.save(path)
                return True
    except Exception:
        log.exception("Не удалось обновить прайс-файл")
    return False


@router.message(Command("price_set"))
async def cmd_price_set(
    message: Message,
    command: CommandObject,
    db: Database,
    settings: Settings,
    catalog: Catalog,
) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    raw = (command.args or "").strip()
    if not raw:
        await message.answer("Формат: <code>/price_set сыр гауда 12.50</code>")
        return
    parts = raw.rsplit(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Формат: <code>/price_set сыр гауда 12.50</code>")
        return
    query, raw_price = parts
    try:
        price = round(float(raw_price.replace(",", ".").replace("$", "")), 2)
    except ValueError:
        await message.answer("Цена должна быть числом, например 12.50")
        return
    if price <= 0 or price > 100000:
        await message.answer("Цена вне разумных пределов.")
        return
    match = catalog.match(query, limit=1, min_score=60)
    if not match:
        await message.answer("Не нашёл такую позицию в прайсе.")
        return
    item = match[0].item
    old = float(item.price or 0)
    item.price = price
    saved = _write_price_to_file(settings.catalog_path, item.id, price)
    await message.answer(
        f"💲 «{esc(item.name)}»: {money(old)} → <b>{money(price)}</b>"
        + ("" if saved else "\n<i>В файл не записалось — цена действует до перезапуска.</i>")
    )
    log.info("Цена: %s (id=%s) %.2f -> %.2f", item.name, item.id, old, price)


@router.message(Command("margin"))
async def cmd_margin(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Маржа за период: нужны закупочные цены в прайсе."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    from orderbot.catalog import Catalog as _Catalog

    days = 30
    raw = (command.args or "").strip()
    if raw.isdigit():
        days = max(1, min(365, int(raw)))
    catalog = _Catalog.load(settings.catalog_path)
    rows = await db.sold_items(days)
    revenue = 0.0
    cost = 0.0
    unknown = 0
    for row in rows:
        qty = float(row.get("qty") or 0)
        price = float(row.get("price") or 0)
        revenue += qty * price
        item = catalog.get(int(row["catalog_id"])) if row.get("catalog_id") else None
        if item is not None and item.purchase:
            cost += qty * float(item.purchase)
        else:
            unknown += 1
    if revenue <= 0:
        await message.answer("За период нет закрытых заказов.")
        return
    margin = revenue - cost
    lines = [f"📈 <b>Маржа за {days} дней</b>", ""]
    lines.append(f"Выручка: <b>{money(revenue)}</b>")
    lines.append(f"Закупка: <b>{money(cost)}</b>")
    lines.append(f"Маржа: <b>{money(margin)}</b>")
    if revenue:
        lines.append(f"Наценка: <b>{margin / revenue * 100:.1f}%</b>")
    if unknown:
        lines.append("")
        lines.append(
            f"⚠️ По {unknown} позициям нет закупочной цены — "
            "добавьте колонку «Закупка» в прайс."
        )
    await message.answer("\n".join(lines))


@router.message(Command("today"))
async def cmd_today(message: Message, db: Database, settings: Settings) -> None:
    """Сводка дня: заказы, наличные, долги, свежесть бэкапа."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    await message.answer(await today_summary(db, settings))


@router.message(Command("log"))
async def cmd_order_log(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Хронология заказа: кто и когда менял статус, оплату, долг."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    raw = (command.args or "").strip().lstrip("№#")
    if not raw.isdigit():
        await message.answer("Как смотреть: /log 47 — номер заказа из карточки.")
        return
    order_id = int(raw)
    events = await db.order_events(order_id, limit=30)
    if not events:
        await message.answer(
            f"По заказу №{order_id} событий нет. История ведётся с 17.11.2026."
        )
        return
    lines = [f"🧾 <b>История заказа №{order_id}</b>", ""]
    for event in reversed(events):
        moment = str(event.get("created_at") or "")[:16]
        kind = event.get("kind")
        payload = event.get("payload") or {}
        if isinstance(payload, str):
            payload = {}
        detail = ", ".join(f"{key}={value}" for key, value in payload.items())
        lines.append(f"{moment} · {esc(str(kind))} {esc(detail)[:120]}")
    await message.answer("\n".join(lines))
