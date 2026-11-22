"""Формирование HTML-сообщений для Telegram."""
from __future__ import annotations

import html
from datetime import date, datetime
from typing import Any

from .catalog import Catalog
from .drafts import draft_total
from .units import format_qty, plural

STATUS_LABELS = {
    "new": "🆕 новый",
    "accepted": "🔧 принят в работу",
    "ready": "📦 собран",
    "delivering": "🚚 в доставке",
    "done": "✅ доставлен",
    "cancelled": "❌ отменён",
    "failed": "⚠️ не доставлен",
}

LOCAL_RATE = 0.0
LOCAL_CURRENCY = "сум"


def set_local_rate(rate: float, currency: str = "TMT") -> None:
    """Курс для показа суммы в местной валюте (0 — не показывать)."""
    global LOCAL_RATE, LOCAL_CURRENCY
    LOCAL_RATE = max(0.0, float(rate or 0))
    LOCAL_CURRENCY = (currency or "TMT").strip() or "TMT"


def order_local_money(order: dict[str, Any]) -> str:
    """Сумма в местной валюте по курсу самого заказа (старые заказы не «плывут»)."""
    try:
        rate = float(order.get("usd_rate_at_order") or 0)
    except (TypeError, ValueError):
        rate = 0.0
    if rate <= 0:
        rate = LOCAL_RATE
    if rate <= 0:
        return ""
    total = float(order.get("total") or 0)
    if not total:
        return ""
    amount = round(total * rate)
    return f"≈ {amount:,}".replace(",", " ") + f" {LOCAL_CURRENCY}"


def local_money(value: float | None) -> str:
    if not value or LOCAL_RATE <= 0:
        return ""
    amount = float(value) * LOCAL_RATE
    return f"≈ {round(amount):,}".replace(",", " ") + f" {LOCAL_CURRENCY}"


PAYMENT_METHOD_LABELS = {
    "cash": "💵 наличными при получении",
    "usdt": "🛰 USDT (TON)",
    "transfer": "🏦 перевод по чеку",
    "credit": "📝 в долг",
    "legacy": "оплачен ранее",
}

PAYMENT_STATUS_LABELS = {
    "unpaid": "не оплачен",
    "paid": "оплачен",
}


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def clip(value: Any, limit: int = 500) -> str:
    """Обрезает длинный текст, чтобы сообщение Telegram не превысило 4096."""
    text = str(value if value is not None else "")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Режет длинное сообщение по строкам, не разрывая HTML-теги строк."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        if len(line) > limit:
            line = line[: limit - 1].rstrip() + "…"
        if current and size + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def money(value: float | None) -> str:
    if value is None:
        return "—"
    number = float(value)
    if abs(number - round(number)) < 0.005:
        return "$" + f"{round(number):,}".replace(",", " ")
    return "$" + f"{number:,.2f}".replace(",", " ")


def payment_line(order: dict[str, Any]) -> str:
    method = PAYMENT_METHOD_LABELS.get(
        str(order.get("payment_method") or "cash"),
        esc(order.get("payment_method") or "наличными"),
    )
    status = PAYMENT_STATUS_LABELS.get(
        str(order.get("payment_status") or "unpaid"),
        esc(order.get("payment_status") or "не оплачен"),
    )
    return f"{method} · {status}"


def date_ru(value: str | date | None) -> str:
    if not value:
        return "не указана"
    try:
        day = value if isinstance(value, date) else date.fromisoformat(str(value))
    except ValueError:
        return esc(value)
    return day.strftime("%d.%m.%Y")


def delivery_line(date_value: str | None, time_value: str | None) -> str:
    text = date_ru(date_value)
    if time_value:
        text += f" к {esc(time_value)}"
    return text


def item_qty(item: dict[str, Any]) -> str:
    qty = item.get("final_qty")
    if qty is None:
        qty = item.get("qty")
    unit = item.get("final_unit") or item.get("unit") or "шт"
    return f"{format_qty(qty)} {plural(unit, qty)}"


