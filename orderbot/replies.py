"""C9: ответ менеджера клиенту прямо из бота.

Клиент жмёт «☎️ Менеджер» и пишет текст — бот пересылает его менеджеру вместе
с id клиента. Ответить можно так:

- `/reply 12345 Ваш заказ уже собирают`
- или ответом (реплаем) на пересланное сообщение: тогда id берётся из текста.

Здесь только разбор и тексты, чтобы покрывать это тестами.
"""
from __future__ import annotations

import re

ID_RE = re.compile(r"\(id\s+(\d{3,})\)")
MAX_REPLY = 3500


def parse_reply(args: str) -> tuple[int, str] | None:
    """Разбирает «12345 текст ответа» → (id клиента, текст)."""
    raw = str(args or "").strip()
    if not raw:
        return None
    parts = raw.split(maxsplit=1)
    if len(parts) < 2 or not parts[0].lstrip("-").isdigit():
        return None
    client_id = int(parts[0])
    text = parts[1].strip()
    if client_id <= 0 or not text:
        return None
    return client_id, text[:MAX_REPLY]


def extract_client_id(text: str | None) -> int | None:
    """Достаёт id клиента из пересланного сообщения «… (id 12345):»."""
    match = ID_RE.search(str(text or ""))
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    return value if value > 0 else None


def forward_note() -> str:
    return "Ответить: /reply ID текст — или ответьте реплаем на это сообщение."


def reply_for_client(text: str, manager_name: str = "") -> str:
    """Текст, который увидит клиент."""
    who = f" ({manager_name})" if manager_name else ""
    return f"💬 Менеджер{who}: {text}"
