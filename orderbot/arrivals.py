"""G7: приход товара текстом — остатки и закупочные цены одной строкой.

Менеджер пишет: `/income помидоры 120 кг по 1.10, огурцы 80 кг по 0.90`.
Бот находит позиции в прайсе, увеличивает остаток, записывает закупочную цену
(с даты, поэтому маржа считается по правильной цене) и пишет приход в журнал
`arrivals`. Так одной командой закрываются и остатки, и закупка для /pricenew.

Разбор живёт здесь, чтобы его покрывали тесты без Telegram.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .catalog import Catalog
from .units import canon_unit

COST_RE = re.compile(r"(?:по|за|@)\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
UNIT_RE = re.compile(
    r"(кг|г|гр|л|мл|шт|уп|упак[\w]*|ящ[\w]*|пач[\w]*|бут[\w]*)", re.IGNORECASE
)
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
SPLIT_RE = re.compile(r",(?=\s|[^\d\s])|[;\n]+")
LEAD_RE = re.compile(
    r"^(?:приход|привезли|приняли|получили|и|а|ещё|еще)\s*:?\s+", re.IGNORECASE
)

MAX_QTY = 100000.0
MAX_COST = 100000.0


@dataclass(frozen=True)
class ArrivalLine:
    """Одна строка прихода: что, сколько, по чём и что нашлось в прайсе."""

    phrase: str
    qty: float | None
    unit: str
    cost: float | None
    catalog_id: int | None = None
    name: str = ""
    stock_unit: str = ""
    score: float = 0.0

    @property
    def ok(self) -> bool:
        return self.catalog_id is not None

    @property
    def unit_matches(self) -> bool:
        if self.catalog_id is None:
            return False
        if not self.unit or not self.stock_unit:
            return True
        return _unit(self.unit) == _unit(self.stock_unit)

    @property
    def adds_stock(self) -> bool:
        return self.ok and self.qty is not None and self.unit_matches


def _number(raw: str) -> float:
    return float(str(raw).replace(",", "."))


def _unit(raw: str | None) -> str:
    """Каноническая единица, если знаем такую; иначе слово как есть."""
    word = str(raw or "").strip().lower()
    if not word:
        return ""
    return canon_unit(word) or word


def _cut_unit(tail: str) -> tuple[str, str]:
    """Отделяет единицу от хвоста строки: «кг, » → («кг», «, »)."""
    match = UNIT_RE.search(str(tail or "")[:12])
    if not match:
        return "", str(tail or "")
    return _unit(match.group(1)), str(tail or "")[match.end():]


def parse_line(segment: str) -> ArrivalLine | None:
    """Разбирает один кусок: «помидоры 120 кг по 1.10»."""
    text = LEAD_RE.sub("", str(segment or "").strip())
    if len(text) < 3:
        return None
    qty: float | None = None
    unit = ""
    cost: float | None = None
    phrase = text
    match = COST_RE.search(text)
    if match:
        cost = _number(match.group(1))
        body = text[: match.start()]
        numbers = list(NUMBER_RE.finditer(body))
        if numbers:
            qty_match = numbers[-1]
            qty = _number(qty_match.group())
            unit, _rest = _cut_unit(body[qty_match.end():])
            phrase = body[: qty_match.start()]
        else:
            phrase = body
    else:
        numbers = list(NUMBER_RE.finditer(text))
        if not numbers:
            return None
        if len(numbers) == 1:
            single = numbers[0]
            unit_after, _rest = _cut_unit(text[single.end():])
            if unit_after:
                # «филе 20 кг» — это количество, цены в строке нет.
                qty = _number(single.group())
                unit = unit_after
                phrase = text[: single.start()]
            else:
                cost = _number(single.group())
                phrase = text[: single.start()]
        else:
            qty_match, cost_match = numbers[-2], numbers[-1]
            qty = _number(qty_match.group())
            cost = _number(cost_match.group())
            unit, _rest = _cut_unit(text[qty_match.end(): cost_match.start()])
            phrase = text[: qty_match.start()]
    phrase = re.sub(r"\s+", " ", phrase.strip(" ,.;:-—")).strip()
    if len(phrase) < 2:
        return None
    if cost is not None and not (0 < cost <= MAX_COST):
        return None
    if qty is not None and not (0 < qty <= MAX_QTY):
        return None
    return ArrivalLine(phrase=phrase, qty=qty, unit=unit, cost=cost)


def parse(text: str) -> list[ArrivalLine]:
    """Разбирает весь текст прихода на строки."""
    result: list[ArrivalLine] = []
    for chunk in SPLIT_RE.split(str(text or "")):
        line = parse_line(chunk)
        if line is not None:
            result.append(line)
    return result


def match(
    lines: list[ArrivalLine], catalog: Catalog, min_score: float = 55.0
) -> list[ArrivalLine]:
    """Сопоставляет строки с прайсом; ненайденные остаются без catalog_id."""
    result: list[ArrivalLine] = []
    for line in lines:
        matches = catalog.match(line.phrase, limit=1, min_score=min_score)
        if not matches:
            result.append(line)
            continue
        item = matches[0].item
        result.append(
            ArrivalLine(
                phrase=line.phrase,
                qty=line.qty,
                unit=line.unit or item.unit,
                cost=line.cost,
                catalog_id=item.id,
                name=item.name,
                stock_unit=item.unit,
                score=float(matches[0].score),
            )
        )
    return result


def describe(lines: list[ArrivalLine], limit: int = 15) -> str:
    """Текст для менеджера: что приняли и что не нашли."""
    found = [line for line in lines if line.ok]
    missing = [line for line in lines if not line.ok]
    out = ["🚚 <b>Приход</b>", ""]
    if found:
        for line in found[:limit]:
            qty = f"{line.qty:g} {line.unit}".strip() if line.qty else "без количества"
            cost = f"по {line.cost:g}" if line.cost else "без цены"
            out.append(f"• {line.name} — {qty} {cost}")
        if len(found) > limit:
            out.append(f"…и ещё {len(found) - limit}")
    if missing:
        out.append("")
        out.append("❓ Не нашёл в прайсе:")
        for line in missing[:5]:
            out.append(f"• {line.phrase}")
        if len(missing) > 5:
            out.append(f"…и ещё {len(missing) - 5}")
    return "\n".join(out)


def totals(lines: list[ArrivalLine]) -> tuple[int, int]:
    """Сколько строк пополнили остаток и сколько записали только цену."""
    with_stock = sum(1 for line in lines if line.adds_stock)
    return with_stock, len(lines) - with_stock


def sample_text() -> str:
    return (
        "Формат: <code>/income помидоры 120 кг по 1.10, огурцы 80 кг по 0.90</code>\n"
        "Цены — в тех же деньгах, что прайс (USD). Единица без указания — как в прайсе.\n"
        "Одной командой обновляются остатки и закупочные цены."
    )