def item_line(index: int, item: dict[str, Any]) -> str:
    status = item.get("status")
    if status == "not_in_catalog":
        return (
            f"{index}. «{esc(clip(item.get('raw'), 120))}» — {item_qty(item)} — "
            f"<i>этого нет у нас в списке, менеджер уточнит сам</i>"
        )
    name = item.get("name") or item.get("raw") or "позиция"
    quantity = item_qty(item)
    price = item.get("price")
    total = item.get("sum")
    if item.get("unit_warning"):
        return (
            f"{index}. <b>{esc(name)}</b> — {quantity} — "
            f"<i>цена за {esc(item.get('catalog_unit') or 'ед.')}: {money(price)}</i> ⚠️"
        )
    if price is None or total is None:
        return f"{index}. <b>{esc(name)}</b> — {quantity}"
    return (
        f"{index}. <b>{esc(name)}</b> — {quantity} × {money(price)} = {money(total)}"
    )


def format_draft(draft: dict[str, Any]) -> str:
    items = [item for item in draft.get("items", []) if item.get("status") != "skipped"]
    lines = [
        "🧾 <b>Заказ</b>",
        f"👤 Клиент: {esc(draft.get('user_name'))}"
        + (f" (@{esc(draft['username'])})" if draft.get("username") else ""),
        f"📅 Доставка: <b>{delivery_line(draft.get('delivery_date'), draft.get('delivery_time'))}</b>",
        "",
    ]
    if not items:
        lines.append("Не понял заказ. Напишите, например: «2 кг филе, завтра к 10».")
    for position, item in enumerate(items, start=1):
        lines.append(item_line(position, item))

    skipped = [item for item in draft.get("items", []) if item.get("status") == "skipped"]
    if skipped:
        lines.append("")
        lines.append("Пропущено: " + ", ".join(f"«{esc(i.get('raw'))}»" for i in skipped))

    warnings = [item for item in items if item.get("unit_warning")]
    if warnings:
        lines.append("")
        lines.append("Единицы не совпадают — менеджер уточнит фасовку.")

    lines.append("")
    lines.append(f"💰 <b>Итого: {money(draft_total(draft))}</b>")
    if draft.get("comment"):
        lines.append(f"📝 Комментарий: {esc(clip(draft['comment']))}")
    if draft.get("raw_text") and draft.get("source") == "voice":
        lines.append(f"🎙 Расшифровка: <i>{esc(clip(draft['raw_text'], 600))}</i>")
    lines.append("")
    lines.append("<i>Проверьте заказ.</i>")
    return "\n".join(lines)


def format_order(order: dict[str, Any], title: str = "Заказ") -> str:
    items = [item for item in order.get("items", []) if item.get("status") != "skipped"]
    chef_name = esc(order.get("chef_name"))
    if order.get("chef_username"):
        chef_name = (
            f'<a href="https://t.me/{esc(order["chef_username"])}">{chef_name}</a>'
        )
    lines = [
        f"{title} №<b>{order.get('id')}</b>",
        f"👤 Клиент: {chef_name}",
        f"📅 Доставка: <b>{delivery_line(order.get('delivery_date'), order.get('delivery_time'))}</b>",
    ]
    if order.get("venue"):
        lines.append(f"🏠 Заведение: {esc(order['venue'])}")
    if order.get("delivery_address"):
        lines.append(f"📍 Адрес: {esc(order['delivery_address'])}")
    if order.get("delivery_phone"):
        lines.append(f"📞 Телефон: {esc(order['delivery_phone'])}")
    lines.append(f"💰 Оплата: {payment_line(order)}")
    if order.get("client_note"):
        lines.append(f"📌 {esc(order['client_note'])}")
    lines.append("")
    for position, item in enumerate(items, start=1):
        if item.get("status") == "not_in_catalog":
            lines.append(
                f"{position}. «{esc(clip(item.get('raw'), 120))}» — "
                f"{format_qty(item.get('qty'))} "
                f"{plural(item.get('unit'), item.get('qty'))} — <i>нет в списке</i>"
            )
            continue
        qty = item.get("qty")
        unit = item.get("unit")
        if item.get("status") == "unit_warning":
            lines.append(
                f"{position}. <b>{esc(item.get('name'))}</b> — "
                f"{format_qty(qty)} {plural(unit, qty)} — "
                "<i>единицы не совпадают — менеджер уточнит фасовку</i>"
            )
            continue
        price = item.get("price") or 0
        total = item.get("total") or 0
        line = (
            f"{position}. <b>{esc(item.get('name'))}</b> — {format_qty(qty)} {plural(unit, qty)}"
            + (f" × {money(price)} = {money(total)}" if price else "")
        )
        if item.get("stock_note"):
            line += f" — ⚠️ {esc(item['stock_note'])}"
        lines.append(line)
    lines.append("")
    lines.append(f"💰 <b>Итого: {money(order.get('total'))}</b>")
    if order.get("comment"):
        lines.append(f"📝 Комментарий: {esc(clip(order['comment']))}")
    lines.append(f"📌 Статус: {STATUS_LABELS.get(order.get('status'), esc(order.get('status')))}")
    return "\n".join(lines)


