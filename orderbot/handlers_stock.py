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
from . import subreplace
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
    shadow = await db.shadow_stats(30)
    if shadow["n"]:
        text += (
            f"\n🧪 <b>Теневые промпты</b>\n"
            f"Сравнений: {shadow['n']}, среднее совпадение {shadow['avg']:.0f}%, "
            f"полных совпадений {shadow['exact']}\n"
        )

    by_source = await db.feedback_by_source(30)
    if by_source:
        names = {"text": "текст", "voice": "голос", "photo": "фото", "webapp": "мини-апп"}
        text += "\n🎯 <b>По источнику заказа</b>\n"
        for row in by_source:
            ok = int(row.get("ok") or 0)
            fix = int(row.get("fix") or 0)
            total = ok + fix
            share = fix / total * 100 if total else 0.0
            source = str(row.get("source") or "неизвестно")
            text += (
                f"• {esc(names.get(source, source))}: поправили {fix} из {total} "
                f"({share:.0f}%)\n"
            )
        photo = await db.photo_quality(30)
        if photo["total"] >= 5 and photo["fix_pct"] >= 30:
            text += (
                "\n⚠️ Фото-списки распознаются хуже: просите клиентов присылать "
                "текстом или голосом.\n"
            )

    feedback = await db.feedback_stats(30)
    if feedback:
        text += "\n\n🎤 <b>Распознавание голоса и фото</b>\n"
        for row in feedback:
            ok = int(row.get("ok") or 0)
            fix = int(row.get("fix") or 0)
            total = ok + fix
            share = fix / total * 100 if total else 0.0
            name = str(row.get("provider") or "неизвестно")
            if name in ("gemini", "vosk"):
                name = {"gemini": "Gemini", "vosk": "Vosk"}[name]
            text += f"• {esc(name)}: поправили {fix} из {total} ({share:.0f}%)\n"
    await message.answer(text, reply_markup=manager_back_keyboard())


