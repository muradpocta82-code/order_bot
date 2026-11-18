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
from .labels import build_labels, today_label
from .config import Settings
from .db import Database
from .formatting import esc, format_orders_page, money
from .keyboards import (
    broadcast_confirm_keyboard,
    claim_actions_keyboard,
    manager_back_keyboard,
    manager_panel_keyboard,
    price_undo_keyboard,
    price_update_keyboard,
)
from .notify import _detach_card, _send_order_card
from .roles import _is_manager, _manager_ok
from .summary import today_summary
from .buylist import as_text as buylist_text, as_xlsx as buylist_xlsx, collect as buylist_collect
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
    chef_id = int(order.get("chef_id") or 0)
    if chef_id:
        user = await db.get_user(chef_id)
        limit = float(user.get("credit_limit") or 0) if user else 0.0
        if limit:
            debt, _count, _age = await db.client_debt(chef_id)
            total = float(order.get("total") or 0)
            if debt + total > limit:
                confirm_key = f"credit_confirm:{order_id}"
                already = await db.get_setting(confirm_key, "")
                if not already:
                    await db.set_setting(confirm_key, "1")
                    await callback.answer(
                        f"⚠️ Лимит долга превышен: долг {money(debt)} + заказ {money(total)} "
                        f"больше лимита {money(limit)}. Нажмите ещё раз, если всё равно в долг.",
                        show_alert=True,
                    )
                    return
                await db.set_setting(confirm_key, "")
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
        user = await db.get_user(int(row["chef_id"])) if row.get("chef_id") else None
        limit = float(user.get("credit_limit") or 0) if user else 0.0
        days = int(user.get("credit_days") or 0) if user else 0
        note = []
        if limit:
            note.append(f"лимит {money(limit)}")
        if days:
            debt, _n, age = await db.client_debt(int(row["chef_id"]))
            if age is not None:
                if age > days:
                    note.append(f"⚠️ просрочка {age - days} дн. (срок {days})")
                else:
                    note.append(f"срок {days} дн.")
        suffix = (" · " + "; ".join(note)) if note else ""
        lines.append(
            f"• {esc(row.get('chef_name') or row['chef_id'])} — "
            f"{money(row['s'])} ({row['n']} зак., с {str(row.get('since'))[:10]}){suffix}\n"
            f"  id: <code>{row['chef_id']}</code>"
        )
    lines.append("")
    lines.append(f"Итого: <b>{money(total)}</b>")
    buckets = await debt_age_buckets(db, settings)
    if buckets:
        parts = [f"{label}: {money(amount)}" for label, amount in buckets.items() if amount]
        if parts:
            lines.append("По возрасту: " + " · ".join(parts))
    oldest = await debt_oldest_days(db, settings)
    if oldest is not None and oldest > 14:
        lines.append(f"⚠️ Самый старый долг — {oldest} дней")
    lines.append("Закрыть долг: <code>/settle ID</code>")
    await message.answer("\n".join(lines))


def _as_date(value: Any):
    from datetime import date, datetime

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:19]).date()
    except ValueError:
        return None


async def debt_age_buckets(db: Database, settings: Settings) -> dict[str, float]:
    """Сколько денег в долгах по возрасту: 0-7, 8-14, 15-30, 30+ дней."""
    from datetime import datetime

    today = datetime.now(settings.timezone).date()
    buckets: dict[str, float] = {"0–7 дн": 0.0, "8–14 дн": 0.0, "15–30 дн": 0.0, "30+ дн": 0.0}
    for order in await db.credit_orders():
        created = _as_date(order.get("created_at"))
        if created is None:
            continue
        age = (today - created).days
        amount = float(order.get("total") or 0)
        if age <= 7:
            buckets["0–7 дн"] += amount
        elif age <= 14:
            buckets["8–14 дн"] += amount
        elif age <= 30:
            buckets["15–30 дн"] += amount
        else:
            buckets["30+ дн"] += amount
    return buckets


