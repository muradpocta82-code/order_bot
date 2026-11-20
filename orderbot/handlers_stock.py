"""Склад и качество разбора: остатки, стоп-лист, «чего не хватает».

Вынесено из handlers.py (I1, второй кусок). Команды менеджера про наличие
товара и качество разбора не связаны с потоком заказа.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from .catalog import Catalog
from .config import Settings
from .db import Database
from .formatting import esc, format_missing, format_quality, money
from . import arrivals
from . import catalogedit
from . import missing
from . import pricereview as price_review
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


@router.message(Command("income", "приход"))
async def cmd_income(
    message: Message,
    command: CommandObject,
    db: Database,
    settings: Settings,
    catalog: Catalog,
) -> None:
    """Приход товара: остатки + закупка одной строкой (G7)."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(
            arrivals.sample_text() + "\n\nИстория приходов: /arrivals [дней]"
        )
        return
    lines = arrivals.match(arrivals.parse(raw), catalog)
    if not lines:
        await message.answer("Не разобрал строку.\n\n" + arrivals.sample_text())
        return
    stock_rows = await current_stock(catalog, db)
    notes: list[str] = []
    stocks_done = 0
    costs_done = 0
    for line in lines:
        item = catalog.get(line.catalog_id)
        if item is None:
            continue
        if line.cost:
            await db.set_cost(item.id, line.cost, source="income")
            costs_done += 1
        if line.qty is not None:
            if line.unit_matches:
                left = await db.adjust_stock(item.id, line.qty, base=item.stock)
                stocks_done += 1
                row = stock_rows.get(item.id)
                was = float(row.left) if row and row.left is not None else None
                notes.append(
                    f"📦 {esc(item.name)}: "
                    + (f"{was:g} → {left:g} {esc(item.unit)}" if was is not None else f"{left:g} {esc(item.unit)}")
                )
            else:
                notes.append(
                    f"⚠️ {esc(item.name)}: единица «{esc(line.unit)}» ≠ "
                    f"«{esc(item.unit)}» — остаток не менял"
                )
        await db.add_arrival(
            item.id,
            item.name,
            line.qty,
            line.unit or item.unit,
            line.cost,
            message.from_user.id,
        )
    text = arrivals.describe(lines)
    found = sum(1 for line in lines if line.ok)
    text += (
        f"\n\n✅ Записал: {found} поз."
        f" Остатки пополнены: {stocks_done}, цены закупки: {costs_done}."
    )
    if notes:
        text += "\n" + "\n".join(notes[:10])
    text += "\n\nНовые цены от закупки: /pricenew · история: /arrivals"
    await message.answer(text)


