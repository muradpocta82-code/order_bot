"""Склад и качество разбора: остатки, стоп-лист, «чего не хватает».

Вынесено из handlers.py (I1, второй кусок). Команды менеджера про наличие
товара и качество разбора не связаны с потоком заказа.
"""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from .catalog import Catalog
from .config import Settings
from .db import Database
from .formatting import esc, format_missing, format_quality, money
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


