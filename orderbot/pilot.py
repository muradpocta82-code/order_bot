"""Метрики пилота (H5): чем бот реально помог за месяц.

Считается по своим же данным: сколько заказов пришло через бота, как быстро
собирали и доставляли, сколько было вопросов и претензий, сколько клиентов
активны и какой средний чек. Только чистые функции — база отдаёт цифры.
"""
from __future__ import annotations

from typing import Any

SOURCE_NAMES = {
    "text": "текст",
    "voice": "голос",
    "photo": "фото",
    "webapp": "мини-апп",
    "repeat": "повтор",
    "usual": "как обычно",
    "manual": "вручную",
    "eval": "проверка",
    "test": "тест",
}


def share(part: float, total: float) -> float:
    """Доля в процентах, без деления на ноль."""
    total = float(total or 0)
    if total <= 0:
        return 0.0
    return round(float(part or 0) / total * 100, 1)


def source_name(source: str | None) -> str:
    key = str(source or "").strip().lower()
    return SOURCE_NAMES.get(key, key or "неизвестно")


def text(stats: dict[str, Any], days: int = 30) -> str:
    """Текст отчёта в HTML-разметке Telegram."""
    orders = int(stats.get("orders") or 0)
    if not orders:
        return f"🎯 <b>Пилот за {days} дней</b>\n\nЗаказов пока нет."
    revenue = float(stats.get("revenue") or 0)
    clients = int(stats.get("clients") or 0)
    sources = stats.get("sources") or []
    bot_orders = sum(
        int(row.get("n") or 0)
        for row in sources
        if str(row.get("source") or "") not in ("manual", "test", "eval")
    )
    lines = [
        f"🎯 <b>Пилот за {days} дней</b>",
        "",
        f"Заказов: <b>{orders}</b> на <b>${revenue:.2f}</b>",
        f"Средний чек: <b>${revenue / orders:.2f}</b>",
        f"Активных клиентов: <b>{clients}</b> (по {orders / max(1, clients):.1f} заказа)",
        f"Через бота: <b>{share(bot_orders, orders):.0f}%</b> заказов",
    ]
    if sources:
        parts = [
            f"{source_name(row.get('source'))} — {int(row.get('n') or 0)}"
            for row in sources[:6]
        ]
        lines.append("Источники: " + ", ".join(parts))
    lines.append("")
    clean = int(stats.get("clean") or 0)
    avg_q = float(stats.get("avg_q") or 0)
    lines.append(f"Без единого вопроса: <b>{share(clean, orders):.0f}%</b> заказов")
    lines.append(f"Вопросов на заказ: <b>{avg_q:.1f}</b>")
    pack_avg = stats.get("pack_avg")
    if pack_avg is not None:
        lines.append(f"Заказ → собран: <b>{float(pack_avg):.0f} мин</b>")
    deliver_avg = stats.get("deliver_avg")
    if deliver_avg is not None:
        lines.append(f"Собран → доставлен: <b>{float(deliver_avg):.0f} мин</b>")
    claims = int(stats.get("claims") or 0)
    lines.append(f"Претензий: <b>{claims}</b> ({share(claims, orders):.1f}% заказов)")
    failed = stats.get("failed_total")
    if failed:
        lines.append(f"Сбоев доставки: <b>{int(failed)}</b>")
    lines.append("")
    lines.append("<i>Цифры «до бота» владелец сравнивает сам: часы менеджера в неделю, "
                 "ошибки в заказах, доля заказов через бота.</i>")
    return "\n".join(lines)
