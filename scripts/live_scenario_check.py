#!/usr/bin/env python3
"""Живой сценарий на РЕАЛЬНОЙ PostgreSQL без Telegram.

Фейковая Bot API-сессия: проходим текст, голос, уточнение количества, правку,
повтор и отмену, /missing, /quality, /health. Пишем только под служебными
Telegram id (990000010/11) и удаляем всё созданное в finally.

Запуск: venv/bin/python scripts/live_scenario_check.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "scripts"))

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.types import User  # noqa: E402

from orderbot.ai import AIClient  # noqa: E402
from orderbot.catalog import Catalog  # noqa: E402
from orderbot.config import load_settings  # noqa: E402
from orderbot.db import Database  # noqa: E402
from orderbot.handlers import router  # noqa: E402
from integration_test import FakeSession, Harness  # noqa: E402

CHEF_ID = 990000010
MANAGER_ID = 990000011
USER = User(id=CHEF_ID, is_bot=False, first_name="Live", username="live_chef")
MANAGER = User(id=MANAGER_ID, is_bot=False, first_name="LiveMgr", username="live_mgr")
MISSING_PHRASE = "лайв-трюфель"

problems: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + name + (f" — {detail}" if not ok and detail else ""))
    if not ok:
        problems.append(name)


class FakeTranscriber:
    def __init__(self, text: str) -> None:
        self.text = text

    async def transcribe(self, path):
        return SimpleNamespace(text=self.text)


async def main() -> int:
    base = load_settings()
    tmp = tempfile.TemporaryDirectory()
    settings = replace(
        base,
        data_dir=Path(tmp.name) / "data",
        deepseek_api_key="",
        gemini_api_key="",
        manager_chat_id=MANAGER_ID,
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    catalog = Catalog.load(settings.catalog_path)
    db = Database(settings.db_path, settings.database_url)
    session = FakeSession()
    bot = Bot(
        token="123456:LIVE_SCENARIO_TOKEN",
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    ai = AIClient(settings, catalog)
    transcriber = FakeTranscriber("пять кг куриного филе")
    dp = Dispatcher()
    dp["db"] = db
    dp["settings"] = settings
    dp["catalog"] = catalog
    dp["ai"] = ai
    dp["transcriber"] = transcriber
    dp.include_router(router)
    harness = Harness(bot, dp, session, db)
    max_order = 0
    max_question = 0
    try:
        await db.connect()
        await db.init()
        print(f"БД: {'PostgreSQL' if db.is_postgres else 'SQLite'} · прайс: {len(catalog.items)}")
        cur = await db.conn.execute("SELECT COALESCE(MAX(id),0) m FROM orders")
        max_order = int((await cur.fetchone())["m"])
        cur = await db.conn.execute("SELECT COALESCE(MAX(id),0) m FROM question_log")
        max_question = int((await cur.fetchone())["m"])
        for key in (f"qty_input:{CHEF_ID}", f"onboard:{CHEF_ID}"):
            await db.set_setting(key, "")
        await db.conn.execute("DELETE FROM users WHERE user_id = ?", (CHEF_ID,))
        await db.conn.commit()

        await harness.text("/start", user=USER, chat_id=CHEF_ID)
        check("новый повар: вопрос о заведении",
              any("Как называется ваше заведение" in t for t in harness.texts(CHEF_ID)))
        session.sent.clear()
        await harness.text("Live Kitchen", user=USER, chat_id=CHEF_ID)
        await db.set_user_access(CHEF_ID, "approved")
        await harness.text("/start", user=USER, chat_id=CHEF_ID)
        check("после допуска — меню", any("Напишите заказ" in t for t in harness.texts(CHEF_ID)))

        session.sent.clear()
        await harness.text("2 кг куриного филе, завтра к десяти", user=USER, chat_id=CHEF_ID)
        check("текстовый заказ создан", await db.count_orders() > 0 and
              any("840 ₽" in t for t in harness.texts(CHEF_ID)))

        session.sent.clear()
        before = await db.count_orders()
        await harness.text("куриное филе 2", user=USER, chat_id=CHEF_ID)
        qty_data, qty_msg = harness.find_button("qty:", CHEF_ID)
        check("уточнение количества", qty_data is not None)
        if qty_data and qty_msg:
            await harness.callback(qty_data, qty_msg, user=USER)
        check("заказ после уточнения", await db.count_orders() == before + 1)

        session.sent.clear()
        before = await db.count_orders()
        await harness.voice(user=USER, chat_id=CHEF_ID)
        check("голосовой заказ (подставной текст)", await db.count_orders() == before + 1)

        session.sent.clear()
        await harness.text("куриное филе 2 кг", user=USER, chat_id=CHEF_ID)
        target = (await db.list_orders(1))[0]
        session.sent.clear()
        await harness.text("добавь куриное филе 1 кг", user=USER, chat_id=CHEF_ID)
        edit_data, edit_msg = harness.find_button("editok:yes", CHEF_ID)
        check("правка заказа предлагается", edit_data is not None)
        if edit_data and edit_msg:
            await harness.callback(edit_data, edit_msg, user=USER)
        updated = await db.get_order(target["id"])
        check("правка применилась", updated["total"] == 1260, str(updated["total"]))

        session.sent.clear()
        await harness.text(f"/order {target['id']}", user=USER, chat_id=CHEF_ID)
        repeat_data, repeat_msg = harness.find_button("repeat:", CHEF_ID)
        check("кнопка «Повторить»", repeat_data is not None)
        before = await db.count_orders()
        if repeat_data and repeat_msg:
            await harness.callback(repeat_data, repeat_msg, user=USER)
        check("повтор создал заказ", await db.count_orders() == before + 1)
        cancel_data, cancel_msg = harness.find_button("ccancel:", CHEF_ID)
        check("кнопка «Отменить»", cancel_data is not None)
        if cancel_data and cancel_msg:
            await harness.callback(cancel_data, cancel_msg, user=USER)

        session.sent.clear()
        await harness.text("лайв-трюфель 1 кг", user=USER, chat_id=CHEF_ID)
        none_data, none_msg = harness.find_button("none:", CHEF_ID)
        check("неизвестная позиция: «Нет в прайсе»", none_data is not None)
        if none_data and none_msg:
            await harness.callback(none_data, none_msg, user=USER)
        session.sent.clear()
        await harness.text("/missing", user=MANAGER, chat_id=MANAGER_ID)
        check("/missing отвечает", any("трюфель" in t.lower() for t in harness.texts(MANAGER_ID)),
              str(harness.texts(MANAGER_ID))[:200])

        session.sent.clear()
        await harness.text("/quality", user=MANAGER, chat_id=MANAGER_ID)
        check("/quality отвечает", any("Качество" in t for t in harness.texts(MANAGER_ID)))
        session.sent.clear()
        await harness.text("/health", user=MANAGER, chat_id=MANAGER_ID)
        health = "\n".join(harness.texts(MANAGER_ID))
        check("/health отвечает", "Здоровье" in health, health[:160])
        check("/health: время ответа БД", "ответ" in health and "мс" in health, health[:160])
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL исключение: {type(exc).__name__}: {exc}")
        problems.append("exception")
    finally:
        try:
            ids = await db.list_orders(200)
            for order in ids:
                if int(order["id"]) > max_order:
                    await db.conn.execute("DELETE FROM order_items WHERE order_id = ?", (order["id"],))
                    await db.conn.execute("DELETE FROM orders WHERE id = ?", (order["id"],))
            await db.conn.execute("DELETE FROM drafts WHERE chat_id = ? OR user_id = ?", (CHEF_ID, CHEF_ID))
            await db.conn.execute("DELETE FROM aliases WHERE chef_id = ?", (CHEF_ID,))
            await db.conn.execute("DELETE FROM users WHERE user_id = ?", (CHEF_ID,))
            await db.conn.execute(
                "DELETE FROM missing_items WHERE phrase IN (?, ?)",
                (MISSING_PHRASE, "лайв трюфель"),
            )
            await db.conn.execute("DELETE FROM question_log WHERE id > ?", (max_question,))
            await db.conn.execute("DELETE FROM settings WHERE key LIKE ?", (f"%:{CHEF_ID}",))
            await db.conn.commit()
        except Exception as exc:
            print("  WARN очистка не удалась:", exc)
        try:
            await db.close()
            await bot.session.close()
            await ai.close()
        except Exception:
            pass
        tmp.cleanup()

    if problems:
        print("ЖИВОЙ СЦЕНАРИЙ ПРОВАЛЕН: " + ", ".join(problems))
        return 1
    print("Живой сценарий на PostgreSQL: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
