#!/usr/bin/env python3
"""Живая проверка /start, заведения и приветствия на РЕАЛЬНОЙ базе.

Фальшивое Telegram-сообщение, сеть не используется, БД — та же, что у бота.
Проверяет: новый повар получает вопрос о заведении, после допуска — меню,
менеджер — панель, приветствие отвечает, а битая транзакция не ломает /start.
Служебные строки удаляются в finally.

Запуск: venv/bin/python scripts/live_start_check.py
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from orderbot.catalog import Catalog  # noqa: E402
from orderbot.config import load_settings  # noqa: E402
from orderbot.db import Database  # noqa: E402
from orderbot.handlers import START_TEXT, _intent_reply, cmd_start  # noqa: E402

CHEF_ID = 990000001
MANAGER_ID = 990000002
KEYS = (f"qty_input:{CHEF_ID}", f"qty_input:{MANAGER_ID}", f"onboard:{CHEF_ID}")


class FakeMessage:
    """Минимальный заменитель aiogram.Message: только то, что нужно /start."""

    def __init__(self, chat_id: int, user_id: int) -> None:
        self.chat = SimpleNamespace(id=chat_id)
        self.from_user = SimpleNamespace(
            id=user_id, full_name="Live Check", username="live_check"
        )
        self.sent: list[tuple[str, object]] = []

    async def answer(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return self


async def main() -> int:
    settings = replace(load_settings(), manager_chat_id=MANAGER_ID)
    catalog = Catalog.load(settings.catalog_path)
    db = Database(settings.db_path, settings.database_url)
    problems: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("  OK   " if ok else "  FAIL ") + name + (f" — {detail}" if not ok and detail else ""))
        if not ok:
            problems.append(name)

    try:
        await db.connect()
        await db.init()
        print(f"БД: {'PostgreSQL' if db.is_postgres else 'SQLite'}")
        # Чистим следы прошлой проверки.
        for key in KEYS:
            await db.set_setting(key, "")
        await db.conn.execute("DELETE FROM users WHERE user_id = ?", (CHEF_ID,))
        await db.conn.commit()

        chef = FakeMessage(CHEF_ID, CHEF_ID)
        await cmd_start(chef, db, settings, catalog)
        text = "\n".join(item[0] for item in chef.sent)
        check("/start повара отвечает", bool(chef.sent))
        check(
            "/start повара: спрашивает заведение",
            "Как называется ваше заведение" in text,
            text[:120],
        )

        # Повар назвал заведение, менеджер допустил.
        await db.set_user_venue(CHEF_ID, "Live Check Kitchen")
        await db.set_user_phone(CHEF_ID, "+993 12 000000")
        await db.set_user_address(CHEF_ID, "Ашхабад, Live Check, 1")
        await db.set_user_access(CHEF_ID, "approved")
        chef = FakeMessage(CHEF_ID, CHEF_ID)
        await cmd_start(chef, db, settings, catalog)
        text = "\n".join(item[0] for item in chef.sent)
        check("/start повара после допуска", bool(chef.sent))
        check("/start повара: текст приветствия", START_TEXT in text, text[:120])
        check("/start повара: есть кнопки", bool(chef.sent and chef.sent[0][1] is not None))

        manager = FakeMessage(MANAGER_ID, MANAGER_ID)
        await cmd_start(manager, db, settings, catalog)
        panel = "\n".join(item[0] for item in manager.sent)
        check("/start менеджера: панель", "Панель менеджера" in panel, panel[:120])

        reply = await _intent_reply("greeting", CHEF_ID, CHEF_ID, db, settings, catalog)
        check("привет повара: ответ", reply is not None)
        check("привет повара: текст", bool(reply and reply[0] == START_TEXT))

        # Битая транзакция не должна убивать соединение: после ошибки /start
        # обязан снова работать (регрессия 25P02).
        try:
            await db.conn.execute("SELECT * FROM __no_such_table_live_check__")
        except Exception:
            pass
        after_error = FakeMessage(CHEF_ID, CHEF_ID)
        await cmd_start(after_error, db, settings, catalog)
        check("/start после ошибки БД", bool(after_error.sent))
        await db.set_setting(KEYS[0], "")
        check("запись после ошибки БД", await db.get_setting(KEYS[0]) == "")
    except Exception as exc:  # noqa: BLE001 — нужно вернуть понятный код
        print(f"  FAIL исключение: {type(exc).__name__}: {exc}")
        problems.append("exception")
    finally:
        for key in KEYS:
            try:
                await db.set_setting(key, "")
            except Exception:
                pass
        try:
            await db.conn.execute("DELETE FROM users WHERE user_id = ?", (CHEF_ID,))
            await db.conn.commit()
        except Exception:
            pass
        try:
            await db.close()
        except Exception:
            pass

    if problems:
        print("ЖИВАЯ ПРОВЕРКА /start ПРОВАЛЕНА: " + ", ".join(problems))
        return 1
    print("Живая проверка /start и приветствия: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
