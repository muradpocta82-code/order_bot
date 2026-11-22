"""Прогноз закупки по дню недели (F8).

В оптовой поставке заказы повторяются по ритму: понедельник — мясо,
пятница — рыба и зелень. Здесь считается «сколько обычно уходит в этот
день недели» по истории продаж. Пока истории мало — честно говорим об этом.

Только чистые функции: база отдаёт историю, решение — здесь.
"""
from __future__ import annotations

from typing import Any

MIN_SAMPLES = 4
WINDOW_WEEKS = 8


def weekday_forecast(
    history: list[dict[str, Any]],
    weekday: int,
    min_samples: int = MIN_SAMPLES,
) -> dict[str, Any]:
    """Средний расход в этот день недели.

    history: [{"date": "2026-11-03", "weekday": 1, "qty": 12.5}, ...]
    Возвращает {"ready", "samples", "avg"}; ready=False, если дней мало.
    """
    values: list[float] = []
    for row in history:
        try:
            if int(row.get("weekday", -1)) != int(weekday):
                continue
            qty = float(row.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty > 0:
            values.append(qty)
    if len(values) < max(1, int(min_samples)):
        return {"ready": False, "samples": len(values), "avg": 0.0}
    return {
        "ready": True,
        "samples": len(values),
        "avg": round(sum(values) / len(values), 3),
    }


def add_forecasts(
    rows: list[dict[str, Any]],
    history: dict[int, list[dict[str, Any]]],
    weekday: int,
    min_samples: int = MIN_SAMPLES,
) -> list[dict[str, Any]]:
    """Проставляет прогноз в строки закупки: row["forecast"]."""
    for row in rows:
        catalog_id = row.get("catalog_id")
        try:
            key = int(catalog_id) if catalog_id else 0
        except (TypeError, ValueError):
            key = 0
        if not key:
            row["forecast"] = {"ready": False, "samples": 0, "avg": 0.0}
            continue
        row["forecast"] = weekday_forecast(
            history.get(key, []), weekday, min_samples
        )
    return rows


def summary_lines(
    rows: list[dict[str, Any]], limit: int = 6
) -> list[str]:
    """Строки «обычно в этот день» для подсказки в закупке."""
    ready = [
        row
        for row in rows
        if (row.get("forecast") or {}).get("ready")
        and float((row.get("forecast") or {}).get("avg") or 0) > 0
    ]
    ready.sort(key=lambda row: -float(row["forecast"]["avg"]))
    lines: list[str] = []
    for row in ready[:limit]:
        unit = row.get("unit") or ""
        avg = float(row["forecast"]["avg"])
        amount = f"{int(round(avg))}" if abs(avg - round(avg)) < 0.001 else f"{avg:.1f}"
        lines.append(f"• {row.get('name') or '—'}: ≈{amount} {unit}")
    return lines