async def debt_oldest_days(db: Database, settings: Settings) -> int | None:
    from datetime import datetime

    today = datetime.now(settings.timezone).date()
    ages = [
        (today - _as_date(order.get("created_at"))).days
        for order in await db.credit_orders()
        if _as_date(order.get("created_at")) is not None
    ]
    return max(ages) if ages else None


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
    if old and abs(price - old) / old > 0.3:
        key = f"price_confirm:{item.id}"
        if await db.get_setting(key, "") != f"{price}":
            await db.set_setting(key, f"{price}")
            direction = "дороже" if price > old else "дешевле"
            percent = abs(price - old) / old * 100
            await message.answer(
                f"⚠️ «{esc(item.name)}» станет {direction} на {percent:.0f}%: "
                f"{money(old)} → {money(price)}.\n"
                "Похоже на опечатку. Если всё верно — повторите ту же команду."
            )
            return
        await db.set_setting(key, "")
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
    costs = await db.costs_map()
    rows = await db.sold_items(days)
    revenue = 0.0
    cost = 0.0
    unknown = 0
    for row in rows:
        qty = float(row.get("qty") or 0)
        price = float(row.get("price") or 0)
        revenue += qty * price
        item = catalog.get(int(row["catalog_id"])) if row.get("catalog_id") else None
        purchase = costs.get(int(row["catalog_id"])) if row.get("catalog_id") else None
        if purchase is None and item is not None and item.purchase:
            purchase = float(item.purchase)
        if purchase is not None:
            cost += qty * purchase
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
            "задайте её командой /cost позиция цена или колонкой «Закупка» в прайсе."
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


