#!/usr/bin/env python3
"""Проверка полного потока: клиент → упаковщик → доставщик → оплата → доставлено."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import User

from orderbot.ai import AIClient
from orderbot.catalog import Catalog
from orderbot.config import load_settings
from orderbot.db import Database, reset_test_database
from orderbot.handlers import router
from scripts.integration_test import FakeSession, Harness

CLIENT_ID = 7101
PACKER_ID = 7202
COURIER_ID = 7303
CLIENT = User(id=CLIENT_ID, is_bot=False, first_name="Клиент", username="client")
PACKER = User(id=PACKER_ID, is_bot=False, first_name="Упаковщик", username="packer")
COURIER = User(id=COURIER_ID, is_bot=False, first_name="Доставщик", username="courier")

passed = 0
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed.append(name)
        print(f"  ✗ {name} {detail}")


def button_with_suffix(harness: Harness, suffix: str, chat_id: int):
    for message in reversed(harness.session.sent):
        if message.chat.id != chat_id or message.reply_markup is None:
            continue
        for row in message.reply_markup.inline_keyboard:
            for button in row:
                if button.callback_data and button.callback_data.endswith(suffix):
                    return button.callback_data, message
    return None, None


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        base = load_settings()
        settings = replace(
            base,
            db_path=Path(tmp) / "flow.db",
            data_dir=Path(tmp) / "data",
            deepseek_api_key="",
            gemini_api_key="",
            manager_chat_id=PACKER_ID,
            courier_chat_id=COURIER_ID,
        )
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        catalog = Catalog.load(settings.catalog_path)
        test_url = os.getenv("TEST_DATABASE_URL", "").strip()
        db = Database(settings.db_path, test_url) if test_url else Database(settings.db_path)
        await db.connect()
        if test_url:
            await reset_test_database(db)
        await db.init()

        await db.ensure_user(CLIENT_ID, "Клиент", "client")
        await db.set_user_venue(CLIENT_ID, "Тестовая кухня")
        await db.set_user_phone(CLIENT_ID, "+993 12 345678")
        await db.set_user_address(CLIENT_ID, "Ашхабад, ул. Тестовая, 1")
        await db.set_user_access(CLIENT_ID, "approved")

        session = FakeSession()
        bot = Bot(
            token="123456:TEST_TOKEN_FOR_DELIVERY_FLOW",
            session=session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        dp = Dispatcher()
        dp["db"] = db
        dp["settings"] = settings
        dp["catalog"] = catalog
        dp["ai"] = AIClient(settings, catalog)
        dp["transcriber"] = None
        dp.include_router(router)
        harness = Harness(bot, dp, session, db)

        await harness.text("2 кг куриного филе, завтра к 10:00", user=CLIENT, chat_id=CLIENT_ID)
        orders = await db.list_orders(5)
        check("заказ создан", len(orders) == 1, str(orders))
        order_id = orders[0]["id"]
        order = await db.get_order(order_id)
        check("адрес клиента в заказе", order["delivery_address"].startswith("Ашхабад"))
        check("оплата наличными по умолчанию", order["payment_method"] == "cash")
        check("упаковщик получил заказ", harness.find_button("ost:", PACKER_ID)[0] is not None)

        ready, ready_msg = button_with_suffix(harness, ":ready", PACKER_ID)
        check("кнопка «Собран»", ready is not None)
        assert ready and ready_msg
        await harness.callback(ready, ready_msg, user=PACKER)
        order = await db.get_order(order_id)
        check("статус ready", order["status"] == "ready", str(order["status"]))
        check("доставщик назначен", order["courier_chat_id"] == COURIER_ID, str(order["courier_chat_id"]))
        pay, pay_msg = harness.find_button("cour:pay:", COURIER_ID)
        check("доставщик получил карточку", pay is not None)
        check("клиенту сообщили о сборке", any("собран" in t.lower() for t in harness.texts(CLIENT_ID)))

        assert pay and pay_msg
        await harness.callback(pay, pay_msg, user=COURIER)
        order = await db.get_order(order_id)
        expected_total = float(order["total"])
        await harness.text(str(expected_total), user=COURIER, chat_id=COURIER_ID)
        order = await db.get_order(order_id)
        check("оплата наличными зафиксирована", order["payment_status"] == "paid", str(order["payment_status"]))
        check("сумма оплаты", float(order["paid_amount"] or 0) == expected_total, str(order["paid_amount"]))
        check("клиенту сообщили об оплате", any("оплата" in t.lower() for t in harness.texts(CLIENT_ID)))

        done, done_msg = harness.find_button("cour:done:", COURIER_ID)
        check("кнопка «Доставлено»", done is not None)
        assert done and done_msg
        await harness.callback(done, done_msg, user=COURIER)
        pin = (await db.get_order(order_id)).get("delivery_pin")
        if pin:
            await harness.text(str(pin), user=COURIER, chat_id=COURIER_ID)
            done2, done2_msg = harness.find_button("cour:done:", COURIER_ID)
            if done2 and done2_msg:
                await harness.callback(done2, done2_msg, user=COURIER)
        order = await db.get_order(order_id)
        check("статус done", order["status"] == "done", str(order["status"]))
        check("время доставки записано", bool(order["delivered_at"]), str(order["delivered_at"]))
        check("упаковщику сообщили о доставке", any("доставлен" in t.lower() for t in harness.texts(PACKER_ID)))

        try:
            await db.close()
        except Exception:
            pass

    print(f"\nПройдено: {passed}   Провалено: {len(failed)}")
    if failed:
        print("Провалы: " + ", ".join(failed))
        return 1
    print("ПОТОК ДОСТАВКИ ПРОВЕРЕН")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
