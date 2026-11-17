"""Единицы измерения: нормализация, склонение, конвертация."""
from __future__ import annotations

import re

# каноническая единица -> (1, 2-4, 5+)
PLURALS: dict[str, tuple[str, str, str]] = {
    "кг": ("кг", "кг", "кг"),
    "г": ("г", "г", "г"),
    "л": ("л", "л", "л"),
    "мл": ("мл", "мл", "мл"),
    "шт": ("шт", "шт", "шт"),
    "упаковка": ("упаковка", "упаковки", "упаковок"),
    "пакет": ("пакет", "пакета", "пакетов"),
    "коробка": ("коробка", "коробки", "коробок"),
    "ведро": ("ведро", "ведра", "ведер"),
    "мешок": ("мешок", "мешка", "мешков"),
    "канистра": ("канистра", "канистры", "канистр"),
    "бутылка": ("бутылка", "бутылки", "бутылок"),
    "банка": ("банка", "банки", "банок"),
    "пачка": ("пачка", "пачки", "пачек"),
    "лоток": ("лоток", "лотка", "лотков"),
    "ящик": ("ящик", "ящика", "ящиков"),
    "блок": ("блок", "блока", "блоков"),
    "паллета": ("паллета", "паллеты", "паллет"),
}

UNIT_KIND: dict[str, str] = {
    "кг": "mass",
    "г": "mass",
    "л": "volume",
    "мл": "volume",
}

PIECE_UNITS = {
    "шт", "упаковка", "пакет", "коробка", "ведро", "мешок",
    "канистра", "бутылка", "банка", "пачка", "лоток", "ящик", "блок", "паллета",
}

# все словоформы -> каноническая единица
_ALIASES: dict[str, list[str]] = {
    "кг": ["кг", "килограмм", "килограмма", "килограммов", "кило", "кг."],
    "г": ["г", "гр", "грамм", "грамма", "граммов"],
    "л": ["л", "литр", "литра", "литров", "литру", "литрах"],
    "мл": ["мл", "миллилитр", "миллилитра", "миллилитров"],
    "шт": ["шт", "штука", "штуки", "штук", "штуку", "штуке", "штукой", "штукам", "штуках"],
    "упаковка": ["упаковка", "упаковки", "упаковок", "упаковку", "упаковке", "упаковкой", "упак", "уп", "упаковкам"],
    "пакет": ["пакет", "пакета", "пакетов", "пакету", "пакете", "пакетом", "пакетик", "пакетика", "пакетиков", "пакеты"],
    "коробка": ["коробка", "коробки", "коробок", "коробку", "коробке", "коробкой", "коробкам", "коробками"],
    "ведро": ["ведро", "ведра", "ведер", "ведру", "ведре", "ведром", "ведрам"],
    "мешок": ["мешок", "мешка", "мешков", "мешку", "мешке", "мешком", "мешкам"],
    "канистра": ["канистра", "канистры", "канистр", "канистру", "канистре", "канистрой"],
    "бутылка": ["бутылка", "бутылки", "бутылок", "бутылку", "бутылке", "бутылкой", "бутылкам"],
    "банка": ["банка", "банки", "банок", "банку", "банке", "банкой", "банкам"],
    "пачка": ["пачка", "пачки", "пачек", "пачку", "пачке", "пачкой", "пачкам"],
    "лоток": ["лоток", "лотка", "лотков", "лотку", "лотке", "лотком"],
    "ящик": ["ящик", "ящика", "ящиков", "ящику", "ящике", "ящиком"],
    "блок": ["блок", "блока", "блоков", "блоку", "блоке", "блоком"],
    "паллета": ["паллета", "паллеты", "паллет", "паллету", "паллете", "паллетой"],
}

UNIT_ALIASES: dict[str, str] = {}
for _canon, _forms in _ALIASES.items():
    for _form in _forms:
        UNIT_ALIASES[_form] = _canon

# для регулярных выражений в NLU
UNIT_WORDS: list[str] = sorted(UNIT_ALIASES, key=len, reverse=True)