def edit_hint(cutoff_raw: str | None, delivery_date: str | None) -> str:
    """Когда ещё можно поправить заказ (C16).

    Отсечка действует на заказы «на завтра», поэтому и подсказка честная:
    до какого времени правки успевают в тот же день.
    """
    cutoff = str(cutoff_raw or "").strip()
    if not cutoff:
        return "✏️ Изменить состав можно, пока заказ не начали собирать — напишите «изменить»."
    try:
        day = str(delivery_date or "")[:10]
        return (
            f"✏️ Изменить или отменить можно до <b>{cutoff}</b> "
            f"(доставка {day[8:10]}.{day[5:7]}) — напишите «изменить»."
            if day
            else f"✏️ Изменить можно до <b>{cutoff}</b> — напишите «изменить»."
        )
    except Exception:
        return "✏️ Изменить состав можно, пока заказ не начали собирать."


def format_client_order(order: dict[str, Any], title: str = "✅ Заказ") -> str:
    """Короткая карточка для клиента: только состав, сумма и доставка."""
    lines = [f"{title} №<b>{order.get('id')}</b>", ""]
    items = [item for item in order.get("items", []) if item.get("status") != "skipped"]
    for position, item in enumerate(items, start=1):
        qty = item.get("qty")
        unit = item.get("unit")
        name = item.get("name") or item.get("raw") or "позиция"
        if item.get("status") == "not_in_catalog":
            lines.append(f"{position}. {esc(name)} — {format_qty(qty)} {plural(unit, qty)}")
        else:
            price = item.get("price") or 0
            total = item.get("total") or 0
            line = f"{position}. {esc(name)} — {format_qty(qty)} {plural(unit, qty)}"
            if price and total:
                line += f" = {money(total)}"
            lines.append(line)
    lines.append("")
    lines.append(f"💰 <b>{money(order.get('total'))}</b>")
    lines.append(
        f"📅 {delivery_line(order.get('delivery_date'), order.get('delivery_time'))}"
    )
    if order.get("comment"):
        lines.append(f"📝 {esc(clip(order['comment']))}")
    if order.get("delivery_pin"):
        lines.append(f"🔑 Код получения: <b>{esc(order['delivery_pin'])}</b>")
    lines.append(f"💳 Оплата: {payment_line(order)}")
    order_local = order_local_money(order)
    if order_local:
        lines.append(f"💱 {order_local}")
    return "\n".join(lines)


def format_candidates_question(item: dict[str, Any]) -> str:
    base = f"«{esc(clip(item.get('raw'), 200))}» — это что именно?"
    if item.get("candidates"):
        return base
    return base + "\n\nТакого у нас нет. Нажмите «Нет в списке» или напишите иначе."


def format_qty_question(item: dict[str, Any]) -> str:
    name = item.get("name") or item.get("raw")
    pack = item.get("pack")
    suffix = f" ({esc(pack)})" if pack else ""
    return f"Сколько нужно: <b>{esc(name)}</b>{suffix}?"


