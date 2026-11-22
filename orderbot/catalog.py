"""Прайс-лист: загрузка catalog.xlsx, нормализация и нечёткий поиск."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook
from rapidfuzz import fuzz

try:  # pymorphy3 ставит леммы, без него работает упрощённый стеммер
    import pymorphy3
except Exception:  # pragma: no cover
    pymorphy3 = None


_WORD_RE = re.compile(r"[0-9a-zа-я%]+", re.IGNORECASE)
_PERCENT_RE = re.compile(r"(\d)\s*%")
_NON_WORD_RE = re.compile(r"[^0-9a-zа-я%]+")

_MORPH = pymorphy3.MorphAnalyzer() if pymorphy3 is not None else None

_STEM_SUFFIXES = (
    "ами", "ями", "ов", "ев", "ах", "ях", "ам", "ям", "ой", "ей",
    "ом", "ем", "ы", "и", "а", "я", "у", "ю", "е", "о", "ь",
)


def normalize(text: str) -> str:
    """Нижний регистр, ё→е, пунктуация → пробел, проценты остаются."""
    text = str(text or "").lower().replace("ё", "е")
    text = _PERCENT_RE.sub(r"\1%", text)
    text = _NON_WORD_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def stem(word: str) -> str:
    for suffix in _STEM_SUFFIXES:
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def lemmatize(normalized: str) -> str:
    result: list[str] = []
    for token in normalized.split():
        if any(ch.isdigit() for ch in token):
            result.append(token)
            continue
        if _MORPH is not None:
            try:
                result.append(_MORPH.parse(token)[0].normal_form)
                continue
            except Exception:
                pass
        result.append(stem(token))
    return " ".join(result)


def _num(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("\xa0", " ").replace(" ", "").replace("₽", "").replace("$", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class CatalogItem:
    id: int
    name: str
    synonyms: tuple[str, ...]
    unit: str
    pack: str
    price: float
    category: str = ""
    stock: float | None = None
    purchase: float | None = None
    supplier: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.name} ({self.pack})" if self.pack else self.name


@dataclass(frozen=True)
class Match:
    item: CatalogItem
    score: float
    phrase: str


@dataclass
class Catalog:
    items: list[CatalogItem] = field(default_factory=list)
    by_id: dict[int, CatalogItem] = field(default_factory=dict)
    _phrases: list[tuple[CatalogItem, str, str]] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path, sheet: str | None = None) -> Catalog:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Прайс не найден: {path}")
        wb = load_workbook(path, data_only=True, read_only=True)
        ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb.worksheets[0]
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
        if not rows:
            raise ValueError(f"Пустой прайс: {path}")

        header = [normalize(cell or "") for cell in rows[0]]
        idx: dict[str, int] = {}
        for pos, title in enumerate(header):
            if title in ("id", "код", "артикул") and "id" not in idx:
                idx["id"] = pos
            elif title.startswith("назван") or title in ("наименование", "name", "товар"):
                idx["name"] = pos
            elif title.startswith("синоним") or title in ("synonyms", "alias"):
                idx["synonyms"] = pos
            elif title.startswith("единиц") or title in ("ед", "unit", "ед.изм"):
                idx["unit"] = pos
            elif title.startswith("фасов") or title in ("pack", "упаковка"):
                idx["pack"] = pos
            elif title.startswith("цена") or title in ("price", "стоимость"):
                idx["price"] = pos
            elif title.startswith("категор"):
                idx["category"] = pos
            elif title.startswith("остат") or title in ("stock", "кол-во"):
                idx["stock"] = pos
            elif title.startswith("закуп") or title in ("purchase", "cost"):
                idx["purchase"] = pos
            elif title.startswith("постав") or title in ("supplier", "vendor"):
                idx["supplier"] = pos

        if "name" not in idx:
            raise ValueError("В прайсе нет колонки «Название»")
        if "price" not in idx:
            raise ValueError("В прайсе нет колонки «Цена»")

        catalog = cls()
        auto_id = 1000
        for row in rows[1:]:
            if row is None:
                continue
            name = str(row[idx["name"]] or "").strip()
            if not name:
                continue
            raw_id = row[idx["id"]] if "id" in idx else None
            try:
                item_id = int(raw_id) if raw_id not in (None, "") else auto_id
            except (TypeError, ValueError):
                item_id = auto_id
            auto_id = max(auto_id, item_id) + 1

            synonyms_raw = str(row[idx["synonyms"]] or "") if "synonyms" in idx else ""
            synonyms = [s.strip() for s in re.split(r"[;\n]+", synonyms_raw) if s.strip()]
            unit = str(row[idx["unit"]] or "шт").strip() if "unit" in idx else "шт"
            pack = str(row[idx["pack"]] or "").strip() if "pack" in idx else ""
            price = _num(row[idx["price"]]) or 0.0
            category = str(row[idx["category"]] or "").strip() if "category" in idx else ""
            stock = _num(row[idx["stock"]]) if "stock" in idx else None
            purchase = _num(row[idx["purchase"]]) if "purchase" in idx else None
            supplier = (
                str(row[idx["supplier"]] or "").strip() if "supplier" in idx else ""
            )

            item = CatalogItem(
                id=item_id,
                name=name,
                synonyms=tuple(synonyms),
                unit=unit,
                pack=pack,
                price=price,
                category=category,
                stock=stock,
                purchase=purchase,
                supplier=supplier,
            )
            catalog.add(item)
        if not catalog.items:
            raise ValueError("В прайсе не найдено ни одной позиции")
        return catalog

    def add(self, item: CatalogItem) -> None:
        self.items.append(item)
        self.by_id[item.id] = item
        phrases = [item.name, *item.synonyms]
        seen: set[str] = set()
        for phrase in phrases:
            norm = normalize(phrase)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            self._phrases.append((item, norm, lemmatize(norm)))

    def dump_json(self, path: str | Path, exclude: set[int] | None = None) -> int:
        """Пишет прайс в JSON: мини-апп читает его вместо Excel (G4).

        Возвращает число позиций. Запись атомарная: сначала .tmp, потом rename,
        чтобы каталог не увидел половину файла.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated": datetime.now().isoformat(timespec="seconds"),
            "items": [
                {
                    "id": item.id,
                    "name": item.name,
                    "unit": item.unit,
                    "pack": item.pack,
                    "price": round(float(item.price or 0), 2),
                    "category": item.category or "",
                    "stock": item.stock,
                    "supplier": item.supplier,
                }
                for item in self.items
                if not exclude or item.id not in exclude
            ],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(path)
        return len(self.items)

    def get(self, item_id: int | None) -> CatalogItem | None:
        if item_id is None:
            return None
        return self.by_id.get(int(item_id))

    def match(self, query: str, limit: int = 4, min_score: float = 40.0) -> list[Match]:
        norm = normalize(query)
        if not norm:
            return []
        lemma = lemmatize(norm)
        best: dict[int, Match] = {}
        for item, phrase_norm, phrase_lemma in self._phrases:
            if lemma == phrase_lemma:
                score = 100.0
            else:
                score = max(
                    fuzz.token_set_ratio(lemma, phrase_lemma),
                    fuzz.token_set_ratio(norm, phrase_norm) * 0.99,
                )
                # Общий синоним («сыр», «масло») — это подмножество слов
                # запроса «сыр пармезан», поэтому token_set_ratio даёт ему
                # 100 и он встаёт вровень с точным названием. Для такого
                # случая считаем по порядку слов: короткий синоним получает
                # заметно меньше и не мешает выбрать полную фразу.
                phrase_tokens = {
                    token for token in phrase_lemma.split()
                    if not any(ch.isdigit() for ch in token)
                }
                query_tokens = {
                    token for token in lemma.split()
                    if not any(ch.isdigit() for ch in token)
                }
                if phrase_tokens and phrase_tokens < query_tokens:
                    score = min(score, fuzz.token_sort_ratio(lemma, phrase_lemma))
            current = best.get(item.id)
            if current is None or score > current.score:
                best[item.id] = Match(item=item, score=round(float(score), 1), phrase=phrase_norm)
        result = [m for m in best.values() if m.score >= min_score]
        result.sort(key=lambda m: (-m.score, m.item.id))
        return result[:limit]

    def substitutes(
        self, item: CatalogItem, limit: int = 3
    ) -> list[CatalogItem]:
        """Похожие позиции той же категории — на замену."""
        if not item.category:
            return []
        price = float(item.price or 0)
        pool = [
            other
            for other in self.items
            if other.id != item.id
            and other.category == item.category
            and (other.stock is None or float(other.stock) > 0)
        ]
        pool.sort(key=lambda other: abs(float(other.price or 0) - price))
        return pool[:limit]

    def search(self, query: str) -> CatalogItem | None:
        matches = self.match(query, limit=1, min_score=0)
        return matches[0].item if matches else None


