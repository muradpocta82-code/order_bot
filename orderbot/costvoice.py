"""Ввод закупочных цен голосом или текстом (B5).

Менеджер диктует: «помидоры 8500, лук 3200, огурцы 9000». Бот разбирает пары
«название + цена», находит позиции в прайсе и показывает список с кнопкой
«Записать» — без подтверждения ничего не меняется.

Цены хранятся в `cost_prices` (история по датам), поэтому маржа считается
по закупке, действовавшей на дату заказа.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .catalog import Catalog

PAIR_RE = re.compile(
    r"(?P<name>[^\d,;.]{2,40}?)\s*(?:по|за|—|-)?\s*(?P<price>\d+(?:[.,]\d{1,4})?)\s*(?:\$|сум|у\.е\.|уе)?",
    re.IGNORECASE,
)


@dataclass
class CostLine:
    """Одна распознанная строка: фраза, цена и найденная позиция."""

    phrase: str
    cost: float
    catalog_id: int | None = None
    name: str = ""
    score: float = 0.0

    @property
    def ok(self) -> bool:
        return self.catalog_id is not None and self.cost > 0


def parse_pairs(text: str) -> list[tuple[str, float]]:
    """Разбирает «помидоры 8500, лук 3200» в пары (название, цена)."""
    result: list[tuple[str, float]] = []
    cleaned = str(text or "").replace("\n", ", ")
    for match in PAIR_RE.finditer(cleaned):
        phrase = match.group("name").strip(" ,.;:-")
        raw_price = match.group("price").replace(",", ".")
        try:
            cost = float(raw_price)
        except ValueError:
            continue
        phrase = re.sub(r"^(и|а|ещё|еще)\s+", "", phrase, flags=re.IGNORECASE).strip()
        if len(phrase) < 2 or cost <= 0:
            continue
        result.append((phrase, cost))
    return result


def match_lines(
    pairs: list[tuple[str, float]], catalog: Catalog, min_score: float = 55.0
) -> list[CostLine]:
    """Сопоставляет пары с прайсом; ненайденные остаются с пустым catalog_id."""
    lines: list[CostLine] = []
    for phrase, cost in pairs:
        matches = catalog.match(phrase, limit=1, min_score=min_score)
        if matches:
            item = matches[0].item
            lines.append(
                CostLine(
                    phrase=phrase,
                    cost=cost,
                    catalog_id=item.id,
                    name=item.name,
                    score=float(matches[0].score),
                )
            )
        else:
            lines.append(CostLine(phrase=phrase, cost=cost))
    return lines


def describe(lines: list[CostLine], limit: int = 12) -> str:
    """Текст подтверждения: что запишем, а что не нашли."""
    good = [line for line in lines if line.ok]
    bad = [line for line in lines if not line.ok]
    parts = ["💵 <b>Новые закупочные цены</b>", ""]
    if good:
        for line in good[:limit]:
            parts.append(f"• {line.name} — {line.cost:g}")
    else:
        parts.append("Ничего не распознал.")
    if bad:
        parts.append("")
        parts.append("<b>Не нашёл в прайсе</b>")
        for line in bad[:limit]:
            parts.append(f"• {line.phrase} — {line.cost:g}")
        parts.append("")
        parts.append("Эти строки запишите командой /cost позиция цена.")
    if len(good) > limit:
        parts.append(f"… и ещё {len(good) - limit}")
    return "\n".join(parts)


def total_of(lines: list[CostLine]) -> int:
    return len([line for line in lines if line.ok])


def sample_text(text: str) -> str:
    """Подсказка для менеджера, как диктовать."""
    return (
        "Скажите или напишите список закупок: «помидоры 8500, лук 3200, огурцы 9000».\n"
        f"Ваш текст: <i>{text[:200]}</i>"
    )


def as_payload(lines: list[CostLine]) -> list[dict[str, Any]]:
    """Только найденные строки — для записи в базу после подтверждения."""
    return [
        {"catalog_id": line.catalog_id, "cost": round(float(line.cost), 4)}
        for line in lines
        if line.ok
    ]
