"""C10: личный ритм клиента — «X не заказывал 9 дней, обычно каждые 4».

Считаем медианный интервал между заказами каждого клиента (нужно минимум
3 заказа). Если молчание дольше привычного ритма — менеджер получает строку
с кнопкой «Напомнить»: бот отправляет клиенту мягкое напоминание с кнопкой
«Как обычно», без давления.

Здесь чистая логика: медиана, решение по молчанию, тексты.
"""
from __future__ import annotations

from datetime import date
from statistics import median

MIN_ORDERS = 3
SILENCE_FACTOR = 1.6      # молчание дольше ритма в 1.6 раза
MIN_SILENCE_DAYS = 3      # раньше трёх дней не напоминаем


def order_dates(days: list[str]) -> list[date]:
    """Превращает список ISO-дат в отсортированный список без повторов."""
    result: set[date] = set()
    for raw in days:
        try:
            result.add(date.fromisoformat(str(raw)[:10]))
        except ValueError:
            continue
    return sorted(result)


def median_interval(days: list[str], min_orders: int = MIN_ORDERS) -> float | None:
    """Привычный интервал между заказами в днях (медиана), иначе None."""
    unique = order_dates(days)
    if len(unique) < min_orders:
        return None
    gaps = [
        (unique[index + 1] - unique[index]).days
        for index in range(len(unique) - 1)
    ]
    gaps = [gap for gap in gaps if gap > 0]
    if not gaps:
        return None
    return float(median(gaps))


def is_silent(
    days: list[str],
    today: date,
    factor: float = SILENCE_FACTOR,
    min_silence: int = MIN_SILENCE_DAYS,
) -> tuple[bool, int, float | None]:
    """Молчит ли клиент дольше своего ритма. Возвращает (да, дней молчания, ритм)."""
    unique = order_dates(days)
    interval = median_interval(days)
    if interval is None or not unique:
        return False, 0, interval
    silent_days = (today - unique[-1]).days
    threshold = max(min_silence, round(interval * factor))
    return silent_days >= threshold, silent_days, interval


def alert_line(name: str, silent_days: int, interval: float | None) -> str:
    rhythm = f", обычно каждые {interval:.0f} дн." if interval else ""
    return f"• {name} — молчит {silent_days} дн.{rhythm}"


def alert_text(rows: list[dict], limit: int = 8) -> str:
    """Сводка для менеджера: кто выпал из ритма."""
    if not rows:
        return "💤 Все клиенты заказывают в своём ритме — напоминать некому."
    lines = ["💤 <b>Выпали из ритма</b>", ""]
    for row in rows[:limit]:
        lines.append(
            alert_line(
                str(row.get("name") or row.get("chef_id")),
                int(row.get("silent_days") or 0),
                row.get("interval"),
            )
        )
    if len(rows) > limit:
        lines.append(f"…и ещё {len(rows) - limit}")
    lines.append("")
    lines.append("Кнопка «Напомнить» пришлёт клиенту мягкое «собрать как обычно».")
    return "\n".join(lines)


def client_text(interval: float | None) -> str:
    rhythm = f"обычно вы заказываете раз в {interval:.0f} дн." if interval else "вы давно не заказывали"
    return (
        f"Здравствуйте! {rhythm.capitalize()} — собрать заказ как обычно? "
        "Проверите состав и подтвердите."
    )