def canon_unit(raw: str | None) -> str | None:
    if not raw:
        return None
    word = raw.strip().lower().replace("ё", "е").rstrip(".")
    if word in UNIT_ALIASES:
        return UNIT_ALIASES[word]
    for alias, canon in UNIT_ALIASES.items():
        if alias.startswith(word) and len(word) >= 2:
            return canon
    return None


def unit_kind(unit: str | None) -> str | None:
    canon = canon_unit(unit)
    if canon is None:
        return None
    return UNIT_KIND.get(canon, "piece")


def plural(unit: str | None, qty: float | None) -> str:
    canon = canon_unit(unit) or (unit or "шт")
    forms = PLURALS.get(canon, (canon, canon, canon))
    if qty is None:
        return forms[0]
    n = abs(qty)
    if float(n).is_integer():
        n = int(n)
        if n % 100 in (11, 12, 13, 14):
            return forms[2]
        if n % 10 == 1:
            return forms[0]
        if n % 10 in (2, 3, 4):
            return forms[1]
        return forms[2]
    return forms[1] if n < 1 else forms[0]


def format_qty(qty: float | None) -> str:
    if qty is None:
        return "?"
    q = float(qty)
    if q.is_integer():
        return str(int(q))
    text = f"{q:.3f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def convert_qty(qty: float | None, from_unit: str | None, to_unit: str | None) -> float | None:
    """Переводит количество в единицы прайса. None — единицы несовместимы."""
    if qty is None:
        return None
    src = canon_unit(from_unit)
    dst = canon_unit(to_unit)
    if src is None or dst is None:
        return float(qty)
    if src == dst:
        return float(qty)
    src_kind = UNIT_KIND.get(src, "piece")
    dst_kind = UNIT_KIND.get(dst, "piece")

    # «упаковка» — универсальная фасовка: считаем её штукой прайса
    if src == "упаковка" and dst_kind == "piece":
        return float(qty)
    if dst == "упаковка" and src_kind == "piece":
        return float(qty)

    # штуки взаимозаменяемы с любой поштучной фасовкой (ведро, пакет, коробка)
    if src == "шт" and dst_kind == "piece":
        return float(qty)
    if dst == "шт" and src_kind == "piece":
        return float(qty)

    if src_kind == "mass" and dst_kind == "mass":
        grams = float(qty) * (1000 if src == "кг" else 1)
        return grams / (1000 if dst == "кг" else 1)
    if src_kind == "volume" and dst_kind == "volume":
        ml = float(qty) * (1000 if src == "л" else 1)
        return ml / (1000 if dst == "л" else 1)
    return None


def pack_size(pack: str | None, catalog_unit: str | None) -> float | None:
    """Размер фасовки: «ведро 5 кг» → 5 (кг)."""
    if not pack:
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*([а-я]+)", str(pack).lower().replace("ё", "е"))
    if not match:
        return None
    try:
        size = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    if canon_unit(match.group(2)) != canon_unit(catalog_unit):
        return None
    return size if size > 0 else None


def price_per_unit(price: float | None, pack: str | None, catalog_unit: str | None) -> float | None:
    """Цена за килограмм/литр, если известна фасовка."""
    size = pack_size(pack, catalog_unit)
    if size is None or price is None:
        return None
    return float(price) / size


def convert_retail_pack(
    qty: float | None, from_unit: str | None, pack: str, catalog_unit: str
) -> float | None:
    """5 бутылок молока при фасовке «1 л» → 5 л (только розничная тара)."""
    if qty is None:
        return None
    src = canon_unit(from_unit)
    if src not in {"бутылка", "банка", "пачка"}:
        return None
    if unit_kind(catalog_unit) not in {"mass", "volume"}:
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*([а-я]+)", (pack or "").lower().replace("ё", "е"))
    if not match:
        return None
    if canon_unit(match.group(2)) != canon_unit(catalog_unit):
        return None
    try:
        pack_size = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    return round(float(qty) * pack_size, 3)

