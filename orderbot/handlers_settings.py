"""Команды расписания: интервалы доставки, отсечка, выходные.

Вынесено из handlers.py (I1): блок команд менеджера про время доставки
не зависит от потока заказа, поэтому живёт отдельным роутером.
"""
from __future__ import annotations

import logging
import re

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from .config import Settings
from .db import Database
from .roles import _is_manager
from .slots import parse_slots, slot_labels

log = logging.getLogger(__name__)

router = Router(name="orderbot_settings")


@router.message(Command("slots"))
async def cmd_slots(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    raw = (await db.get_setting("delivery_slots") or "").strip()
    arg = (command.args or "").strip()
    current = ", ".join(slot_labels(parse_slots(raw)))
    if not arg:
        await message.answer(
            f"Интервалы доставки: <b>{current}</b>\n"
            "Задать свои: <code>/slots 9-12,12-15,15-18</code>\n"
            "Сбросить к обычным: <code>/slots default</code>"
        )
        return
    if arg.lower() in ("default", "сброс", "обычные"):
        await db.set_setting("delivery_slots", "")
        await message.answer(
            f"Вернул обычные интервалы: <b>{', '.join(slot_labels(parse_slots(None)))}</b>"
        )
        return
    slots = parse_slots(arg)
    if len(slots) == 1 and "-" not in arg:
        await message.answer(
            "Нужен формат «начало-конец», например <code>/slots 9-12,12-15</code>"
        )
        return
    value = ",".join(f"{start}-{end}" for start, end in slots)
    await db.set_setting("delivery_slots", value)
    await message.answer(
        f"Записал интервалы: <b>{', '.join(slot_labels(slots))}</b>\n"
        "Клиенту, который не назвал время, бот предложит выбрать один из них."
    )


@router.message(Command("cutoff"))
async def cmd_cutoff(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    raw = (await db.get_setting("cutoff_time") or "").strip()
    arg = (command.args or "").strip().lower()
    if not arg:
        await message.answer(
            f"Время отсечки: <b>{raw or 'не задано'}</b>\n"
            "Задать: <code>/cutoff 18:00</code>, выключить: <code>/cutoff off</code>"
        )
        return
    if arg in ("off", "выкл", "нет"):
        await db.set_setting("cutoff_time", "")
        await message.answer("Отсечку выключил.")
        return
    match = re.fullmatch(r"([01]?\d|2[0-3])[:.]([0-5]\d)", arg)
    if not match:
        await message.answer("Формат: <code>/cutoff 18:00</code>")
        return
    value = f"{int(match.group(1)):02d}:{match.group(2)}"
    await db.set_setting("cutoff_time", value)
    await message.answer(
        f"Отсечка: после <b>{value}</b> заказы на завтра переносятся на послезавтра."
    )


@router.message(Command("weekend"))
async def cmd_weekend(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    names = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    current = (await db.get_setting("weekend_days") or "").strip()
    arg = (command.args or "").strip().lower()
    if not arg:
        pretty = ", ".join(names[int(d)] for d in current.split(",") if d.isdigit()) or "нет"
        await message.answer(
            f"Выходные дни доставки: <b>{pretty}</b>\n"
            "Задать: <code>/weekend 5,6</code> (0 — понедельник), выключить: <code>/weekend off</code>"
        )
        return
    if arg in ("off", "выкл", "нет"):
        await db.set_setting("weekend_days", "")
        await message.answer("Выходные выключил.")
        return
    days = sorted({int(part) for part in re.findall(r"[0-6]", arg)})
    if not days:
        await message.answer("Формат: <code>/weekend 5,6</code>")
        return
    value = ",".join(str(day) for day in days)
    await db.set_setting("weekend_days", value)
    await message.answer(
        "Выходные дни доставки: <b>" + ", ".join(names[day] for day in days) + "</b>."
    )


