"""Пороги и тексты без деплоя (I5).

Пороги живут в настройках базы: менеджер меняет их командой `/tune`,
бот подхватывает сразу. Тексты — там же, поверх кода: в коде остаётся
значение по умолчанию, в базе — только переопределение.

Здесь чистые функции: список допустимых ключей, разбор значения, разбор
строки «ключ = текст».
"""
from __future__ import annotations

from typing import Any

# ключ: (минимальное, максимальное, значение по умолчанию, описание)
LIMITS: dict[str, tuple[float, float, float, str]] = {
    "forecast_min_samples": (2, 20, 4, "сколько заказов нужно для прогноза по дню недели"),
    "receipt_tolerance_pct": (0, 25, 1, "допуск суммы чека к заказу, %"),
    "eta_spread_min": (0, 120, 15, "разброс окна прибытия, минут"),
    "stock_warn_threshold": (0, 1000, 5, "порог «заканчивается» на складе"),
}

TEXT_KEYS: dict[str, str] = {
    "edit_hint": "подсказка про правку заказа после подтверждения",
    "arrival": "текст «курьер в пути» клиенту",
    "thanks": "короткое спасибо после оценки распознавания",
}


def clean_key(raw: str | None) -> str:
    return str(raw or "").strip().lower().replace("-", "_")


def parse_value(key: str, raw: str | None) -> float | None:
    """Значение порога в допустимых пределах или None."""
    name = clean_key(key)
    if name not in LIMITS:
        return None
    low, high, _default, _desc = LIMITS[name]
    text = str(raw or "").replace(",", ".").replace("%", "").strip()
    try:
        value = float(text)
    except ValueError:
        return None
    if value < low or value > high:
        return None
    return round(value, 2)


def format_value(key: str, value: float) -> str:
    if float(value) == int(value):
        return f"{int(value)}"
    return f"{value:g}"


def list_lines(values: dict[str, Any]) -> list[str]:
    """Строки «ключ — значение (описание)» для /tune без аргументов."""
    lines: list[str] = []
    for key, (_low, _high, default, desc) in LIMITS.items():
        raw = values.get(key)
        current = format_value(key, float(raw)) if raw is not None else format_value(key, float(default))
        mark = "" if raw is None else " ← изменено"
        lines.append(f"• <code>{key}</code> = <b>{current}</b>{mark}\n  <i>{desc}</i>")
    return lines


def parse_text_assignment(raw: str | None) -> tuple[str, str] | None:
    """«edit_hint = изменить до 18:00» → (key, text)."""
    text = str(raw or "").strip()
    if "=" not in text:
        return None
    key, value = text.split("=", 1)
    name = clean_key(key)
    value = " ".join(value.split())
    if name not in TEXT_KEYS or not value:
        return None
    return name, value[:600]


async def threshold(db, key: str) -> float:
    """Порог из настроек или значение по умолчанию."""
    name = clean_key(key)
    if name not in LIMITS:
        raise KeyError(key)
    default = LIMITS[name][2]
    raw = (await db.get_setting(f"tune:{name}", "") or "").strip()
    value = parse_value(name, raw) if raw else None
    return float(default if value is None else value)


async def text(db, key: str, default: str) -> str:
    """Текст из настроек или значение по умолчанию из кода."""
    name = clean_key(key)
    raw = (await db.get_setting(f"text:{name}", "") or "").strip()
    return raw or default


def unknown_text_key(raw: str | None) -> bool:
    return clean_key(raw) not in TEXT_KEYS
