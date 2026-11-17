"""Подготовка черновика: остатки, фасовки, отсечка и выходные.

Здесь решается, что именно спросить у повара перед созданием заказа,
и как пересчитать количество под реальную фасовку прайса.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .catalog import Catalog
from .config import Settings
from .db import Database
from .units import canon_unit, format_qty, pack_size, plural, price_per_unit

async def _apply_stock(draft: dict, db: Database, catalog: Catalog) -> bool:
    """Помечает позиции без остатка; True — есть что уточнять."""
    overrides = await db.stock_overrides()
    blocked = False
    for item in draft.get("items", []):
        catalog_id = item.get("catalog_id")
        if not catalog_id:
            continue
        catalog_item = catalog.get(int(catalog_id))
        info = overrides.get(int(catalog_id))
        available = True
        left: float | None = None
        if info is not None:
            available = bool(info.get("available"))
            left = info.get("stock_left")
        elif catalog_item is not None and catalog_item.stock is not None:
            available = float(catalog_item.stock) > 0
            left = float(catalog_item.stock)
        if not available:
            item["status"] = "unavailable"
            subs = catalog.substitutes(catalog_item) if catalog_item else []
            item["substitutes"] = [sub.id for sub in subs]
            note = "нет в наличии"
            if subs:
                note += "; можно заменить: " + ", ".join(
                    sub.name for sub in subs[:2]
                )
            item["stock_note"] = note
            blocked = True
            continue
        qty = item.get("final_qty")
        if left is not None and qty is not None and float(qty) > left:
            unit = catalog_item.unit if catalog_item else item.get("final_unit")
            item["stock_note"] = (
                f"на складе только {format_qty(left)} {plural(unit, left)}"
            )
    return blocked


def _apply_unit_choice(item: dict, code: str, catalog: Catalog) -> None:
    catalog_item = catalog.get(item.get("catalog_id"))
    if catalog_item is None:
        item["status"] = "ok"
        return
    base = catalog_item.unit
    price = float(catalog_item.price)
    if code in ("pack1", "pack2"):
        count = 1 if code == "pack1" else 2
        item["final_qty"] = float(count)
        item["final_unit"] = base
        item["sum"] = round(count * price, 2)
    else:  # ровно столько, сколько просили — по цене за единицу фасовки
        size = pack_size(catalog_item.pack, base)
        qty = float(item.get("qty") or 1)
        per_unit = price_per_unit(price, catalog_item.pack, base)
        item["final_qty"] = qty
        item["final_unit"] = canon_unit(item.get("unit")) or base
        item["sum"] = (
            round(qty * per_unit, 2)
            if per_unit is not None
            else round(qty * price, 2)
        )
        item["unit_warning"] = False
        if size is None:
            item["sum"] = round(qty * price, 2)
    item["unit_warning"] = False
    item["status"] = "ok"
    item["note"] = ""


async def _apply_units(draft: dict, db: Database, catalog: Catalog) -> None:
    """Либо применяет привычную фасовку повара, либо задаёт вопрос кнопками."""
    for item in draft.get("items", []):
        if item.get("status") != "ok" or not item.get("unit_warning"):
            continue
        catalog_id = item.get("catalog_id")
        habit = await db.get_chat_state(
            draft.get("user_id"), f"unit_habit:{catalog_id}"
        )
        if habit in ("pack1", "pack2", "exact"):
            _apply_unit_choice(item, habit, catalog)
        else:
            item["status"] = "needs_unit"


def _next_workday(day, weekends: set[int]):
    from datetime import timedelta as _td

    for _ in range(10):
        if day.weekday() not in weekends:
            return day
        day = day + _td(days=1)
    return day


async def _apply_cutoff(draft: dict, db: Database, settings: Settings) -> str | None:
    """Сдвигает дату после отсечки/в выходной. Возвращает текст-пометку."""
    from datetime import date as _date
    from datetime import timedelta as _td

    now = datetime.now(settings.timezone)
    try:
        target = _date.fromisoformat(draft["delivery_date"])
    except (TypeError, ValueError):
        return None
    note = None
    cutoff_raw = (await db.get_setting("cutoff_time") or "").strip()
    if cutoff_raw and target == now.date() + _td(days=1):
        try:
            hour, minute = [int(part) for part in cutoff_raw.split(":")[:2]]
            if (now.hour, now.minute) >= (hour, minute):
                target = target + _td(days=1)
                note = (
                    f"После {cutoff_raw} на завтра уже не успеваем, "
                    f"поставил на {target.strftime('%d.%m')}. Подходит?"
                )
        except (ValueError, TypeError):
            pass
    weekends_raw = (await db.get_setting("weekend_days") or "").strip()
    weekends = {
        int(part) for part in weekends_raw.replace(" ", "").split(",") if part.isdigit()
    }
    if weekends and target.weekday() in weekends:
        shifted = _next_workday(target, weekends)
        if shifted != target:
            if note is None:
                note = (
                    f"{target.strftime('%d.%m')} не доставляем, "
                    f"поставил на {shifted.strftime('%d.%m')}. Подходит?"
                )
            target = shifted
    draft["delivery_date"] = target.isoformat()
    if note:
        draft["date_note"] = note
    return note


async def _suggest_similar(catalog: Catalog, item: dict) -> list[tuple[int, str]]:
    """Похожие позиции из той же категории, что и отсутствующая."""
    source = catalog.get(item.get("catalog_id"))
    if source is None:
        return []
    result: list[tuple[int, str]] = []
    for candidate in catalog.items:
        if candidate.id == source.id or candidate.category != source.category:
            continue
        if candidate.stock is not None and float(candidate.stock) <= 0:
            continue
        result.append((candidate.id, candidate.name))
        if len(result) == 4:
            break
    return result
