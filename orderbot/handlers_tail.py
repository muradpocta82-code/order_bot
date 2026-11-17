"""Последние обработчики: всё, что не подошло ни под один фильтр.

Этот роутер подключается в самом конце — иначе он перехватывал бы кнопки
и сообщения, которые должны обрабатывать конкретные обработчики.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

router = Router(name="orderbot_tail")


@router.callback_query()
async def cb_unknown(callback: CallbackQuery) -> None:
    await callback.answer("Кнопка устарела. Пришлите заказ заново.", show_alert=True)


# ---------------------------------------------------------------------------
# прочее
# ---------------------------------------------------------------------------


@router.message(F.text.startswith("/"))
async def unknown_command(message: Message) -> None:
    await message.answer("Не знаю такую команду. Список команд: /help")


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Напишите заказ текстом или запишите голосом. Пример — /help")