@router.message(Command("stock"))
async def cmd_stock_consume(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Списывать ли остатки при доставке заказа (G6)."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    arg = (command.args or "").strip().lower()
    if arg in ("off", "0", "выкл", "нет"):
        await db.set_setting("stock_consume_sales", "0")
        await message.answer(
            "Остатки при доставке больше не списываются — правьте их /income и /writeoff."
        )
        return
    if arg in ("on", "1", "вкл", "да"):
        await db.set_setting("stock_consume_sales", "1")
        await message.answer(
            "Остатки списываются автоматически, когда курьер нажимает «Доставлено»."
        )
        return
    enabled = await db.get_setting("stock_consume_sales", "1") == "1"
    await message.answer(
        f"Списание остатков при доставке: {'включено' if enabled else 'выключено'}.\n"
        "Включить: /stock on · выключить: /stock off\n"
        "Пополнение: /income · списание брака: /writeoff · что заканчивается: /lowstock"
    )


@router.message(Command("habit"))
async def cmd_habit(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Напоминания «обычно вы заказываете по средам» (C2)."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    arg = (command.args or "").strip().lower()
    if arg in ("off", "0", "выкл", "нет"):
        await db.set_setting("habit_nudge_enabled", "0")
        await message.answer("Напоминания по привычке выключены.")
        return
    if arg in ("on", "1", "вкл", "да"):
        await db.set_setting("habit_nudge_enabled", "1")
        await message.answer(
            "Напоминания по привычке включены: в привычный день недели клиент, "
            "который ещё не заказал, получит «Собрать как обычно?»."
        )
        return
    enabled = await db.get_setting("habit_nudge_enabled", "1") == "1"
    rows = await db.client_order_days(120)
    clients = {int(row["chef_id"]) for row in rows if row.get("chef_id") is not None}
    await message.answer(
        f"Напоминания по привычке: {'включены' if enabled else 'выключены'}.\n"
        f"Клиентов с историей за 120 дней: {len(clients)}.\n"
        "Включить: /habit on · выключить: /habit off"
    )


@router.message(Command("missadd"))
async def cmd_missadd(
    message: Message,
    command: CommandObject,
    db: Database,
    settings: Settings,
    catalog: Catalog,
) -> None:
    """Добавляет позицию, которую просили, в прайс и сообщает клиентам (B11)."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    raw = (command.args or "").strip()
    new_item = missing.parse_missadd(raw)
    if new_item is None:
        await message.answer(
            "Формат: <code>/missadd трюфель 12.50 кг Сыры</code>\n"
            "Название, цена (обязательно), единица и категория — по желанию.\n"
            "Сводку просят /missing."
        )
        return
    existing = catalog.search(new_item.phrase)
    if existing is not None:
        await message.answer(
            f"Такая позиция уже есть: «{esc(existing.name)}» — "
            f"{money(existing.price)}/{esc(existing.unit)}."
        )
        return
    archive = price_review.backup_catalog(
        settings.catalog_path, settings.data_dir / "price_versions", "missadd"
    )
    if archive is not None:
        try:
            await db.add_price_version(
                str(archive), "перед /missadd", len(catalog.items)
            )
            for old_path in await db.prune_price_versions(5):
                try:
                    Path(old_path).unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception as exc:
            log.warning("Не записал версию прайса: %s", exc)
    try:
        new_id = catalogedit.append_item(
            settings.catalog_path,
            new_item.name,
            new_item.price,
            unit=new_item.unit,
            category=new_item.category,
        )
    except Exception as exc:
        log.exception("Не удалось добавить позицию в прайс")
        await message.answer(f"Не смог записать в прайс: {exc}")
        return
    catalogedit.reload_into(catalog, settings.catalog_path)
    try:
        catalog.dump_json(settings.data_dir / "catalog.json")
    except Exception as exc:
        log.warning("Не обновил catalog.json: %s", exc)
    askers = await db.missing_askers(new_item.phrase, 60)
    sent = 0
    for user_id in askers:
        try:
            await message.bot.send_message(
                int(user_id),
                missing.client_notice(new_item.name, new_item.price, new_item.unit),
            )
            sent += 1
        except Exception as exc:
            log.info("Не сообщил клиенту %s о новой позиции: %s", user_id, exc)
    lines = [
        f"✅ Добавил в прайс: «{esc(new_item.name)}» — "
        f"{money(new_item.price)}/{esc(new_item.unit)} (id {new_id}).",
    ]
    if sent:
        lines.append(f"Сообщил клиентам, которые просили: {sent}.")
    elif askers:
        lines.append("Клиентам сообщить не удалось (не писали боту).")
    else:
        lines.append("Просивших не нашёл — сообщать некому.")
    lines.append("Цена и название правятся как обычно: /price_set, /price.")
    await message.answer("\n".join(lines))


@router.message(Command("arrivals"))
async def cmd_arrivals(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """История приходов за период."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    raw = (command.args or "").strip()
    days = max(1, min(365, int(raw))) if raw.isdigit() else 30
    rows = await db.arrival_rows(days)
    if not rows:
        await message.answer(f"Приходов за {days} дней нет. Записать: /income помидоры 120 кг по 1.10")
        return
    lines = [f"🚚 <b>Приходы за {days} дней</b>", ""]
    for row in rows[:20]:
        qty = row.get("qty")
        cost = row.get("cost")
        when = str(row.get("ts") or "")[:16].replace("T", " ")
        line = f"• {when} — {esc(row.get('name') or 'позиция')}"
        if qty is not None:
            line += f" {float(qty):g} {esc(row.get('unit') or '')}"
        if cost is not None:
            line += f" по {money(float(cost))}"
        lines.append(line)
    if len(rows) > 20:
        lines.append(f"…и ещё {len(rows) - 20}")
    await message.answer("\n".join(lines))


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
