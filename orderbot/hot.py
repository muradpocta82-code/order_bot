"""B14: «горячее предложение» на скоропорт.

Менеджер пишет `/hot помидоры 1.10 до 18:00` — бот рассылает короткое
предложение тем, кто покупал эту позицию, с кнопкой «Взять». Клиент жмёт
кнопку, менеджер получает интерес (кто и что), а количество клиент пишет
обычным сообщением — оно уходит в обычный поток заказа.

Здесь разбор команды и тексты; без Telegram.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_PRICE = 100000.0
TIME_UNTIL_RE = re.compile(r"до\s*(\d{1,2})[:.](\d{2})", re.IGNORECASE)
TIME_BARE_RE = re.compile(r"(?:^|\s)(\d{1,2}):(\d{2})\b")


@dataclass(frozen=True)
class Offer:
    phrase: str
    price: float
    until: str = ""

    @property
    def until_text(self) -> str:
        return f" до {self.until}" if self.until else ""


def parse_offer(args: str) -> Offer | None:
    """Разбирает «помидоры 1.10 до 18:00»."""
    raw = " ".join(str(args or "").split())
    if not raw:
        return None
    until = ""
    match = TIME_UNTIL_RE.search(raw) or TIME_BARE_RE.search(raw)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        until = f"{hour:02d}:{minute:02d}"
        raw = (raw[: match.start()] + raw[match.end():]).strip(" ,.;-")
    parts = raw.split()
    if len(parts) < 2:
        return None
    raw_price = parts[-1].replace(",", ".").replace("$", "")
    try:
        price = round(float(raw_price), 2)
    except ValueError:
        return None
    if not (0 < price <= MAX_PRICE):
        return None
    phrase = " ".join(parts[:-1]).strip(" ,.;-")
    if len(phrase) < 2:
        return None
    return Offer(phrase=phrase, price=price, until=until)


def offer_text(name: str, price: float, unit: str, until: str = "", pack: str = "") -> str:
    pack_part = f" ({pack})" if pack else ""
    return (
        f"🔥 Горячее предложение: {name}{pack_part} — ${price:.2f}/{unit}"
        f"{f' до {until}' if until else ''}.\n"
        "Если нужно — жмите «Взять», напишите количество, и я оформлю заказ."
    )


def taken_text(name: str) -> str:
    return (
        f"Отлично, {name} за вами. Напишите количество — например «5 кг» — "
        "и я передам заказ менеджеру."
    )


def manager_note(client_name: str, name: str, price: float, until: str = "") -> str:
    return (
        f"🔥 {client_name} нажал «Взять» на {name} по ${price:.2f}"
        f"{f' (до {until})' if until else ''}. Ждём количество."
    )


def sample_text() -> str:
    return (
        "Формат: <code>/hot помидоры 1.10 до 18:00</code>\n"
        "Рассылка уйдёт тем, кто покупал эту позицию за последние 90 дней; "
        "отказавшиеся от рассылок пропускаются."
    )