@router.message(Command("tune"))
async def cmd_tune(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Пороги без деплоя: /tune — список, /tune ключ значение — изменить (I5)."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    from orderbot import tuning

    parts = (command.args or "").strip().split()
    if len(parts) < 2:
        values: dict[str, object] = {}
        for key in tuning.LIMITS:
            raw = (await db.get_setting(f"tune:{key}", "") or "").strip()
            if raw:
                values[key] = tuning.parse_value(key, raw)
        lines = ["⚙️ <b>Пороги</b>", ""]
        lines.extend(tuning.list_lines(values))
        lines.append("")
        lines.append("Изменить: <code>/tune forecast_min_samples 6</code>")
        await message.answer("\n".join(lines), reply_markup=manager_back_keyboard())
        return
    key = tuning.clean_key(parts[0])
    value = tuning.parse_value(key, parts[1])
    if key not in tuning.LIMITS:
        await message.answer(
            "Неизвестный порог. Список: /tune"
        )
        return
    if value is None:
        low, high, _default, _desc = tuning.LIMITS[key]
        await message.answer(
            f"Для <code>{key}</code> нужно число от {low:g} до {high:g}."
        )
        return
    await db.set_setting(f"tune:{key}", tuning.format_value(key, value))
    await message.answer(
        f"⚙️ <code>{key}</code> = <b>{tuning.format_value(key, value)}</b>. "
        "Применяется сразу, без деплоя."
    )


@router.message(Command("text"))
async def cmd_text(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Тексты без деплоя: /text — список, /text ключ = текст (I5)."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    from orderbot import tuning

    raw = command.args or ""
    if "=" not in raw:
        lines = ["📝 <b>Тексты</b>", ""]
        for key, desc in tuning.TEXT_KEYS.items():
            value = (await db.get_setting(f"text:{key}", "") or "").strip()
            mark = f" → «{esc(value[:80])}»" if value else " (по умолчанию)"
            lines.append(f"• <code>{key}</code>{mark}\n  <i>{desc}</i>")
        lines.append("")
        lines.append("Изменить: <code>/text edit_hint = изменить до 18:00</code>")
        await message.answer("\n".join(lines), reply_markup=manager_back_keyboard())
        return
    parsed = tuning.parse_text_assignment(raw)
    if parsed is None:
        await message.answer(
            "Формат: <code>/text ключ = текст</code>. Список ключей: /text"
        )
        return
    key, value = parsed
    await db.set_setting(f"text:{key}", value)
    await message.answer(
        f"📝 Текст <code>{key}</code> обновлён. Применяется сразу."
    )


@router.message(Command("text_del"))
async def cmd_text_del(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Вернуть текст по умолчанию (I5)."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    from orderbot import tuning

    key = tuning.clean_key(command.args)
    if tuning.unknown_text_key(key):
        await message.answer("Неизвестный ключ. Список: /text")
        return
    await db.set_setting(f"text:{key}", "")
    await message.answer(f"📝 Текст <code>{key}</code> вернулся к значению из кода.")


@router.message(Command("aliases"))
async def cmd_aliases(
    message: Message, db: Database, settings: Settings, catalog: Catalog
) -> None:
    """Общий словарь: что понимают все повара (E3)."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    rows = await db.common_aliases(40)
    if not rows:
        await message.answer(
            "Общий словарь пуст. Он наполняется сам: когда три разных повара "
            "выбирают одну и ту же позицию для одной фразы.\n"
            "Добавить вручную: <code>/alias_add с/к бекон = бекон</code>"
        )
        return
    lines = ["📖 <b>Общий словарь</b>", ""]
    for row in rows:
        item = catalog.get(int(row["catalog_id"]))
        name = item.name if item else f"id{row['catalog_id']}"
        chefs = int(row["chefs"] or 0)
        lines.append(
            f"• «{esc(row['phrase'])}» → {esc(name)}"
            + (f" · поваров {chefs}" if chefs else "")
        )
    lines.append("")
    lines.append("Удалить: <code>/alias_del фраза</code>")
    await message.answer("\n".join(lines), reply_markup=manager_back_keyboard())


@router.message(Command("alias_add"))
async def cmd_alias_add(
    message: Message,
    command: CommandObject,
    db: Database,
    settings: Settings,
    catalog: Catalog,
) -> None:
    """Добавить фразу в общий словарь: /alias_add с/к бекон = бекон (E3)."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    raw = (command.args or "").strip()
    if "=" not in raw:
        await message.answer(
            "Формат: <code>/alias_add фраза = позиция</code>\n"
            "Пример: /alias_add с/к бекон = бекон"
        )
        return
    phrase, target = raw.split("=", 1)
    phrase = " ".join(phrase.lower().replace("ё", "е").split())
    if not phrase:
        await message.answer("Пустая фраза. Формат: <code>/alias_add фраза = позиция</code>")
        return
    match = catalog.match(target.strip(), limit=1, min_score=55)
    if not match:
        await message.answer("Такой позиции нет в прайсе.")
        return
    item = match[0].item
    await db.save_alias(phrase, item.id, 0)
    await message.answer(
        f"📖 «{esc(phrase)}» → «{esc(item.name)}» в общем словаре. "
        "Теперь понимают все повара."
    )


@router.message(Command("alias_del"))
async def cmd_alias_del(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """Убрать фразу из общего словаря (E3)."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    phrase = " ".join((command.args or "").lower().replace("ё", "е").split())
    if not phrase:
        await message.answer("Формат: <code>/alias_del с/к бекон</code>")
        return
    removed = await db.delete_alias(phrase, 0)
    if removed:
        await message.answer(f"🗑 «{esc(phrase)}» убрана из общего словаря.")
    else:
        await message.answer(
            "Такой фразы в общем словаре нет. Список: /aliases"
        )


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


@router.message(Command("supplier"))
async def cmd_supplier(
    message: Message,
    command: CommandObject,
    db: Database,
    settings: Settings,
    catalog: Catalog,
) -> None:
    """Поставщик позиции: /supplier помидоры Ош-база (B12)."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    raw = (command.args or "").strip()
    if not raw:
        counts: dict[str, int] = {}
        for item in catalog.items:
            name = item.supplier or "без поставщика"
            counts[name] = counts.get(name, 0) + 1
        lines = ["🏷 <b>Поставщики</b>", ""]
        for name, count in sorted(counts.items(), key=lambda pair: -pair[1]):
            lines.append(f"• {esc(name)} — {count} поз.")
        lines.append("")
        lines.append(
            "Задать: <code>/supplier помидоры Ош-база</code>\n"
            "Закупочный лист делится по поставщикам: /buylist"
        )
        await message.answer("\n".join(lines))
        return
    parts = raw.rsplit(maxsplit=1)
    if len(parts) != 2:
        await message.answer(
            "Формат: <code>/supplier помидоры Ош-база</code>\n"
            "Пусто: <code>/supplier помидоры -</code> (убрать поставщика)"
        )
        return
    query, supplier = parts
    matches = catalog.match(query, limit=1, min_score=60)
    if not matches:
        await message.answer(f"Не нашёл «{esc(query)}» в прайсе.")
        return
    item = matches[0].item
    value = "" if supplier.strip() in ("-", "нет", "убрать") else supplier.strip()
    archive = price_review.backup_catalog(
        settings.catalog_path, settings.data_dir / "price_versions", "supplier"
    )
    saved = catalogedit.set_supplier(settings.catalog_path, item.id, value)
    if not saved:
        await message.answer("Не удалось записать поставщика в прайс.")
        return
    catalogedit.reload_into(catalog, settings.catalog_path)
    try:
        catalog.dump_json(settings.data_dir / "catalog.json")
    except Exception:
        pass
    if archive is not None:
        try:
            await db.add_price_version(str(archive), "перед /supplier", len(catalog.items))
            for old_path in await db.prune_price_versions(5):
                try:
                    Path(old_path).unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception:
            pass
    await message.answer(
        f"🏷 «{esc(item.name)}»: поставщик "
        + (f"<b>{esc(value)}</b>." if value else "убран.")
        + "\nЗакупочный лист: /buylist"
    )


@router.message(Command("sub"))
async def cmd_sub(
    message: Message,
    command: CommandObject,
    db: Database,
    settings: Settings,
    catalog: Catalog,
) -> None:
    """Правила замены: /sub гуада чеддер · /sub гуада нет · /sub гуада спросить (C8)."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(subreplace.rules_text(catalog, await db.sub_rules()))
        return
    parts = raw.rsplit(maxsplit=1)
    if len(parts) != 2:
        await message.answer(
            "Формат: <code>/sub гуада чеддер</code> (замена), "
            "<code>/sub гуада нет</code> (не заменять), "
            "<code>/sub гуада спросить</code> (по умолчанию).\n"
            "Без аргументов — список правил."
        )
        return
    target_query, value = parts
    matches = catalog.match(target_query, limit=1, min_score=60)
    if not matches:
        await message.answer(f"Не нашёл «{esc(target_query)}» в прайсе.")
        return
    item = matches[0].item
    rule = subreplace.parse_rule(value)
    if rule is None:
        substitute = catalog.match(value, limit=1, min_score=60)
        if not substitute:
            await message.answer(
                "Не понял правило. Можно: <code>нет</code>, <code>спросить</code> "
                "или название позиции для замены."
            )
            return
        chosen = substitute[0].item
        if chosen.id == item.id:
            await message.answer("Замена не может быть той же позицией.")
            return
        await db.set_sub_rule(item.id, subreplace.MODE_ONLY, chosen.id)
        await message.answer(
            f"🔁 «{esc(item.name)}»: если нет в наличии — предлагать "
            f"«{esc(chosen.name)}»."
        )
        return
    if rule.mode == subreplace.MODE_ASK:
        await db.clear_sub_rule(item.id)
        await message.answer(
            f"🔁 «{esc(item.name)}»: снова спрашивать клиента и предлагать похожие."
        )
        return
    await db.set_sub_rule(item.id, rule.mode)
    await message.answer(
        f"🔁 «{esc(item.name)}»: {rule.text}. Замена клиенту предлагаться не будет."
        if rule.mode == subreplace.MODE_NO
        else f"🔁 «{esc(item.name)}»: {rule.text}."
    )


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
