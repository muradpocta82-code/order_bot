"""F2: выгрузка заказов в Google Sheets.

Работает, когда владелец завёл сервисный аккаунт Google и положил его JSON
на сервер: в `.env` заданы `GOOGLE_SA_FILE` (путь к JSON) и `SHEETS_ID`
(идентификатор таблицы из ссылки). Пока их нет — модуль молчит, бот работает
как раньше, а `doctor.py` напоминает, чего не хватает.

Лишних зависимостей не нужно: `google-auth` уже стоит (пришёл с Gemini),
токен подписывается штатным сервисным аккаунтом, а запросы идут по REST
через `aiohttp`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import aiohttp

from .config import Settings

SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
API = "https://sheets.googleapis.com/v4/spreadsheets"
TIMEOUT = 20

log = logging.getLogger(__name__)

_tokens: dict[str, tuple[str, float]] = {}


def _env_value(name: str) -> str:
    """Значение из .env — чтобы настройка подхватывалась без перезапуска."""
    try:
        from .config import BASE_DIR

        for line in (BASE_DIR / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def credentials_path(settings: Settings) -> Path | None:
    raw = str(getattr(settings, "google_sa_file", "") or "").strip() or _env_value(
        "GOOGLE_SA_FILE"
    )
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_file() else None


def sheet_id(settings: Settings) -> str:
    return (
        str(getattr(settings, "sheets_id", "") or "").strip() or _env_value("SHEETS_ID")
    )


def ready(settings: Settings) -> bool:
    """Включена ли выгрузка: есть и ключ сервисного аккаунта, и таблица."""
    return credentials_path(settings) is not None and bool(sheet_id(settings))


def _token_sync(path: Path) -> tuple[str, float]:
    """Токен сервисного аккаунта (синхронно, зовём в отдельном потоке)."""
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_file(
        str(path), scopes=list(SCOPES)
    )
    credentials.refresh(Request())
    expires = credentials.expiry.timestamp() if credentials.expiry else 0.0
    return str(credentials.token), expires


async def token(settings: Settings) -> str | None:
    """Токен с кэшем до истечения срока."""
    path = credentials_path(settings)
    if path is None:
        return None
    cached = _tokens.get(str(path))
    if cached and cached[1] - 60 > time.time():
        return cached[0]
    try:
        value, expires = await asyncio.to_thread(_token_sync, path)
    except Exception as exc:  # noqa: BLE001 — выгрузка не должна ломать заказ
        log.warning("Google Sheets: не получил токен (%s)", exc)
        return None
    _tokens[str(path)] = (value, expires)
    return value


async def append_rows(
    settings: Settings,
    rows: list[list[Any]],
    sheet: str = "Заказы",
) -> bool:
    """Дописывает строки в конец листа. False — если выгрузка недоступна."""
    if not rows or not ready(settings):
        return False
    access_token = await token(settings)
    if not access_token:
        return False
    range_name = f"{sheet}!A1"
    url = f"{API}/{sheet_id(settings)}/values/{range_name}:append"
    params = {"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"}
    payload = {"values": rows}
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        timeout = aiohttp.ClientTimeout(total=TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url, params=params, json=payload, headers=headers
            ) as response:
                if response.status >= 400:
                    text = await response.text()
                    log.warning("Google Sheets: отказ %s %s", response.status, text[:200])
                    return False
    except Exception as exc:  # noqa: BLE001
        log.warning("Google Sheets: не удалось дописать строки (%s)", exc)
        return False
    log.info("Google Sheets: дописано строк — %s", len(rows))
    return True


async def check(settings: Settings) -> tuple[bool | None, str]:
    """Проверка для /sheets и doctor: (работает ли, текст статуса)."""
    path = credentials_path(settings)
    if path is None:
        return None, "ключ сервисного аккаунта не найден (GOOGLE_SA_FILE)"
    if not sheet_id(settings):
        return None, "не задан SHEETS_ID (идентификатор таблицы)"
    access_token = await token(settings)
    if not access_token:
        return False, "не удалось получить токен Google"
    return True, "токен получен"
