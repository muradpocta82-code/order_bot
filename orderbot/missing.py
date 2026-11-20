"""B11: спрос без предложения — что просят, но чего нет в прайсе.

Повара иногда спрашивают позиции, которых нет в прайсе (кнопка «Нет в прайсе»).
Каждый такой запрос пишется в `missing_requests` с клиентом и датой, поэтому
менеджер видит не только «слово», но и «кто просил».

Раз в неделю бот присылает сводку «топ запросов», а `/missadd трюфель 12.50`
добавляет позицию в прайс и сообщает тем, кто её просил.

Здесь только чистые функции: разбор команды и текст сводки.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .units import canon_unit

MAX_PRICE = 100000.0
SPACES_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class NewItem:
    phrase: str
    name: str
    price: float
    unit: str
    category: str


def normalize_phrase(text: str) -> str:
    return SPACES_RE.sub(" ", str(text or "").lower().replace("ё", "е")).strip()


def parse_missadd(args: str) -> NewItem | None:
    """Разбирает «трюфель 12.50 кг Сыры» → название, цена, единица, категория."""
    tokens = SPACES_RE.split(str(args or "").strip())
    tokens = [token for token in tokens if token]
    if len(tokens) < 2:
        return None
    price: float | None = None
    price_at: int | None = None
    for index in range(len(tokens) - 1, -1, -1):
        raw = tokens[index].replace(",", ".").replace("$", "")
        try:
            value = round(float(raw), 2)
        except ValueError:
            continue
        price = value
        price_at = index
        break
    if price is None or price_at is None or not (0 < price <= MAX_PRICE):
        return None
    phrase = " ".join(tokens[:price_at]).strip(" ,.;:-")
    if len(phrase) < 2:
        return None
    rest = tokens[price_at + 1:]
    unit = ""
    if rest:
        canon = canon_unit(rest[0])
        if canon:
            unit = canon
            rest = rest[1:]
    category = " ".join(rest).strip(" ,.;:-")
    name = phrase[:1].upper() + phrase[1:]
    return NewItem(
        phrase=normalize_phrase(phrase),
        name=name,
        price=price,
        unit=unit or "шт",
        category=category,
    )


def digest_text(rows: list[dict], days: int = 30, limit: int = 10) -> str:
    """Сводка «что просят, а у нас нет»."""
    header = f"🗒 <b>Просят, но нет в прайсе</b> (за {days} дней)"
    if not rows:
        return header + "\n\nПусто — все запросы закрыты прайсом."
    lines = [header, ""]
    for position, row in enumerate(rows[:limit], start=1):
        phrase = row.get("phrase") or "запрос"
        count = int(row.get("count") or 0)
        users = int(row.get("users") or 0)
        suffix = f", клиентов: {users}" if users else ""
        lines.append(f"{position}. «{phrase}» — {count} раз(а){suffix}")
    if len(rows) > limit:
        lines.append(f"…и ещё {len(rows) - limit}")
    first = (rows[0].get("phrase") or "")
    lines.append("")
    lines.append(
        f"Добавить в прайс: <code>/missadd {first} 12.50 кг</code> — "
        "клиентам, которые просили, бот сообщит сам."
    )
    return "\n".join(lines)


def client_notice(name: str, price: float, unit: str) -> str:
    """Сообщение клиенту, который просил позицию."""
    return (
        f"Хорошая новость: в прайсе появилось «{name}» — "
        f"${price:.2f}/{unit}. Напишите, сколько нужно, и я передам заказ."
    )
