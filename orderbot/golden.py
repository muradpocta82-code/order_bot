"""Золотой набор из боевых заказов (E2).

Кнопка/команда менеджера «запомнить как пример» сохраняет пару
«текст повара → правильные позиции» в tests/orders_golden_live.jsonl.
Телефоны, адреса и имена вычищаются: набор идёт в тесты, а не в переписку.
Перед правкой промпта/парсера: venv/bin/python scripts/eval_parse.py --local-only
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
LIVE_PATH = BASE_DIR / "tests" / "orders_golden_live.jsonl"

PHONE_RE = re.compile(r"(\+?\d[\d\s\-()]{6,}\d)")
ADDRESS_RE = re.compile(r"(адрес|улица|ул\.|дом|кв\.|ориентир)[^,\n]*", re.IGNORECASE)


def clean_phrase(text: str) -> str:
    """Убирает телефоны и адреса из текста заказа."""
    cleaned = PHONE_RE.sub(" ", text or "")
    cleaned = ADDRESS_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:500]


def items_payload(items: list[dict]) -> list[dict]:
    """Только распознанные позиции: id прайса, количество, единица."""
    result: list[dict] = []
    for item in items or []:
        try:
            catalog_id = int(item.get("catalog_id") or 0)
        except (TypeError, ValueError):
            continue
        if not catalog_id:
            continue
        try:
            qty = float(item.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        result.append(
            {
                "catalog_id": catalog_id,
                "qty": qty,
                "unit": str(item.get("unit") or ""),
            }
        )
    return result


def next_id(path: Path = LIVE_PATH) -> int:
    if not path.exists():
        return 1000
    last = 999
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            last = max(last, int(data.get("id") or 0))
    except (OSError, json.JSONDecodeError, TypeError):
        return 1000
    return last + 1


def remember_example(
    order: dict, path: Path = LIVE_PATH, note: str = ""
) -> dict | None:
    """Дописывает пример из заказа. None — если из заказа нечего взять."""
    phrase = clean_phrase(str(order.get("raw_text") or ""))
    items = items_payload(order.get("items") or [])
    if len(phrase) < 5 or not items:
        return None
    case = {
        "id": next_id(path),
        "phrase": phrase,
        "delivery_date": str(order.get("delivery_date") or ""),
        "items": items,
        "source": "live",
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    if note:
        case["note"] = note[:200]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(case, ensure_ascii=False) + "\n")
    log.info("В золотой набор из боевых добавлен заказ №%s", order.get("id"))
    return case


def count(path: Path = LIVE_PATH) -> int:
    if not path.exists():
        return 0
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0
