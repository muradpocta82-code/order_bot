"""Разбор текста заказа: дата, время, позиции, количества.

Модуль не зависит от Telegram и внешних AI: это детерминированный fallback,
который работает всегда и покрывает типовые фразы поваров.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .units import canon_unit

# ---------------------------------------------------------------------------
# числительные
# ---------------------------------------------------------------------------

_UNIT_WORDS = {
    1: ("один", "одна", "одну"),
    2: ("два", "две"),
    3: ("три",),
    4: ("четыре",),
    5: ("пять",),
    6: ("шесть",),
    7: ("семь",),
    8: ("восемь",),
    9: ("девять",),
}

NUM_WORDS: dict[str, float] = {
    "полтора": 1.5, "полторы": 1.5, "пару": 2, "пара": 2, "пары": 2,
    "ноль": 0, "нуль": 0,
}
# базовые формы
for value, forms in _UNIT_WORDS.items():
    for form in forms:
        NUM_WORDS[form] = value
# десятки и составные числительные
for tens_word, tens_value in (
    ("двадцать", 20), ("тридцать", 30), ("сорок", 40), ("пятьдесят", 50),
    ("шестьдесят", 60), ("семьдесят", 70), ("восемьдесят", 80), ("девяносто", 90),
):
    NUM_WORDS[tens_word] = tens_value
    for value, forms in _UNIT_WORDS.items():
        for form in forms:
            NUM_WORDS[f"{tens_word} {form}"] = tens_value + value
# отдельные падежные формы, встречающиеся в речи
NUM_WORDS.update({
    "двух": 2, "трех": 3, "четырех": 4, "пяти": 5, "шести": 6,
    "семи": 7, "восьми": 8, "девяти": 9, "десяти": 10,
    "пятнадцати": 15, "двадцати": 20, "тридцати": 30,
})

_UNIT_ALIASES_FOR_REGEX = [
    "кг", "килограмм", "килограмма", "килограммов", "кило",
    "г", "гр", "грамм", "грамма", "граммов",
    "л", "литр", "литра", "литров",
    "мл", "миллилитр", "миллилитра",
    "шт", "штука", "штуки", "штук", "штуку",
    "упаковка", "упаковки", "упаковок", "упаковку", "упак", "уп",
    "пакет", "пакета", "пакетов", "пакету", "пакеты", "пакетик",
    "коробка", "коробки", "коробок", "коробку", "коробке",
    "ведро", "ведра", "ведер", "ведру",
    "мешок", "мешка", "мешков", "мешке",
    "канистра", "канистры", "канистр", "канистру",
    "бутылка", "бутылки", "бутылок", "бутылку",
    "банка", "банки", "банок", "банку",
    "пачка", "пачки", "пачек", "пачку",
    "лоток", "лотка", "лотков",
    "ящик", "ящика", "ящиков",
    "блок", "блока", "блоков",
    "паллета", "паллеты", "паллет",
]

_UNIT_ALT = "|".join(re.escape(w) for w in sorted(_UNIT_ALIASES_FOR_REGEX, key=len, reverse=True))
_NUM_ALT = "|".join(
    re.escape(w) for w in sorted(NUM_WORDS, key=len, reverse=True)
) + r"|\d+(?:[.,]\d+)?"

NUM_UNIT_RE = re.compile(
    rf"(?<![0-9a-zа-я])(?P<num>{_NUM_ALT})\s*(?P<unit>{_UNIT_ALT})(?![0-9a-zа-я])",
    re.IGNORECASE,
)
UNIT_ONLY_RE = re.compile(
    rf"(?<![0-9a-zа-я])(?P<unit>{_UNIT_ALT})(?![0-9a-zа-я])",
    re.IGNORECASE,
)
POLKILO_RE = re.compile(
    r"(?<![0-9a-zа-я])(?P<word>полкило|полкилограмма|поллитра|поллитровка|поллитровку)(?![0-9a-zа-я])",
    re.IGNORECASE,
)
TEN_RE = re.compile(r"(?<![0-9a-zа-я])(?P<word>десяток|дюжина|дюжину)(?![0-9a-zа-я])", re.IGNORECASE)

# ---------------------------------------------------------------------------
# даты и время
# ---------------------------------------------------------------------------

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

WEEKDAYS = {
    "понедельник": 0,
    "вторник": 1,
    "среда": 2, "среду": 2, "среде": 2,
    "четверг": 3,
    "пятница": 4, "пятницу": 4, "пятнице": 4,
    "суббота": 5, "субботу": 5, "субботе": 5,
    "воскресенье": 6, "воскресенья": 6,
}

TIME_WORDS = {
    "часу": 1, "двум": 2, "трем": 3, "четырем": 4, "пяти": 5, "шести": 6,
    "семи": 7, "восьми": 8, "девяти": 9, "десяти": 10,
    "одиннадцати": 11, "двенадцати": 12,
}

_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DAY_MONTH_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?(?!\d)",
    re.IGNORECASE,
)
_NUM_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?!\d)(?!\s*%)")
_WEEKDAY_RE = re.compile(
    r"(?<![0-9a-zа-я])(?:в|во|на|к|до)?\s*(" + "|".join(
        sorted(WEEKDAYS, key=len, reverse=True)
    ) + r")(?![0-9a-zа-я])",
    re.IGNORECASE,
)
_TIME_DIGIT_RE = re.compile(
    r"(?<![0-9a-zа-я])(?:к|на|до|в)\s+(\d{1,2})(?:[:.](\d{2}))?\s*"
    r"(?:час(?:а|ов|ам|ами)?|утра|дня|вечера|ночи)?"
    r"(?!\s*(?:%|кг|г|л|мл|шт|штук|коробк|пакет|ведр|мешк|канистр|бутылк|банк|пачк|упаков|ящик|лотк|блок))",
    re.IGNORECASE,
)
_TIME_WORD_RE = re.compile(
    r"(?<![0-9a-zа-я])к\s+(" + "|".join(TIME_WORDS) + r")(?![0-9a-zа-я])"
    r"(?!\s*(?:кг|г|л|мл|шт|штук|коробк|пакет|ведр|мешк|канистр|бутылк|банк|пачк|упаков|ящик|лотк|блок))",
    re.IGNORECASE,
)
_TIME_MEAL_RE = re.compile(r"(?<![0-9a-zа-я])(?:к|на)\s+(обеду|утру|вечеру)(?![0-9a-zа-я])", re.IGNORECASE)
_COMMENT_RE = re.compile(
    r"(?<![0-9a-zа-я])(?:комментарий|коммент|примечание|пожелание|просьба)\s*[:\-–—]?\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)

# ---------------------------------------------------------------------------
# структуры
# ---------------------------------------------------------------------------


@dataclass
class ParsedItem:
    raw: str
    qty: float | None = None
    unit: str | None = None
    catalog_id: int | None = None
    candidates: list[int] = field(default_factory=list)
    score: float = 0.0
    status: str = "new"
    note: str = ""


@dataclass
class ParsedOrder:
    items: list[ParsedItem]
    delivery_date: date
    delivery_time: str | None
    comment: str
    provider: str
    raw_text: str


@dataclass
class DeliveryInfo:
    day: date
    time: str | None
    comment: str
    text: str


# ---------------------------------------------------------------------------
# вспомогательное
# ---------------------------------------------------------------------------


def _blank(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


def _to_float(token: str) -> float | None:
    token = token.strip().lower().replace("ё", "е")
    if re.fullmatch(r"\d+(?:[.,]\d+)?", token):
        return float(token.replace(",", "."))
    value = NUM_WORDS.get(token)
    return float(value) if value is not None else None


def _format_time(hour: int, minute: int = 0) -> str | None:
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _next_weekday(today: date, target: int) -> date:
    delta = (target - today.weekday()) % 7
    return today + timedelta(days=delta)


def extract_delivery(text: str, today: date) -> DeliveryInfo:
    working = text
    day: date | None = None
    time_value: str | None = None
    comment = ""

    # дата: ISO
    m = _ISO_DATE_RE.search(working)
    if m:
        try:
            day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            working = _blank(working, m.start(), m.end())
        except ValueError:
            day = None

    # дата: 4 ноября [2026]
    if day is None:
        m = _DAY_MONTH_RE.search(working)
        if m:
            try:
                year = int(m.group(3)) if m.group(3) else today.year
                day = date(year, MONTHS[m.group(2).lower()], int(m.group(1)))
                if day < today and not m.group(3):
                    day = day.replace(year=day.year + 1)
                working = _blank(working, m.start(), m.end())
            except ValueError:
                day = None

    # дата: 04.11[.2026] (но не «к 10.30» — это время)
    if day is None:
        m = _NUM_DATE_RE.search(working)
        if m and not re.search(r"(?:^|\s)(?:к|на|до|в)\s*$", working[: m.start()], re.IGNORECASE):
            try:
                d, mo = int(m.group(1)), int(m.group(2))
                y = int(m.group(3)) if m.group(3) else today.year
                if y < 100:
                    y += 2000
                candidate = date(y, mo, d)
                if candidate < today and not m.group(3):
                    candidate = candidate.replace(year=candidate.year + 1)
                day = candidate
                working = _blank(working, m.start(), m.end())
            except ValueError:
                day = None

    # день недели
    if day is None:
        m = _WEEKDAY_RE.search(working)
        if m:
            word = m.group(1).lower()
            if word in WEEKDAYS:
                day = _next_weekday(today, WEEKDAYS[word])
                working = _blank(working, m.start(), m.end())

    # относительные даты
    if day is None:
        m = re.search(r"(?<![0-9a-zа-я])послезавтра(?![0-9a-zа-я])", working, re.IGNORECASE)
        if m:
            day = today + timedelta(days=2)
            working = _blank(working, m.start(), m.end())
        else:
            m = re.search(r"(?<![0-9a-zа-я])завтра(?![0-9a-zа-я])", working, re.IGNORECASE)
            if m:
                day = today + timedelta(days=1)
                working = _blank(working, m.start(), m.end())
            else:
                m = re.search(r"(?<![0-9a-zа-я])сегодня(?![0-9a-zа-я])", working, re.IGNORECASE)
                if m:
                    day = today
                    working = _blank(working, m.start(), m.end())

    # срочно = сегодня
    m = re.search(r"(?<![0-9a-zа-я])(?:срочно|как можно быстрее|поскорее)(?![0-9a-zа-я])", working, re.IGNORECASE)
    if m:
        if day is None:
            day = today
        working = _blank(working, m.start(), m.end())

    # время: к 10 / к 10:30
    m = _TIME_DIGIT_RE.search(working)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        value = _format_time(hour, minute)
        if value:
            time_value = value
            working = _blank(working, m.start(), m.end())

    # время: к десяти
    if time_value is None:
        m = _TIME_WORD_RE.search(working)
        if m:
            value = _format_time(TIME_WORDS[m.group(1).lower()])
            if value:
                time_value = value
                working = _blank(working, m.start(), m.end())

    # время: к обеду
    if time_value is None:
        m = _TIME_MEAL_RE.search(working)
        if m:
            meal = m.group(1).lower()
            time_value = {"обеду": "12:00", "утру": "09:00", "вечеру": "18:00"}[meal]
            comment = f"к {meal}"
            working = _blank(working, m.start(), m.end())

    # комментарий
    m = _COMMENT_RE.search(working)
    if m:
        comment = (comment + " " + m.group(1).strip()).strip() if comment else m.group(1).strip()
        working = _blank(working, m.start(), m.end())

    if day is None:
        day = today + timedelta(days=1)
    return DeliveryInfo(day=day, time=time_value, comment=comment, text=working)


# ---------------------------------------------------------------------------
# количества
# ---------------------------------------------------------------------------


@dataclass
class _Qty:
    start: int
    end: int
    qty: float
    unit: str | None


def _find_quantities(low: str) -> list[_Qty]:
    found: list[_Qty] = []
    for m in NUM_UNIT_RE.finditer(low):
        qty = _to_float(m.group("num"))
        unit = canon_unit(m.group("unit"))
        if qty is None or unit is None:
            continue
        found.append(_Qty(m.start(), m.end(), qty, unit))
    for m in POLKILO_RE.finditer(low):
        word = m.group("word")
        if word.startswith("полкило"):
            found.append(_Qty(m.start(), m.end(), 0.5, "кг"))
        else:
            found.append(_Qty(m.start(), m.end(), 0.5, "л"))
    for m in TEN_RE.finditer(low):
        qty = 10 if m.group("word").lower().startswith("десят") else 12
        found.append(_Qty(m.start(), m.end(), float(qty), "шт"))

    # «одиночные» единицы: «коробку» = 1 коробка
    for m in UNIT_ONLY_RE.finditer(low):
        if any(q.start < m.end() and m.start() < q.end for q in found):
            continue
        unit = canon_unit(m.group("unit"))
        if unit:
            found.append(_Qty(m.start(), m.end(), 1.0, unit))

    found.sort(key=lambda q: (q.start, -(q.end - q.start)))
    result: list[_Qty] = []
    last_end = -1
    for q in found:
        if q.start >= last_end:
            result.append(q)
            last_end = q.end
    return result


_STOPWORDS = {
    "пожалуйста", "нужно", "надо", "нужен", "нужна", "нужны", "нужное",
    "дай", "дайте", "привези", "привезите", "привезти", "закажи", "закажите",
    "заказ", "заказать", "мне", "нам", "еще", "ещё", "также", "срочно",
    "сегодня", "завтра", "послезавтра", "утром", "днем", "вечером",
    "на", "до", "к", "в", "во", "по", "за", "с", "со", "и", "а", "но",
    "плюс", "итого", "всего", "будет", "примите", "принять",
    "сделай", "сделайте", "доставка", "доставить", "около",
    "где", "то", "это", "все", "всё",
}
_WORD_RE = re.compile(r"[0-9a-zа-я%]+", re.IGNORECASE)


def clean_product(text: str) -> str:
    words = _WORD_RE.findall(text.lower().replace("ё", "е"))
    return " ".join(w for w in words if w not in _STOPWORDS)


def split_segments(text: str) -> list[str]:
    normalized = re.sub(r"[;!?\n]+", ",", text)
    parts = re.split(r",(?!\d)|\s+и\s+|\s+плюс\s+|\s*\+\s*|\s+а также\s+", normalized, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def _segment_items(segment: str) -> list[ParsedItem]:
    low = segment.lower().replace("ё", "е")
    quantities = _find_quantities(low)
    if not quantities:
        product = clean_product(segment)
        if not product:
            return []
        return [ParsedItem(raw=product, qty=None, unit=None)]

    items: list[ParsedItem] = []
    prev_end = 0
    consumed_after = False
    for index, q in enumerate(quantities):
        before = segment[prev_end:q.start]
        if index > 0 and consumed_after:
            before = ""
        product = clean_product(before)
        if product:
            consumed_after = False
        else:
            next_start = quantities[index + 1].start if index + 1 < len(quantities) else len(segment)
            product = clean_product(segment[q.end:next_start])
            consumed_after = True
        raw = product or segment.strip()
        items.append(ParsedItem(raw=raw, qty=q.qty, unit=q.unit))
        prev_end = q.end
    return items


def parse_number(text: str) -> float | None:
    """Вытаскивает одно число из короткого ответа («7», «семь», «1,5 кг»)."""
    low = text.lower().replace("ё", "е")
    match = re.search(r"\d+(?:[.,]\d+)?", low)
    if match:
        return float(match.group(0).replace(",", "."))
    for word in sorted(NUM_WORDS, key=len, reverse=True):
        if re.search(rf"(?<![а-яa-z]){re.escape(word)}(?![а-яa-z])", low):
            return float(NUM_WORDS[word])
    return None


_STATUS_INTENT_RE = re.compile(
    r"(где\s+(мой\s+|наш\s+)?заказ|когда\s+(привезут|доставят|будет)|"
    r"статус\s+заказа|что\s+с\s+заказом)"
)
_CANCEL_INTENT_RE = re.compile(
    r"(отмените|отмени|отмена|отменить\s+заказ|уберите\s+заказ)"
)
_PRICE_INTENT_RE = re.compile(
    r"(?:(?:покажи|скинь|пришли|дай)\s+)?"
    r"(прайс|прайс-лист|цены|что\s+почем|список\s+товаров|ассортимент)"
)
_HELP_INTENT_RE = re.compile(
    r"(помощь|помоги|помогите|как\s+заказать|что\s+ты\s+умеешь)"
)
_THANKS_INTENT_RE = re.compile(r"(спасибо(\s+большое)?|благодарю|спс)")
_GREETING_INTENT_RE = re.compile(
    r"(привет|приветик|здравствуйте|здравствуй|добрый\s+день|"
    r"добрый\s+вечер|доброе\s+утро|hi|hello)"
)


def detect_intent(text: str) -> str | None:
    """Распознаёт не-заказ без AI: приветствие, статус, отмену, прайс, помощь."""
    low = " ".join(str(text or "").lower().replace("ё", "е").split())
    low = low.strip(" .,!?;:—–-")
    if not low:
        return None
    if _STATUS_INTENT_RE.search(low):
        return "status"
    if _CANCEL_INTENT_RE.fullmatch(low):
        return "cancel"
    if _PRICE_INTENT_RE.fullmatch(low):
        return "price"
    if _HELP_INTENT_RE.fullmatch(low):
        return "help"
    if _THANKS_INTENT_RE.fullmatch(low):
        return "thanks"
    if _GREETING_INTENT_RE.fullmatch(low):
        return "greeting"
    return None


def parse_local(
    text: str,
    tz: ZoneInfo | str = "Asia/Tashkent",
    now: datetime | None = None,
) -> ParsedOrder:
    """Детерминированный разбор заказа без AI."""
    if isinstance(tz, str):
        tz = ZoneInfo(tz)
    current = now.astimezone(tz) if now else datetime.now(tz)
    delivery = extract_delivery(text, current.date())
    items: list[ParsedItem] = []
    for segment in split_segments(delivery.text):
        items.extend(_segment_items(segment))
    # пустые/мусорные позиции отбрасываем
    items = [i for i in items if i.raw and re.search(r"[а-яa-z]", i.raw, re.IGNORECASE)]
    return ParsedOrder(
        items=items,
        delivery_date=delivery.day,
        delivery_time=delivery.time,
        comment=delivery.comment,
        provider="local",
        raw_text=text,
    )
