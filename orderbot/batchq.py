"""Пакетные уточнения: несколько позиций — один вопрос вместо цепочки (E5).

Раньше бот спрашивал по одной позиции: «Филе — сколько?» → ответ → «Моцарелла — сколько?»
и так далее. На заказе из трёх непонятных позиций повар получал три сообщения.

Теперь, если уточнения нужны сразу по нескольким позициям одного типа
(количество или фасовка), бот присылает ОДНО сообщение нумерованным списком,
а кнопки под ним подписаны номером позиции. Каждое нажатие убирает свою позицию
из списка, и сообщение обновляется на месте — без новых сообщений.
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .formatting import esc
from .units import format_qty, pack_size, plural

BATCH_STATUSES = ("needs_unit", "needs_qty")
MAX_BATCH = 4          # больше — нечитаемо, остальное спросим следующим сообщением
QTY_OPTIONS = (1, 2, 5)  # быстрые варианты количества; точное число — текстом


def batch_group(items: list[dict]) -> list[dict]:
    """Позиции одного типа уточнения (берём самый частый тип), не больше MAX_BATCH."""
    counts: dict[str, list[dict]] = {}
    for item in items or []:
        status = str(item.get("status") or "")
        if status in BATCH_STATUSES:
            counts.setdefault(status, []).append(item)
    if not counts:
        return []
    status = max(counts, key=lambda key: len(counts[key]))
    group = sorted(counts[status], key=lambda item: int(item.get("idx") or 0))
    if len(group) < 2:
        return []
    return group[:MAX_BATCH]


def item_label(item: dict) -> str:
    """Короткая подпись позиции для списка и кнопок."""
    name = str(item.get("name") or item.get("raw") or "позиция")
    qty = item.get("qty")
    unit = str(item.get("unit") or item.get("catalog_unit") or "")
    try:
        qty_value = float(qty or 0)
    except (TypeError, ValueError):
        qty_value = 0.0
    if qty_value and unit:
        return f"{name} — {format_qty(qty_value)} {unit}"
    return name


def batch_text(items: list[dict]) -> str:
    """Текст одного вопроса на несколько позиций."""
    head = "Уточню сразу несколько позиций:"
    lines = [f"<b>{head}</b>", ""]
    for number, item in enumerate(items, start=1):
        lines.append(f"{number}) {esc(item_label(item))}")
    unit_status = str(items[0].get("status") or "") if items else ""
    lines.append("")
    if unit_status == "needs_unit":
        lines.append("Выберите вариант по каждой позиции (цифра в кнопке — её номер).")
    else:
        lines.append("Сколько взять? Цифра в кнопке — номер позиции.")
    return "\n".join(lines)


def batch_keyboard(draft_id: str, items: list[dict]) -> InlineKeyboardMarkup:
    """Кнопки: одна строка на позицию, подписи начинаются с её номера."""
    rows: list[list[InlineKeyboardButton]] = []
    for number, item in enumerate(items, start=1):
        idx = int(item.get("idx") or 0)
        status = str(item.get("status") or "")
        row: list[InlineKeyboardButton] = []
        if status == "needs_unit":
            base = str(item.get("catalog_unit") or "шт")
            size = pack_size(item.get("pack"), base)
            for count in (1, 2):
                label = f"{number}) {count} {plural(base, count)}"
                if size:
                    label += f" ≈{format_qty(size * count)}"
                row.append(
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"unit:{draft_id}:{idx}:pack{count}",
                    )
                )
            requested = float(item.get("qty") or 1)
            requested_unit = str(item.get("unit") or base)
            row.append(
                InlineKeyboardButton(
                    text=f"{number}) ={format_qty(requested)} {requested_unit}",
                    callback_data=f"unit:{draft_id}:{idx}:exact",
                )
            )
        else:
            unit = str(item.get("final_unit") or item.get("unit") or "")
            for value in QTY_OPTIONS:
                row.append(
                    InlineKeyboardButton(
                        text=f"{number}) {value} {plural(unit, value)}",
                        callback_data=f"qty:{draft_id}:{idx}:{value}",
                    )
                )
        rows.append(row)
    rows.append(
        [InlineKeyboardButton(text="Отмена", callback_data=f"dcancel:{draft_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
