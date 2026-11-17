"""Интервалы доставки (слоты): настройка и выбор.

Хранятся одной строкой в settings: "09:00-12:00,12:00-15:00,15:00-18:00".
Клиент выбирает слот кнопкой, в заказ пишется время начала интервала.
"""
from __future__ import annotations

from datetime import datetime

DEFAULT_SLOTS = "09:00-12:00,12:00-15:00,15:00-18:00"


def _norm_clock(raw: str) -> str:
    text = raw.strip().replace(".", ":").replace(" ", "")
    if not text:
        raise ValueError(raw)
    if ":" not in text:
        text = f"{text}:00"
    hours, minutes = text.split(":", 1)
    return f"{int(hours):02d}:{int(minutes):02d}"


def parse_slots(raw: str | None) -> list[tuple[str, str]]:
    """Строка настроек -> список интервалов. Испорченное значение игнорируется."""
    text = (raw or "").strip() or DEFAULT_SLOTS
    result: list[tuple[str, str]] = []
    for chunk in text.replace(";", ",").split(","):
        chunk = chunk.strip()
        if "-" not in chunk:
            continue
        start_raw, end_raw = chunk.split("-", 1)
        try:
            start, end = _norm_clock(start_raw), _norm_clock(end_raw)
        except (ValueError, TypeError):
            continue
        if start >= end:
            continue
        if (start, end) not in result:
            result.append((start, end))
    if not result:
        return [("09:00", "12:00"), ("12:00", "15:00"), ("15:00", "18:00")]
    return result


def slot_labels(slots: list[tuple[str, str]]) -> list[str]:
    return [f"{start}–{end}" for start, end in slots]


def default_time(slots: list[tuple[str, str]], now: datetime) -> str:
    """Ближайший ещё не начавшийся интервал (для подсказки по умолчанию)."""
    current = now.strftime("%H:%M")
    for start, _ in slots:
        if start > current:
            return start
    return slots[0][0] if slots else "09:00"