def format_catalog(catalog: Catalog, hidden: set[int] | None = None) -> str:
    """Прайс для клиента: позиции без остатка и снятые с продажи скрываем (G6)."""
    hidden = hidden or set()
    lines = ["📋 <b>Наш список</b>", ""]
    current_category = None
    skipped = 0
    for item in catalog.items:
        if item.id in hidden:
            skipped += 1
            continue
        category = item.category or "Прочее"
        if category != current_category:
            if current_category is not None:
                lines.append("")
            lines.append(f"<b>{esc(category)}</b>")
            current_category = category
        lines.append(
            f"• {esc(item.name)} ({esc(item.pack)}) — {money(item.price)}/{esc(item.unit)}"
        )
    if skipped:
        lines.append("")
        lines.append(f"Ещё {skipped} поз. сейчас нет в наличии — спросите менеджера.")
    return "\n".join(lines)


def _times_word(value: int) -> str:
    number = abs(int(value)) % 100
    if 11 <= number <= 14:
        return "раз"
    last = number % 10
    if last == 1:
        return "раз"
    if 2 <= last <= 4:
        return "раза"
    return "раз"


def format_missing(rows: list[dict[str, Any]], days: int = 30) -> str:
    lines = ["🗒 <b>Чего нет в прайсе</b>", f"За {days} дней:", ""]
    if not rows:
        lines.append("Пока пусто.")
        return "\n".join(lines)
    for position, row in enumerate(rows, start=1):
        count = int(row.get("count") or 0)
        lines.append(
            f"{position}. «{esc(row.get('phrase'))}» — {count} {_times_word(count)}"
        )
    return "\n".join(lines)


def format_quality(stats: dict[str, Any], days: int = 30) -> str:
    orders = int(stats.get("orders") or 0)
    if not orders:
        return f"📊 <b>Качество за {days} дней</b>\n\nЗаказов пока нет."
    clean = int(stats.get("clean") or 0)
    average = float(stats.get("avg") or 0)
    lines = [
        f"📊 <b>Качество за {days} дней</b>",
        "",
        f"Заказов: <b>{orders}</b>",
        f"Без единого вопроса: <b>{clean / orders * 100:.0f}%</b>",
        f"Вопросов на заказ: <b>{average:.1f}</b>",
    ]
    top = stats.get("top") or []
    if top:
        lines.append("")
        lines.append("Чаще всего переспрашивал:")
        for position, row in enumerate(top, start=1):
            count = int(row.get("c") or row.get("count") or 0)
            lines.append(
                f"{position}. «{esc(row.get('phrase'))}» — {count} {_times_word(count)}"
            )
    return "\n".join(lines)


def format_order_list(orders: list[dict[str, Any]]) -> str:
    if not orders:
        return "Заказов пока нет."
    lines = ["📦 <b>Последние заказы</b>", ""]
    for order in orders:
        lines.append(
            f"№<b>{order['id']}</b> · {esc(order.get('created_at', '')[:16])} · "
            f"{money(order.get('total'))} · {STATUS_LABELS.get(order.get('status'), esc(order.get('status')))}"
        )
    lines.append("")
    lines.append("Подробнее: <code>/order НОМЕР</code>")
    return "\n".join(lines)


def _short_dt(value: str | None) -> str:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return str(value or "")[:16]


def _short_name(value: str | None, limit: int = 18) -> str:
    text = " ".join(str(value or "—").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_orders_page(
    orders: list[dict[str, Any]],
    page: int,
    pages: int,
    total: int,
    title: str,
) -> str:
    lines = [f"{title} — <b>{total}</b>", ""]
    if not orders:
        lines.append("Пока пусто.")
        return "\n".join(lines)
    for order in orders:
        status = STATUS_LABELS.get(order.get("status"), esc(order.get("status")))
        lines.append(
            f"№<b>{order['id']}</b> · {_short_dt(order.get('created_at'))} · "
            f"🚚 {delivery_line(order.get('delivery_date'), order.get('delivery_time'))} · "
            f"{esc(_short_name(order.get('chef_name')))} · {money(order.get('total'))} · {status}"
        )
    if pages > 1:
        lines.append("")
        lines.append(f"<i>Страница {page + 1} из {pages}</i>")
    return "\n".join(lines)
