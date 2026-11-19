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


def week_metrics(orders: list[dict[str, Any]]) -> dict[str, float]:
    """Цифры недели: заказы, выручка, средний чек, клиенты."""
    active = [order for order in orders if order.get("status") != "cancelled"]
    revenue = sum(float(order.get("total") or 0) for order in active)
    clients = {int(order["chef_id"]) for order in active if order.get("chef_id")}
    return {
        "orders": float(len(active)),
        "revenue": revenue,
        "avg": revenue / len(active) if active else 0.0,
        "clients": float(len(clients)),
    }


def _delta(current: float, previous: float) -> str:
    """Стрелка и процент изменения (пусто, если сравнивать нечего)."""
    if previous <= 0:
        return "—"
    diff = (current - previous) / previous * 100
    arrow = "🔼" if diff > 0.5 else ("🔽" if diff < -0.5 else "➡️")
    return f"{arrow} {diff:+.0f}%"


def week_comparison(
    current: dict[str, float], previous: dict[str, float]
) -> list[str]:
    """Строки сравнения с прошлой неделей (F5)."""
    if previous.get("orders", 0) <= 0:
        return ["<i>Прошлая неделя без заказов — сравнивать не с чем.</i>"]
    money = lambda value: f"${value:,.2f}"
    return [
        "<b>К прошлой неделе</b>",
        f"Выручка: {money(current['revenue'])} ({_delta(current['revenue'], previous['revenue'])})",
        f"Заказы: {int(current['orders'])} ({_delta(current['orders'], previous['orders'])})",
        f"Средний чек: {money(current['avg'])} ({_delta(current['avg'], previous['avg'])})",
        f"Клиенты: {int(current['clients'])} ({_delta(current['clients'], previous['clients'])})",
    ]


def week_advice(
    current: dict[str, float],
    previous: dict[str, float],
    failed: int = 0,
    claims: int = 0,
    dropped: int = 0,
) -> list[str]:
    """Три вывода и три действия по правилам, без ИИ (F5)."""
    conclusions: list[str] = []
    actions: list[str] = []
    revenue_diff = (
        (current["revenue"] - previous["revenue"]) / previous["revenue"] * 100
        if previous.get("revenue", 0) > 0
        else 0.0
    )
    if previous.get("revenue", 0) > 0 and revenue_diff <= -10:
        conclusions.append(
            f"Выручка упала на {abs(revenue_diff):.0f}% — теряем объём."
        )
        actions.append(
            "Позвонить 3 клиентам, кто заказывал на прошлой неделе и молчит на этой."
        )
    elif revenue_diff >= 10:
        conclusions.append(f"Выручка выросла на {revenue_diff:.0f}% — спрос есть.")
        actions.append("Спросить выросших клиентов, что зашло, и закрепить это в прайсе.")
    if previous.get("avg", 0) > 0 and current["avg"] < previous["avg"] * 0.9:
        conclusions.append("Средний чек просел — берут меньше за заказ.")
        actions.append("Предложить 2–3 клиентам добавить фасовку или сопутствующее.")
    if failed:
        conclusions.append(f"Недоставок за неделю: {failed}.")
        actions.append("Разобрать причины недоставок в /ops и закрыть повторы.")
    if claims:
        conclusions.append(f"Претензий за неделю: {claims}.")
        actions.append("Закрыть претензии и учесть кредит-ноты в следующем счёте.")
    if dropped:
        conclusions.append(f"Пропали {dropped} клиент(ов) на 2+ недели.")
        actions.append("Отправить им мягкое напоминание из /queue или позвонить.")
    if current["orders"] and current["clients"] and current["orders"] / current["clients"] < 1.2:
        conclusions.append("Почти все заказы — по одному на клиента.")
        actions.append("Напомнить клиентам про недельный заказ одной суммой.")
    lines: list[str] = []
    if conclusions:
        lines.append("")
        lines.append("<b>3 вывода</b>")
        lines.extend(f"{index}. {text}" for index, text in enumerate(conclusions[:3], 1))
    if actions:
        lines.append("<b>3 действия</b>")
        lines.extend(f"{index}. {text}" for index, text in enumerate(actions[:3], 1))
    return lines


def weekly_report(
    orders: list[dict[str, Any]],
    venues: dict[int, str],
    regulars: list[str],
    dropped: list[str],
    start_text: str,
    end_text: str,
    extra_lines: list[str] | None = None,
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
    if extra_lines:
        lines.extend(extra_lines)
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
