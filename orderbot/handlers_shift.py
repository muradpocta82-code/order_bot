"""Касса, сотрудники и смены (I1).

Кассовая сверка курьера, отчёт /cash, список /staff, переключение смены,
добавление и удаление сотрудников. Вынесено из handlers.py.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from .config import Settings
from .db import Database
from .formatting import esc, format_order, money, parse_amount
from .keyboards import courier_status_keyboard
from .roles import _is_courier, _is_manager, load_staff_from_db

log = logging.getLogger(__name__)

router = Router(name="orderbot_shift")


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
    amount = parse_amount(text)
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

