"""Списания и усушка: разбор команды и отчёт по потерям (B9).

Менеджер пишет `/writeoff помидоры 3 кг усушка` (или диктует «списание
помидоры 3 усушка»). Потеря записывается с закупочной ценой дня, поэтому
маржа по овощам и зелени перестаёт быть завышенной.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

NUMBER_RE = re.compile(r"(\d+(?:[.,]\d+)?)")
UNIT_RE = re.compile(r"\b(кг|шт|л|уп|упак|кор|коробка|ящик)\b", re.IGNORECASE)
REASONS = {
    "усушка": "усушка",
    "портит": "порча",
    "порча": "порча",
    "просроч": "просрочка",
    "брак": "брак",
    "бой": "бой",
    "подар": "подарок",
    "образц": "образцы",
}


@dataclass
class WriteoffRequest:
    """Разобранная команда списания."""

    phrase: str
    qty: float
    unit: str = ""
    reason: str = ""


def parse_command(text: str) -> WriteoffRequest | None:
    """«помидоры 3 кг усушка» → позиция, количество, единица, причина."""
    raw = str(text or "").strip()
    if not raw:
        return None
    match = NUMBER_RE.search(raw)
    if not match:
        return None
    qty = float(match.group(1).replace(",", "."))
    if qty <= 0:
        return None
    rest = (raw[: match.start()] + " " + raw[match.end():]).strip()
    unit_match = UNIT_RE.search(rest)
    unit = unit_match.group(1).lower() if unit_match else ""
    if unit_match:
        rest = (rest[: unit_match.start()] + " " + rest[unit_match.end():]).strip()
    reason = ""
    lowered = rest.lower()
    for key, label in REASONS.items():
        index = lowered.find(key)
        if index >= 0:
            reason = label
            rest = (rest[:index] + " " + rest[index + len(key):]).strip()
            break
    phrase = re.sub(r"\s+", " ", rest).strip(" ,.;:-")
    if len(phrase) < 2:
        return None
    return WriteoffRequest(phrase=phrase, qty=qty, unit=unit, reason=reason)


def report_lines(rows: list[dict[str, Any]], days: int, limit: int = 5) -> list[str]:
    """Отчёт по потерям: сумма и доля по категориям, топ позиций.

    rows — строки отчёта из базы: category, name, qty, unit, cost_value.
    """
    if not rows:
        return [f"За {days} дней списаний нет."]
    total = sum(float(row.get("cost_value") or 0) for row in rows)
    lines = [f"🗑 <b>Потери за {days} дней</b>: {total:.2f} USD", ""]
    by_category: dict[str, float] = {}
    for row in rows:
        category = str(row.get("category") or "без категории")
        by_category[category] = by_category.get(category, 0.0) + float(row.get("cost_value") or 0)
    lines.append("<b>По категориям</b>")
    for category, value in sorted(by_category.items(), key=lambda kv: -kv[1])[:limit]:
        share = (value / total * 100) if total else 0.0
        lines.append(f"• {category}: {value:.2f} ({share:.0f}% потерь)")
    lines.append("")
    lines.append("<b>Что списывали чаще</b>")
    by_item: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("name") or row.get("catalog_id") or "позиция")
        entry = by_item.setdefault(key, {"qty": 0.0, "unit": row.get("unit") or "", "value": 0.0})
        entry["qty"] += float(row.get("qty") or 0)
        entry["value"] += float(row.get("cost_value") or 0)
    for name, entry in sorted(by_item.items(), key=lambda kv: -kv[1]["value"])[:limit]:
        lines.append(
            f"• {name}: {entry['qty']:g} {entry['unit']} на {entry['value']:.2f}"
        )
    if total and by_category:
        worst = max(by_category.items(), key=lambda kv: kv[1])
        lines.append("")
        lines.append(
            f"Внимание: больше всего теряем на «{worst[0]}» — "
            f"{worst[1] / total * 100:.0f}% потерь."
        )
    return lines
