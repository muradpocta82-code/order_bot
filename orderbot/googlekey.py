"""Приём ключа сервисного аккаунта Google через Telegram (F2).

Владельцу не нужно уметь копировать файлы на сервер: он отправляет JSON-ключ
менеджеру-боту в чат, бот проверяет, что это действительно ключ сервисного
аккаунта, и кладёт его в `/opt/order_bot/google-sa.json` с правами 600.

Что бот не делает: не печатает содержимое ключа в чат и в журнал.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from aiogram import Bot
from aiogram.types import Document

from .config import BASE_DIR, Settings

log = logging.getLogger(__name__)

KEY_FILE = "google-sa.json"
REQUIRED = ("client_email", "private_key", "token_uri")


def env_path() -> Path:
    return BASE_DIR / ".env"


def set_env_value(name: str, value: str) -> None:
    """Пишет строку в .env (заменяет существующую), не печатая значение."""
    path = env_path()
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    found = False
    out = []
    for line in lines:
        if line.startswith(f"{name}="):
            out.append(f"{name}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{name}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def looks_like_service_account(payload: object) -> tuple[bool, str]:
    """Проверяет, что прислали ключ сервисного аккаунта, а не что-то другое."""
    if not isinstance(payload, dict):
        return False, "это не JSON-объект"
    if str(payload.get("type") or "") != "service_account":
        return False, "в файле type не service_account"
    for field in REQUIRED:
        if not str(payload.get(field) or "").strip():
            return False, f"в ключе нет поля {field}"
    if "BEGIN PRIVATE KEY" not in str(payload.get("private_key") or ""):
        return False, "private_key повреждён"
    return True, str(payload.get("client_email") or "")


async def save_key(bot: Bot, settings: Settings, document: Document) -> tuple[bool, str]:
    """Скачивает присланный JSON, проверяет и сохраняет. Возвращает (успех, текст)."""
    if int(document.file_size or 0) > 64 * 1024:
        return False, "файл слишком большой для ключа Google"
    try:
        file = await bot.get_file(document.file_id)
        if file.file_path is None:
            return False, "Telegram не отдал файл"
        raw = await bot.download_file(file.file_path)
        payload = json.loads(raw.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — ошибка разбора не должна ронять бота
        log.info("Ключ Google не прочитан: %s", type(exc).__name__)
        return False, "не получилось прочитать JSON"
    ok, detail = looks_like_service_account(payload)
    if not ok:
        return False, detail

    target = BASE_DIR / KEY_FILE
    try:
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        target.chmod(0o600)
        try:
            import shutil

            shutil.chown(target, user="orderbot", group="orderbot")
        except (LookupError, PermissionError, OSError):
            pass
    except OSError as exc:
        log.warning("Ключ Google не сохранён: %s", type(exc).__name__)
        return False, "не удалось сохранить файл на сервере"

    set_env_value("GOOGLE_SA_FILE", str(target))
    log.info("Ключ сервисного аккаунта Google сохранён (%s)", detail)
    return True, detail
