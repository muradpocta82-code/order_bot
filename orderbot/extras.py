"""Чистые функции для правок заказа, сводок и коррекции голоса.

Без Telegram и БД — чтобы покрывать тестами.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from rapidfuzz import fuzz

from .catalog import Catalog, normalize
from .units import format_qty, plural

# ---------------------------------------------------------------------------
# правка заказа текстом
# ---------------------------------------------------------------------------

_ADD_RE = re.compile(r"^(?:добавь|добавить|добавьте|ещ[её]|плюс)\s+(.+)$", re.I)
_REMOVE_RE = re.compile(
    r"^(?:убери|уберите|удали|удалить|убрать|без)\s+(.+)$", re.I
)
_REPLACE_RE = re.compile(
    r"^вместо\s+(.+?)\s+(?:сделай|сделайте|поставь|поставьте)\s+(.+)$", re.I
)


def parse_edit(text: str) -> dict[str, str] | None:
    """Распознаёт «добавь…», «убери…», «вместо … сделай …» без AI."""
    low = " ".join(str(text or "").lower().replace("ё", "е").split()).strip(" .,!")
    if not low:
        return None
    match = _REPLACE_RE.match(low)
    if match:
        return {"action": "replace", "old": match.group(1).strip(), "new": match.group(2).strip()}
    match = _ADD_RE.match(low)
    if match:
        return {"action": "add", "text": match.group(1).strip()}
    match = _REMOVE_RE.match(low)
    if match:
        return {"action": "remove", "text": match.group(1).strip()}
    return None


def find_order_item(order: dict[str, Any], phrase: str, catalog: Catalog | None = None) -> dict[str, Any] | None:
    """Ищет позицию заказа по названию/сырому тексту фразы."""
    needle = normalize(phrase)
    if not needle:
        return None
    items = order.get("items") or []
    # 1) прямое вхождение слов
    for item in items:
        hay = normalize(f"{item.get('name') or ''} {item.get('raw') or ''}")
        if needle and (needle in hay or hay in needle):
            return item
    # 2) по каталогу: сопоставить фразу и взять совпавший catalog_id
    if catalog is not None:
        match = catalog.match(phrase, limit=1, min_score=70)
        if match:
            for item in items:
                if item.get("catalog_id") == match[0].item.id:
                    return item
    # 3) по отдельным словам
    words = [w for w in needle.split() if len(w) >= 4]
    for item in items:
        hay = normalize(f"{item.get('name') or ''} {item.get('raw') or ''}")
        if any(word in hay for word in words):
            return item
    return None


def items_summary(items: list[dict[str, Any]]) -> str:
    if not items:
        return "— пусто"
    lines = []
    for item in items:
        qty = item.get("final_qty") if item.get("final_qty") is not None else item.get("qty")
        unit = item.get("final_unit") or item.get("unit") or "шт"
        total = item.get("total")
        if total is None:
            total = item.get("sum")
        price = round(float(total), 2) if total is not None else None
        lines.append(
            f"• {item.get('name') or item.get('raw')} — {format_qty(qty)} {plural(unit, qty)}"
            + (f" = ${price:,.2f}" if price is not None else "")
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# сводка на завтра и недельный отчёт
# ---------------------------------------------------------------------------
def tomorrow_summary(orders: list[dict[str, Any]], day_text: str) -> str:
    """Складывает заказы на завтра по товарам и по времени доставки."""
    if not orders:
        return ""
    totals: dict[tuple[str, str], float] = defaultdict(float)
    by_time: dict[str, list[str]] = defaultdict(list)
    venues: set[str] = set()
    revenue = 0.0
    for order in orders:
        revenue += float(order.get("total") or 0)
        venue = order.get("venue") or order.get("chef_name") or "повар"
        venues.add(venue)
        time_key = order.get("delivery_time") or "без времени"
        by_time[time_key].append(f"{venue} (№{order['id']})")
        for item in order.get("items", []):
            name = item.get("name") or item.get("raw") or "позиция"
            unit = item.get("unit") or "шт"
            totals[(name, unit)] += float(item.get("qty") or 0)

    lines = [f"🌙 <b>Сводка на {day_text}</b>", ""]
    lines.append(f"Заказов: <b>{len(orders)}</b> · заведений: <b>{len(venues)}</b> · "
                 f"сумма: <b>${revenue:,.2f}</b>")
    lines.append("")
    lines.append("<b>По товарам</b>")
    for (name, unit), qty in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"• {name}: {format_qty(qty)} {plural(unit, qty)}")
    lines.append("")
    lines.append("<b>По времени</b>")
    for time_key in sorted(by_time):
        lines.append(f"• {time_key}: " + ", ".join(by_time[time_key]))
    return "\n".join(lines)


def weekly_report(
    orders: list[dict[str, Any]],
    venues: dict[int, str],
    regulars: list[str],
    dropped: list[str],
    start_text: str,
    end_text: str,
) -> str:
    active = [o for o in orders if o.get("status") != "cancelled"]
    revenue = sum(float(o.get("total") or 0) for o in active)
    products: dict[tuple[str, str], float] = defaultdict(float)
    venue_count: dict[str, int] = defaultdict(int)
    for order in active:
        venue = venues.get(int(order.get("chef_id") or 0)) or order.get("chef_name") or "повар"
        venue_count[venue] += 1
        for item in order.get("items", []):
            name = item.get("name") or item.get("raw") or "позиция"
            products[(name, item.get("unit") or "шт")] += float(item.get("qty") or 0)

    lines = [
        f"📊 <b>Отчёт за неделю</b> {start_text}–{end_text}",
        "",
        f"Заказов: <b>{len(active)}</b>",
        f"Выручка: <b>${revenue:,.2f}</b>",
        "",
        "<b>Топ-5 товаров</b>",
    ]
    top_products = sorted(products.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    if top_products:
        for (name, unit), qty in top_products:
            lines.append(f"• {name}: {format_qty(qty)} {plural(unit, qty)}")
    else:
        lines.append("• нет данных")
    lines.append("")
    lines.append("<b>Топ-5 заведений</b>")
    top_venues = sorted(venue_count.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    if top_venues:
        for venue, count in top_venues:
            lines.append(f"• {venue}: {count} заказ(ов)")
    else:
        lines.append("• нет данных")
    lines.append("")
    lines.append("<b>Заказывали каждую неделю</b>")
    lines.extend(f"• {name}" for name in regulars) if regulars else lines.append("• нет")
    lines.append("")
    lines.append("<b>Пропали на 2+ недели</b>")
    lines.extend(f"• {name}" for name in dropped) if dropped else lines.append("• нет")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# коррекция текста после распознавания речи
# ---------------------------------------------------------------------------

_NUM_WORDS_FIX = {
    "один": "1", "одну": "1", "одна": "1", "два": "2", "две": "2",
    "три": "3", "четыре": "4", "пять": "5", "шесть": "6", "семь": "7",
    "восемь": "8", "девять": "9", "десять": "10", "полтора": "1,5",
}


def _vocabulary(catalog: Catalog) -> set[str]:
    words: set[str] = set()
    for item in catalog.items:
        for phrase in (item.name, *item.synonyms):
            for word in normalize(phrase).split():
                if len(word) >= 3 and not any(ch.isdigit() for ch in word):
                    words.add(word)
    return words


def correct_voice_text(text: str, catalog: Catalog, min_ratio: float = 80.0) -> str:
    """Чинит типичные ошибки распознавания по словарю прайса и числам."""
    if not text:
        return text
    vocab = _vocabulary(catalog)
    result: list[str] = []
    for raw_word in str(text).split():
        core = re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ]", "", raw_word).lower().replace("ё", "е")
        if core in _NUM_WORDS_FIX:
            result.append(_NUM_WORDS_FIX[core])
            continue
        if len(core) < 4 or core in vocab or any(ch.isdigit() for ch in core):
            result.append(raw_word)
            continue
        best_word, best_score = None, 0.0
        for candidate in vocab:
            if abs(len(candidate) - len(core)) > 3:
                continue
            score = fuzz.ratio(core, candidate)
            if score > best_score:
                best_word, best_score = candidate, score
        if best_word is not None and best_score >= min_ratio:
            # сохраняем исходные знаки препинания вокруг слова
            prefix = raw_word[: len(raw_word) - len(raw_word.lstrip("«\"'(,"))]
            suffix = raw_word[len(raw_word.rstrip("»\"'),.!?;:")):]
            result.append(f"{prefix}{best_word}{suffix}")
        else:
            result.append(raw_word)
    return " ".join(result)
