"""Склад и качество разбора: остатки, стоп-лист, «чего не хватает».

Вынесено из handlers.py (I1, второй кусок). Команды менеджера про наличие
товара и качество разбора не связаны с потоком заказа.
"""
from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from .catalog import Catalog
from .config import Settings
from .db import Database
from .formatting import esc, format_missing, format_quality, money
from .stock import (
    WARN_KEY,
    current as current_stock,
    low_stock,
    low_stock_text,
    threshold,
    writeoff_effect,
)
from .keyboards import manager_back_keyboard
from .roles import _is_manager

log = logging.getLogger(__name__)

router = Router(name="orderbot_stock")


@router.message(Command("missing"))
async def cmd_missing(message: Message, db: Database, settings: Settings) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    rows = await db.list_missing(days=30, limit=15)
    await message.answer(
        format_missing(rows), reply_markup=manager_back_keyboard()
    )


@router.message(Command("quality"))
async def cmd_quality(message: Message, db: Database, settings: Settings) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    stats = await db.quality_stats(days=30)
    text = format_quality(stats)
    parse = await db.parse_stats(30)
    if parse["total"]:
        text += "\n\n🔍 <b>Разбор заказов за 30 дней</b>\n"
        text += f"Всего разобрано: {parse['total']}, вопросов: {parse['questions']}\n"
        for row in parse["rows"][:6]:
            avg_q = float(row.get("avg_q") or 0)
            avg_ms = float(row.get("avg_ms") or 0)
            text += (
                f"• {esc(str(row.get('source')))} / {esc(str(row.get('path')))}: "
                f"{row['n']} зак., вопросов {avg_q:.1f}, "
                f"{avg_ms / 1000:.1f} с\n"
            )
    await message.answer(text, reply_markup=manager_back_keyboard())


@router.message(Command("stop"))
async def cmd_stop_item(
    message: Message, command: CommandObject, db: Database, settings: Settings, catalog: Catalog
) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    query = (command.args or "").strip()
    if not query:
        await message.answer("Напишите товар: <code>/stop сыр пармезан</code>")
        return
    match = catalog.match(query, limit=1, min_score=55)
    if not match:
        await message.answer("Такого товара нет в прайсе.")
        return
    item = match[0].item
    await db.set_item_available(item.id, False, 0)
    await message.answer(
        f"🚫 «{esc(item.name)}» помечен как «нет в наличии».", reply_markup=manager_back_keyboard()
    )


@router.message(Command("start_item"))
async def cmd_start_item(
    message: Message, command: CommandObject, db: Database, settings: Settings, catalog: Catalog
) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    query = (command.args or "").strip()
    if not query:
        await message.answer("Напишите товар: <code>/start_item сыр пармезан</code>")
        return
    match = catalog.match(query, limit=1, min_score=55)
    if not match:
        await message.answer("Такого товара нет в прайсе.")
        return
    item = match[0].item
    await db.set_item_available(item.id, True, None)
    await message.answer(
        f"✅ «{esc(item.name)}» снова в наличии.", reply_markup=manager_back_keyboard()
    )




@router.message(Command("writeoff"))
async def cmd_writeoff(
    message: Message,
    command: CommandObject,
    db: Database,
    settings: Settings,
    catalog: Catalog,
) -> None:
    """Списание/усушка: /writeoff помидоры 3 кг усушка (B9)."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    from .writeoff import parse_command

    raw = (command.args or "").strip()
    request = parse_command(raw)
    if request is None:
        await message.answer(
            "Формат: <code>/writeoff позиция количество причина</code>\n"
            "Пример: <code>/writeoff помидоры 3 кг усушка</code>\n"
            "Причины: усушка, порча, просрочка, брак, бой, подарок."
        )
        return
    matches = catalog.match(request.phrase, limit=1, min_score=55)
    if not matches:
        await message.answer(
            f"Не нашёл «{esc(request.phrase)}» в прайсе. "
            "Проверьте название или используйте /price."
        )
        return
    item = matches[0].item
    unit = request.unit or item.unit
    day = datetime.now(settings.timezone).date().isoformat()
    cost = await db.cost_on(item.id, day)
    if cost is None:
        costs = await db.costs_map()
        cost = costs.get(item.id)
    if cost is None and item.purchase:
        cost = float(item.purchase)
    await db.add_writeoff(
        item.id,
        item.name,
        request.qty,
        unit,
        request.reason or "без причины",
        cost,
        message.from_user.id,
    )
    value = (float(cost) * request.qty) if cost is not None else None
    stock_rows = await current_stock(catalog, db)
    left_before = stock_rows.get(item.id)
    note = writeoff_effect(left_before, unit)
    if not note:
        left_after = await db.adjust_stock(item.id, -float(request.qty), base=item.stock)
        note = f"остаток: {left_after:g} {item.unit} (было {float(left_before.left or 0):g})"
    lines = [
        f"🗑 Списано: <b>{esc(item.name)} {request.qty:g} {esc(unit)}</b>"
        + (f" — {esc(request.reason)}" if request.reason else "")
    ]
    if value is not None:
        lines.append(f"По закупке: {money(value)}")
    else:
        lines.append("⚠️ Закупочная цена неизвестна — задайте её /cost, чтобы видеть потери.")
    if note:
        lines.append(f"📦 {note}")
    await message.answer("\n".join(lines))


@router.message(Command("lowstock"))
async def cmd_lowstock(
    message: Message,
    command: CommandObject,
    db: Database,
    settings: Settings,
    catalog: Catalog,
) -> None:
    """Что заканчивается: /lowstock или /lowstock 10 (сменить порог)."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    raw = (command.args or "").strip().replace(",", ".")
    if raw:
        try:
            value = max(0.0, round(float(raw), 2))
        except ValueError:
            await message.answer("Формат: /lowstock 10 — порог в единицах прайса.")
            return
        await db.set_setting(WARN_KEY, f"{value:g}")
    limit = await threshold(db)
    rows = low_stock(await current_stock(catalog, db), limit)
    await message.answer(low_stock_text(rows, limit))


@router.message(Command("losses"))
async def cmd_losses(
    message: Message, command: CommandObject, db: Database, settings: Settings, catalog: Catalog
) -> None:
    """Отчёт по потерям: категории и позиции за период (B9)."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    from .writeoff import report_lines

    days = 30
    raw = (command.args or "").strip()
    if raw.isdigit():
        days = max(1, min(365, int(raw)))
    rows = await db.writeoff_rows(days)
    for row in rows:
        item = catalog.get(int(row["catalog_id"])) if row.get("catalog_id") else None
        row["category"] = getattr(item, "category", "") if item is not None else ""
    await message.answer("\n".join(report_lines(rows, days)))