def diff_catalogs(old: Catalog, new: Catalog) -> dict:
    """Сравнивает два прайса: цены, новые и убранные позиции."""
    price_changes: list[tuple[CatalogItem, CatalogItem]] = []
    added: list[CatalogItem] = []
    removed: list[CatalogItem] = []
    for item_id, item in new.by_id.items():
        previous = old.by_id.get(item_id)
        if previous is None:
            added.append(item)
        elif abs(float(previous.price) - float(item.price)) > 0.005:
            price_changes.append((previous, item))
    for item_id, item in old.by_id.items():
        if item_id not in new.by_id:
            removed.append(item)
    return {
        "old_count": len(old.items),
        "new_count": len(new.items),
        "price_changes": price_changes,
        "added": added,
        "removed": removed,
    }


# --- опасные пары: жирность, сорт, свежее/мороженое ------------------------
_FAT_RE = re.compile(r"(\d{1,3})\s*%")
_STATE_WORDS = ("морожен", "свеж", "охлажд", "заморож", "обезжир", "цельн",
                "сорт", "калибр", "крупн", "мелк")


def risk_signature(text: str) -> set[str]:
    """Свойства товара, на которых нельзя ошибаться молча."""
    low = (text or "").lower().replace("ё", "е")
    signature = {f"fat:{m}" for m in _FAT_RE.findall(low)}
    for word in _STATE_WORDS:
        if word in low:
            signature.add(word)
    return signature


def conflicting(query: str, name: str) -> bool:
    """True, если запрос и товар расходятся по жирности/сорту/состоянию."""
    left, right = risk_signature(query), risk_signature(name)
    fats_left = {x for x in left if x.startswith("fat:")}
    fats_right = {x for x in right if x.startswith("fat:")}
    if fats_left and fats_right and fats_left != fats_right:
        return True
    words_left = {x for x in left if not x.startswith("fat:")}
    words_right = {x for x in right if not x.startswith("fat:")}
    if words_left and words_right and words_left != words_right:
        return True
    return False
