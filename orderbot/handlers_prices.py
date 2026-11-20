"""B6: «Цены на завтра» — команды /pricenew, /pricenew_set, /markup.

Логика расчёта — в `orderbot/pricereview.py`, здесь только общение с менеджером.
"""
from __future__ import annotations

import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import pricereview as price_review
from .catalog import Catalog, normalize
from .config import Settings
from .db import Database
from .formatting import esc, money
from .roles import _is_manager, _manager_ok

log = logging.getLogger(__name__)
router = Router(name="orderbot_prices")

HINT = "Формат: /pricenew_set помидоры 1.35 · наценки: /markup"


def review_keyboard(count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"✅ Опубликовать ({count})", callback_data="pn:pub"
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Поправить строку", callback_data="pn:edit"
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="pn:no")],
        ]
    )


@router.message(Command("pricenew"))
async def cmd_pricenew(
    message: Message,
    command: CommandObject,
    db: Database,
    settings: Settings,
    catalog: Catalog,
) -> None:
    """Собирает новый прайс из закупки и наценки и показывает изменения."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    costs = await db.costs_map()
    if not costs:
        await message.answer(
            "Чтобы считать цены от закупки, нужны закупочные цены — сейчас их нет ни у одной позиции.\n\n"
            "Задать по одной: <code>/cost помидоры 1.10</code>\n"
            "Списком голосом или текстом: /costvoice помидоры 120 кг по 1.10, огурцы 80 кг по 0.90\n"
            "Разница между закупкой и ценой продажи — ваша маржа (/margin)."
        )
        return
    default = await price_review.default_markup(db)
    by_category = await price_review.markups(db, catalog)
    proposals, missing = price_review.build(catalog, costs, by_category, default)
    if not proposals:
        await message.answer(price_review.render([], missing))
        return
    await db.set_setting(
        price_review.PROPOSAL_KEY, price_review.to_payload(proposals)
    )
    await message.answer(
        price_review.render(proposals, missing),
        reply_markup=review_keyboard(len(proposals)),
    )


@router.callback_query(F.data == "pn:pub")
async def cb_pricenew_publish(
    callback: CallbackQuery, db: Database, settings: Settings, catalog: Catalog
) -> None:
    """Публикует предложение в catalog.xlsx и в памяти бота."""
    if not await _manager_ok(callback, settings):
        return
    prices = price_review.from_payload(
        await db.get_setting(price_review.PROPOSAL_KEY, "")
    )
    if not prices:
        await callback.answer("Предложение устарело: /pricenew", show_alert=True)
        return
    archive = price_review.backup_catalog(
        settings.catalog_path, settings.data_dir / "price_versions", "pricenew"
    )
    if archive is not None:
        try:
            await db.add_price_version(
                str(archive), "перед /pricenew", len(catalog.items)
            )
            for old_path in await db.prune_price_versions(5):
                try:
                    Path(old_path).unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception as exc:
            log.warning("Не записал версию прайса: %s", exc)
    updated, failed = price_review.write_prices(settings.catalog_path, prices)
    if not updated:
        await callback.answer()
        await callback.message.answer(
            "❌ Не удалось записать цены в файл прайса — ничего не менял.\n"
            "Проверьте права на catalog.xlsx и повторите /pricenew."
        )
        return
    price_review.apply_to_catalog(catalog, prices)
    try:
        catalog.dump_json(settings.data_dir / "catalog.json")
    except Exception as exc:
        log.warning("Не обновил catalog.json для мини-аппа: %s", exc)
    await db.set_setting(price_review.LAST_DONE_KEY, price_review.mark_reviewed_today())
    await db.set_setting(price_review.PROPOSAL_KEY, "")
    await callback.answer()
    lines = [f"✅ Новый прайс опубликован: {updated} поз."]
    if failed:
        lines.append(f"⚠️ Не нашёл в файле: {failed} — проверьте прайс вручную.")
    lines.append("Клиенты видят новые цены сразу, мини-апп обновлён.")
    try:
        await callback.message.edit_text("\n".join(lines))
    except Exception:
        await callback.message.answer("\n".join(lines))
    log.info("Цены на завтра опубликованы: %d позиций", updated)


@router.callback_query(F.data == "pn:edit")
async def cb_pricenew_edit(callback: CallbackQuery, settings: Settings) -> None:
    if not await _manager_ok(callback, settings):
        return
    await callback.answer()
    await callback.message.answer(
        "Пришлите правку строки одним сообщением:\n"
        "<code>/pricenew_set помидоры 1.35</code>\n"
        "Предложение остаётся в силе — после правок нажмите «Опубликовать»."
    )


@router.callback_query(F.data == "pn:no")
async def cb_pricenew_cancel(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    if not await _manager_ok(callback, settings):
        return
    await db.set_setting(price_review.PROPOSAL_KEY, "")
    await callback.answer()
    try:
        await callback.message.edit_text("Отменил, прайс не менялся.")
    except Exception:
        await callback.message.answer("Отменил, прайс не менялся.")


@router.message(Command("pricenew_set"))
async def cmd_pricenew_set(
    message: Message,
    command: CommandObject,
    db: Database,
    settings: Settings,
    catalog: Catalog,
) -> None:
    """Правка одной строки в уже собранном предложении."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    prices = price_review.from_payload(
        await db.get_setting(price_review.PROPOSAL_KEY, "")
    )
    if not prices:
        await message.answer("Сначала соберите предложение: /pricenew")
        return
    parts = (command.args or "").strip().rsplit(maxsplit=1)
    if len(parts) != 2:
        await message.answer(HINT)
        return
    query, raw_price = parts
    try:
        price = round(float(raw_price.replace(",", ".").replace("$", "")), 2)
    except ValueError:
        await message.answer("Цена должна быть числом, например 1.35")
        return
    if not price_review.MIN_PRICE <= price <= price_review.MAX_PRICE:
        await message.answer("Цена вне разумных пределов.")
        return
    match = catalog.match(query, limit=1, min_score=60)
    if not match:
        await message.answer("Не нашёл такую позицию в прайсе.")
        return
    item = match[0].item
    if item.id not in prices:
        await message.answer(
            f"«{esc(item.name)}» нет в предложении — цена и так совпадает с наценкой.\n"
            "Если нужно изменить принудительно: /price_set " + esc(item.name) + " " + f"{price:g}"
        )
        return
    prices[item.id] = price
    await db.set_setting(price_review.PROPOSAL_KEY, price_review.payload(prices))
    costs = await db.costs_map()
    cost = costs.get(item.id) or (float(item.purchase) if item.purchase else 0.0)
    margin = (price - float(cost)) / price * 100 if price and cost else 0.0
    await message.answer(
        f"✏️ «{esc(item.name)}»: {money(item.price)} → <b>{money(price)}</b> "
        f"(наценка {margin:.0f}%)\n"
        f"Осталось строк в предложении: {len(prices)}. Опубликовать — кнопка под /pricenew."
    )