@router.message(Command("price_morning"))
async def cmd_price_morning(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Включает/выключает утреннюю рассылку цен клиентам."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    arg = (command.args or "").strip().lower()
    if arg in ("on", "1", "вкл", "да"):
        await db.set_setting("price_morning_enabled", "1")
        hour = await db.get_setting("price_morning_hour", "8")
        await message.answer(
            f"Утренние цены включены. Отправка после {hour}:00 по Ташкенту, "
            "один раз в день, только активным клиентам.\n"
            "Выключить: /price_morning off"
        )
        return
    if arg in ("off", "0", "выкл", "нет"):
        await db.set_setting("price_morning_enabled", "0")
        await message.answer("Утренние цены выключены.")
        return
    enabled = await db.get_setting("price_morning_enabled", "0") == "1"
    hour = await db.get_setting("price_morning_hour", "8")
    await message.answer(
        f"Утренние цены: {'включены' if enabled else 'выключены'}, после {hour}:00.\n"
        "Включить: /price_morning on · выключить: /price_morning off\n"
        "Время меняется настройкой price_morning_hour."
    )


@router.message(Command("buylist"))
async def cmd_buylist(
    message: Message,
    command: CommandObject,
    db: Database,
    settings: Settings,
    catalog: Catalog,
) -> None:
    """Что закупать на дату: текст + файл Excel."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    from datetime import datetime, timedelta

    now = datetime.now(settings.timezone)
    raw = (command.args or "").strip().lower()
    if raw in ("", "завтра", "tomorrow"):
        day = (now.date() + timedelta(days=1)).isoformat()
    elif raw in ("сегодня", "today"):
        day = now.date().isoformat()
    else:
        try:
            day = datetime.fromisoformat(raw).date().isoformat()
        except ValueError:
            await message.answer(
                "Формат: /buylist (на завтра), /buylist сегодня или /buylist 2026-11-20"
            )
            return
    rows, pending = await buylist_collect(db, catalog, day)
    await message.answer(buylist_text(rows, pending, day))
    if not rows:
        return
    from aiogram.types import BufferedInputFile

    document = BufferedInputFile(
        buylist_xlsx(rows, day), filename=f"zakupka-{day}.xlsx"
    )
    await message.answer_document(document, caption=f"Закупка на {day} — Excel")


@router.message(Command("credit_terms"))
async def cmd_credit_terms(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Условия отсрочки клиента: /credit_terms ID ЛИМИТ ДНЕЙ (0 — без ограничения)."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    parts = (command.args or "").split()
    if len(parts) < 3 or not parts[0].lstrip("-").isdigit():
        await message.answer(
            "Формат: <code>/credit_terms ID ЛИМИТ ДНЕЙ</code>\n"
            "Пример: <code>/credit_terms 123456789 300 14</code> — долг до $300, оплата за 14 дней.\n"
            "Чтобы убрать ограничения: <code>/credit_terms ID 0 0</code>"
        )
        return
    chef_id = int(parts[0])
    try:
        limit = float(parts[1].replace(",", "."))
        days = int(parts[2])
    except ValueError:
        await message.answer("Лимит и срок должны быть числами.")
        return
    await db.set_credit_terms(chef_id, limit or None, days or None)
    debt, count, age = await db.client_debt(chef_id)
    lines = [
        f"✅ Условия отсрочки для <code>{chef_id}</code> обновлены.",
        f"Лимит: {money(limit) if limit else 'без ограничения'}",
        f"Срок оплаты: {days} дн." if days else "Срок оплаты: не задан",
        f"Текущий долг: {money(debt)} ({count} зак.)",
    ]
    if age is not None:
        lines.append(f"Самый старый долг: {age} дн.")
    await message.answer("\n".join(lines))


@router.message(Command("cost"))
async def cmd_cost(
    message: Message,
    command: CommandObject,
    db: Database,
    settings: Settings,
    catalog: Catalog,
) -> None:
    """Закупочная цена позиции: /cost помидоры 1.10 (в тех же деньгах, что прайс)."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    raw = (command.args or "").strip()
    costs = await db.costs_map()
    if not raw:
        missing = [item for item in catalog.items if item.id not in costs]
        lines = [
            "🧾 <b>Закупочные цены</b>",
            f"Задано: {len(costs)} из {len(catalog.items)} позиций.",
            "",
            "Задать: <code>/cost помидоры 1.10</code>",
            "Валюта та же, что в прайсе (USD).",
        ]
        if missing:
            names = ", ".join(item.name for item in missing[:8])
            lines.append("")
            lines.append(f"Без закупки, например: {names}")
        await message.answer("\n".join(lines))
        return
    parts = raw.rsplit(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Формат: <code>/cost помидоры 1.10</code>")
        return
    query, raw_cost = parts
    try:
        cost = round(float(raw_cost.replace(",", ".").replace("$", "")), 4)
    except ValueError:
        await message.answer("Цена закупки должна быть числом, например 1.10")
        return
    if cost <= 0 or cost > 100000:
        await message.answer("Цена вне разумных пределов.")
        return
    match = catalog.match(query, limit=1, min_score=60)
    if not match:
        await message.answer("Не нашёл такую позицию в прайсе.")
        return
    item = match[0].item
    await db.set_cost(item.id, cost, source="manager")
    sell = float(item.price or 0)
    lines = [f"✅ «{esc(item.name)}»: закупка {money(cost)}"]
    if sell:
        margin = sell - cost
        lines.append(f"Продажа {money(sell)} · маржа {money(margin)} ({margin / sell * 100:.0f}%)")
    if sell and margin <= 0:
        lines.append("⚠️ Позиция убыточная — проверьте цену продажи.")
    await message.answer("\n".join(lines))


@router.message(Command("queue"))
async def cmd_queue(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Очередь сборки: сегодня и завтра по слотам, вместо потока карточек."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    from datetime import datetime, timedelta

    now = datetime.now(settings.timezone)
    days = [now.date(), now.date() + timedelta(days=1)]
    if (command.args or "").strip().lower() in ("сегодня", "today"):
        days = days[:1]
    lines = ["🗂 <b>Очередь сборки</b>"]
    empty = True
    for day in days:
        orders = [
            order
            for order in await db.orders_for_date(day.isoformat())
            if str(order.get("status")) in ("new", "accepted", "ready")
        ]
        lines.append("")
        lines.append(f"<b>{day.strftime('%d.%m')}</b> — {len(orders)} заказ(ов)")
        if not orders:
            lines.append("   пусто")
            continue
        empty = False
        groups: dict[str, list] = {}
        for order in orders:
            groups.setdefault(str(order.get("delivery_time") or "без слота"), []).append(order)
        for slot, slot_orders in sorted(groups.items()):
            total = sum(float(order.get("total") or 0) for order in slot_orders)
            lines.append(f"   ⏰ {esc(slot)} · {len(slot_orders)} · {money(total)}")
            for order in sorted(slot_orders, key=lambda o: int(o["id"])):
                status = {
                    "new": "не принят",
                    "accepted": "в сборке",
                    "ready": "собран",
                }.get(str(order.get("status")), str(order.get("status")))
                name = esc(order.get("chef_name") or order.get("chef_id"))
                lines.append(f"      №{order['id']} {name} — {status}")
    if empty:
        lines.append("")
        lines.append("Собирать нечего.")
    lines.append("")
    lines.append("Карточка заказа: /log НОМЕР — история, /orders — список")
    await message.answer("\n".join(lines))


@router.message(Command("note"))
async def cmd_note(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Заметка о клиенте: /note ID текст (видна в каждой карточке его заказа)."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(
            "Формат: <code>/note ID текст</code>\n"
            "Пример: /note 123456789 принимает с 6 до 8, вход со двора\n"
            "Очистить: /note ID -"
        )
        return
    parts = raw.split(maxsplit=1)
    if not parts[0].lstrip("-").isdigit():
        await message.answer("Первым идёт id клиента (он есть в карточке и в /debt).")
        return
    chef_id = int(parts[0])
    text = parts[1].strip() if len(parts) > 1 else ""
    if not text:
        note = await db.get_setting(f"client_note:{chef_id}", "")
        await message.answer(
            f"📌 Заметка: {esc(note)}" if note else "Заметки нет."
        )
        return
    if text in ("-", "удалить", "нет"):
        await db.set_setting(f"client_note:{chef_id}", "")
        await message.answer("Заметка удалена.")
        return
    await db.set_setting(f"client_note:{chef_id}", text[:300])
    await message.answer(f"✅ Заметка сохранена и будет видна в карточках заказов клиента {chef_id}.")


@router.message(Command("for"))
async def cmd_for_client(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Заказ за клиента (звонок/WhatsApp): /for ID, далее — текст заказа."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    arg = (command.args or "").strip()
    if arg.lower() in ("off", "стоп", "нет", "отмена"):
        await db.clear_chat_state(message.chat.id, "for_client")
        await message.answer("Режим «заказ за клиента» выключен.")
        return
    if not arg.isdigit():
        await message.answer(
            "Формат: <code>/for ID</code> — дальше пишете заказ текстом за этого клиента.\n"
            "ID клиента виден в карточке заказа и в /debt.\n"
            "Отменить: <code>/for off</code>"
        )
        return
    user = await db.get_user(int(arg))
    if user is None:
        await message.answer("Такого клиента нет. Проверьте id.")
        return
    await db.set_chat_state(message.chat.id, "for_client", arg)
    name = user.get("name") or arg
    venue = user.get("venue") or "—"
    await message.answer(
        f"📞 Оформляем заказ за клиента: <b>{esc(name)}</b> ({esc(venue)}).\n"
        "Пришлите текст заказа одним сообщением. Отменить: /for off"
    )


@router.message(Command("labels"))
async def cmd_labels(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Наклейки на коробки: /labels (завтра) или /labels 2026-11-20."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    from datetime import datetime, timedelta

    now = datetime.now(settings.timezone)
    raw = (command.args or "").strip().lower()
    if raw in ("", "завтра", "tomorrow"):
        day = (now.date() + timedelta(days=1)).isoformat()
    elif raw in ("сегодня", "today"):
        day = now.date().isoformat()
    else:
        try:
            day = datetime.fromisoformat(raw).date().isoformat()
        except ValueError:
            await message.answer("Формат: /labels, /labels сегодня или /labels 2026-11-20")
            return
    orders = [
        order
        for order in await db.orders_for_date(day)
        if str(order.get("status")) != "cancelled"
    ]
    if not orders:
        await message.answer(f"На {day} заказов нет — наклейки не нужны.")
        return
    from aiogram.types import BufferedInputFile

    document = BufferedInputFile(build_labels(orders, day), filename=today_label(day, len(orders)))
    await message.answer_document(
        document, caption=f"🏷 Наклейки на {day}: {len(orders)} шт. Печать на A4, 8 на лист."
    )


@router.message(Command("stop"))
async def cmd_stop_item(
    message: Message,
    command: CommandObject,
    db: Database,
    settings: Settings,
    catalog: Catalog,
) -> None:
    """Стоп-лист на сегодня: /stop помидоры — позиция скрывается из заказа."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    raw = (command.args or "").strip()
    if not raw:
        await message.answer("Формат: <code>/stop помидоры</code>. Список: /stoplist")
        return
    match = catalog.match(raw, limit=1, min_score=55)
    if not match:
        await message.answer("Такой позиции нет в прайсе.")
        return
    item = match[0].item
    await db.set_item_available(item.id, False, 0)
    await message.answer(
        f"🚫 «{esc(item.name)}» в стоп-листе: клиентам не предлагается. "
        f"Вернуть: <code>/unstop {esc(item.name)}</code>"
    )


@router.message(Command("unstop"))
async def cmd_unstop_item(
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
        await message.answer("Формат: <code>/unstop помидоры</code>")
        return
    match = catalog.match(raw, limit=1, min_score=55)
    if not match:
        await message.answer("Такой позиции нет в прайсе.")
        return
    item = match[0].item
    await db.set_item_available(item.id, True, None)
    await message.answer(f"✅ «{esc(item.name)}» снова доступна.")


@router.message(Command("stoplist"))
async def cmd_stoplist(
    message: Message, db: Database, settings: Settings, catalog: Catalog
) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    overrides = await db.stock_overrides()
    stopped = [
        item for item in catalog.items
        if str(item.id) in {str(key) for key in overrides}
        and not overrides[int(item.id)]["available"]
    ]
    if not stopped:
        await message.answer(
            "Стоп-лист пуст. Поставить позицию в стоп: <code>/stop помидоры</code>"
        )
        return
    lines = ["🚫 <b>Стоп-лист</b>", ""]
    for item in stopped:
        lines.append(f"• {esc(item.name)} — вернуть: <code>/unstop {esc(item.name)}</code>")
    await message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("clm:"))
async def cb_claim_action(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    """Решение по претензии: замена, кредит-нота или отказ."""
    if not await _manager_ok(callback, settings):
        return
    try:
        _, raw_claim, action = callback.data.split(":")
        claim_id = int(raw_claim)
    except (IndexError, ValueError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    claim = await db.get_claim(claim_id)
    if claim is None:
        await callback.answer("Претензия не найдена", show_alert=True)
        return
    if str(claim.get("status")) != "open":
        await callback.answer(f"Уже закрыта: {claim.get('status')}", show_alert=True)
        return
    titles = {
        "replace": "замена в следующем заказе",
        "credit": "кредит-нота",
        "reject": "отказ",
    }
    if action not in titles:
        await callback.answer("Неизвестное решение", show_alert=True)
        return
    await db.close_claim(claim_id, action)
    await db.log_event(
        int(claim["order_id"]),
        "claim",
        {"claim_id": claim_id, "decision": action},
        actor_id=callback.from_user.id,
    )
    try:
        await callback.message.edit_text(
            f"Претензия №{claim_id}: <b>{titles[action]}</b>.\n"
            f"Заказ №{claim['order_id']} · {esc(str(claim.get('text'))[:200])}"
        )
    except Exception:
        pass
    if claim.get("user_id"):
        texts = {
            "replace": "Заменим позицию в следующем заказе — без доплаты.",
            "credit": "Оформили кредит-ноту: сумма зачтётся в следующем счёте.",
            "reject": "Рассмотрели обращение и, к сожалению, не нашли подтверждения. "
                      "Менеджер свяжется, чтобы обсудить.",
        }
        try:
            await callback.bot.send_message(
                int(claim["user_id"]),
                f"По обращению №{claim_id} (заказ №{claim['order_id']}): {texts[action]}",
            )
        except Exception:
            pass
    await callback.answer("Записал решение")


@router.message(Command("claims"))
async def cmd_claims(message: Message, db: Database, settings: Settings) -> None:
    """Открытые претензии: что ещё не решено."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    rows = await db.open_claims(20)
    if not rows:
        await message.answer("Открытых претензий нет.")
        return
    lines = [f"⚠️ <b>Открытые претензии: {len(rows)}</b>", ""]
    for row in rows:
        lines.append(
            f"№{row['id']} · заказ №{row['order_id']} · "
            f"{str(row.get('created_at'))[:16]}\n   {esc(str(row.get('text'))[:200])}"
        )
    lines.append("")
    lines.append("Решение — кнопками в сообщении о претензии.")
    await message.answer("\n".join(lines))


@router.message(Command("ops"))
async def cmd_ops(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Скорость сборки и доставки, причины сбоев и итоги претензий."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    days = 30
    raw = (command.args or "").strip()
    if raw.isdigit():
        days = max(1, min(365, int(raw)))
    stats = await db.ops_stats(days)
    if not stats["orders"]:
        await message.answer(
            f"За {days} дней нет заказов с историей. Журнал событий ведётся с 17.11.2026."
        )
        return
    lines = [f"⚙️ <b>Исполнение за {days} дней</b>", ""]
    lines.append(f"Заказов: <b>{stats['orders']}</b>")
    if stats["pack_avg"] is not None:
        lines.append(
            f"Создан → собран: <b>{stats['pack_avg']:.0f} мин</b> "
            f"(по {stats['pack_count']} заказам)"
        )
    else:
        lines.append("Создан → собран: пока нет данных")
    if stats["deliver_avg"] is not None:
        lines.append(
            f"Собран → доставлен: <b>{stats['deliver_avg']:.0f} мин</b> "
            f"(по {stats['deliver_count']} заказам)"
        )
    else:
        lines.append("Собран → доставлен: пока нет данных")
    if stats["failed"]:
        lines.append("")
        lines.append("<b>Недоставки</b>")
        for row in stats["failed"]:
            lines.append(f"• {esc(str(row['reason']))} — {row['n']}")
    if stats["claims"]:
        lines.append("")
        lines.append("<b>Претензии</b>")
        titles = {
            "open": "в работе",
            "replace": "заменили",
            "credit": "кредит-нота",
            "reject": "отклонили",
            "closed": "закрыты",
        }
        for row in stats["claims"]:
            lines.append(f"• {titles.get(str(row['status']), row['status'])} — {row['n']}")
    await message.answer("\n".join(lines))


@router.message(Command("refs"))
async def cmd_refs(message: Message, db: Database, settings: Settings) -> None:
    """Кто кого привёл: клиенты по реферальным ссылкам."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    rows = await db.settings_like("ref:")
    if not rows:
        await message.answer(
            "Приглашений пока нет. Клиент может получить свою ссылку командой /invite."
        )
        return
    counts: dict[str, int] = {}
    for row in rows:
        referrer = str(row.get("value") or "")
        counts[referrer] = counts.get(referrer, 0) + 1
    lines = [f"🤝 <b>Приглашения</b> (всего {len(rows)})", ""]
    for referrer, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        user = await db.get_user(int(referrer)) if referrer.isdigit() else None
        name = (user or {}).get("venue") or (user or {}).get("name") or referrer
        lines.append(f"• {esc(str(name))} привёл {count}")
    lines.append("")
    lines.append("Новые приглашённые: " + ", ".join(
        str(row["key"]).split(":", 1)[1] for row in rows[-5:]
    ))
    await message.answer("\n".join(lines))
