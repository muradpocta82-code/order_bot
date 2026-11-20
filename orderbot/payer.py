"""C6: роль «плательщик» — бухгалтер точки получает деньги и документы.

Плательщик — второй Telegram-аккаунт на точку: он получает накладные
и напоминания о долге, но заказы делает повар. Связь хранится в таблице
`venue_contacts(user_id, venue_id, role)`.

Здесь только разбор команды и тексты — без Telegram.
"""
from __future__ import annotations

import re

ID_RE = re.compile(r"\d{3,}")


def parse_pair(args: str) -> tuple[int, int] | None:
    """Разбирает «12345 98765» → (id клиента, id плательщика)."""
    ids = ID_RE.findall(str(args or ""))
    if len(ids) != 2:
        return None
    client_id, payer_id = int(ids[0]), int(ids[1])
    if client_id <= 0 or payer_id <= 0 or client_id == payer_id:
        return None
    return client_id, payer_id


def parse_single(args: str) -> int | None:
    """Один id из аргументов (для /payer_del)."""
    ids = ID_RE.findall(str(args or ""))
    if len(ids) != 1:
        return None
    value = int(ids[0])
    return value if value > 0 else None


def connected_text(client_name: str) -> str:
    return (
        f"Вы подключены как плательщик по точке «{client_name}».\n"
        "Будете получать накладные и напоминания об оплате. Заказы делает повар."
    )


def debt_text(client_name: str, debt: float, count: int, age: int) -> str:
    return (
        f"Оплата по точке «{client_name}»: ${debt:.2f} за {count} заказ(ов), "
        f"самый ранний — {age} дней назад.\n"
        "Если оплата уже отправлена — напишите менеджеру, отметим."
    )


def removed_text() -> str:
    return "Роль плательщика с вас снята: накладные и напоминания больше не приходят."