@router.message(Command("markup"))
async def cmd_markup(
    message: Message,
    command: CommandObject,
    db: Database,
    settings: Settings,
    catalog: Catalog,
) -> None:
    """Таблица наценок и установка: /markup Сыры 30 или /markup default 25."""
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        return
    raw = (command.args or "").strip()
    if raw:
        parts = raw.rsplit(maxsplit=1)
        if len(parts) != 2:
            await message.answer("Формат: /markup Сыры 30 · /markup default 25")
            return
        name, raw_percent = parts
        try:
            percent = price_review.clamp_markup(float(raw_percent.replace(",", ".")))
        except ValueError:
            await message.answer("Наценка должна быть числом, например 30")
            return
        if name.strip().lower() in ("default", "общая", "все", "по умолчанию"):
            await db.set_setting(price_review.MARKUP_DEFAULT_KEY, f"{percent:g}")
            await message.answer(
                f"Общая наценка: <b>{percent:g}%</b> (для категорий без своей)."
            )
        else:
            categories = sorted({i.category for i in catalog.items if i.category})
            wanted = normalize(name)
            found = next((c for c in categories if normalize(c) == wanted), None)
            if found is None and wanted:
                found = next(
                    (c for c in categories if wanted in normalize(c)), None
                )
            if found is None:
                await message.answer(
                    "Не нашёл такую категорию. Есть:\n"
                    + ", ".join(esc(c) for c in categories)
                )
                return
            await db.set_setting(
                price_review.MARKUP_PREFIX + found, f"{percent:g}"
            )
            await message.answer(
                f"Наценка «{esc(found)}»: <b>{percent:g}%</b>.\n"
                "Пересчитать прайс: /pricenew"
            )
    costs = await db.costs_map()
    default = await price_review.default_markup(db)
    by_category = await price_review.markups(db, catalog)
    await message.answer(
        price_review.markups_text(catalog, by_category, default, costs)
    )
