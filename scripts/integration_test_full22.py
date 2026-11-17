"""Сквозной тест логики бота без Telegram: фейковая Bot API-сессия.

Запуск: venv/bin/python scripts/integration_test.py
Проверяет: команды, разбор заказа, кнопки выбора, количество, подтверждение,
сохранение в БД и отправку менеджеру.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.exceptions import TelegramNetworkError  # noqa: E402
from aiogram.types import CallbackQuery, Chat, File, Message, Update, User  # noqa: E402

from orderbot.ai import AIClient  # noqa: E402
from orderbot.catalog import Catalog  # noqa: E402
from orderbot.config import load_settings  # noqa: E402
from orderbot.db import Database, reset_test_database  # noqa: E402
from orderbot.handlers import router  # noqa: E402
from orderbot.stt import Transcriber  # noqa: E402
from main import _resend_unnotified  # noqa: E402

CHAT_ID = 1001
MANAGER_ID = 2002
USER = User(id=CHAT_ID, is_bot=False, first_name="Повар", username="chef")
MANAGER = User(id=MANAGER_ID, is_bot=False, first_name="Менеджер", username="manager")

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


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[object] = []
        self.sent: list[Message] = []
        self._message_id = 100
        self.fail_manager = False

    async def close(self) -> None:
        return None

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield b""

    async def make_request(self, bot: Bot, method, timeout: int | None = None):
        self.calls.append(method)
        name = type(method).__name__

        if name == "GetMe":
            return User(id=1, is_bot=True, first_name="TestBot", username="test_bot").as_(bot)

        if name == "SendMessage":
            if self.fail_manager and getattr(method, "chat_id", None) == MANAGER_ID:
                raise TelegramNetworkError(method, "simulated timeout")
            self._message_id += 1
            chat = Chat(id=getattr(method, "chat_id", CHAT_ID), type="private")
            message = Message(
                message_id=self._message_id,
                date=datetime.now(),
                chat=chat,
                from_user=User(id=1, is_bot=True, first_name="TestBot", username="test_bot"),
                text=getattr(method, "text", None),
                reply_markup=getattr(method, "reply_markup", None),
            ).as_(bot)
            self.sent.append(message)
            return message

        if name == "EditMessageText":
            self._message_id += 1
            chat_id = getattr(method, "chat_id", CHAT_ID)
            message = Message(
                message_id=getattr(method, "message_id", self._message_id),
                date=datetime.now(),
                chat=Chat(id=chat_id, type="private"),
                from_user=User(id=1, is_bot=True, first_name="TestBot", username="test_bot"),
                text=getattr(method, "text", ""),
                reply_markup=getattr(method, "reply_markup", None),
            ).as_(bot)
            self.sent.append(message)
            return message

        if name == "GetFile":
            return File(file_id="f", file_unique_id="u", file_size=10, file_path="voice/x.ogg").as_(bot)

        return True


class Harness:
    def __init__(self, bot: Bot, dp: Dispatcher, session: FakeSession, db: Database):
        self.bot = bot
        self.dp = dp
        self.session = session
        self.db = db
        self.update_id = 1

    async def text(self, text: str, user: User = USER, chat_id: int = CHAT_ID) -> None:
        self.update_id += 1
        message = Message(
            message_id=self.update_id,
            date=datetime.now(),
            chat=Chat(id=chat_id, type="private"),
            from_user=user,
            text=text,
        ).as_(self.bot)
        await self.dp.feed_update(self.bot, Update(update_id=self.update_id, message=message))

    async def callback(self, data: str, message: Message, user: User = USER) -> None:
        self.update_id += 1
        query = CallbackQuery(
            id=str(self.update_id),
            from_user=user,
            chat_instance="test",
            message=message,
            data=data,
        )
        await self.dp.feed_update(self.bot, Update(update_id=self.update_id, callback_query=query))

    def find_button(self, prefix: str, chat_id: int | None = None) -> tuple[str | None, Message | None]:
        for message in reversed(self.session.sent):
            if chat_id is not None and message.chat.id != chat_id:
                continue
            markup = message.reply_markup
            if markup is None:
                continue
            for row in markup.inline_keyboard:
                for button in row:
                    if button.callback_data and button.callback_data.startswith(prefix):
                        return button.callback_data, message
        return None, None

    def texts(self, chat_id: int | None = None) -> list[str]:
        return [
            message.text or ""
            for message in self.session.sent
            if chat_id is None or message.chat.id == chat_id
        ]


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        base = load_settings()
        settings = replace(
            base,
            db_path=Path(tmp) / "test.db",
            data_dir=Path(tmp) / "data",
            deepseek_api_key="",
            gemini_api_key="",
            manager_chat_id=MANAGER_ID,
        )
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        catalog = Catalog.load(settings.catalog_path)
        test_url = os.getenv("TEST_DATABASE_URL", "").strip()
        db = Database(settings.db_path, test_url) if test_url else Database(settings.db_path)
        await db.connect()
        if test_url:
            await reset_test_database(db)
        await db.init()

        ai = AIClient(settings, catalog)
        transcriber = Transcriber(settings, ai)
        session = FakeSession()
        bot = Bot(
            token="123456:TEST_TOKEN_FOR_INTEGRATION",
            session=session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        dp = Dispatcher()
        dp["db"] = db
        dp["settings"] = settings
        dp["catalog"] = catalog
        dp["ai"] = ai
        dp["transcriber"] = transcriber
        dp.include_router(router)
        harness = Harness(bot, dp, session, db)

        print("=" * 70)
        print("СКВОЗНОЙ ТЕСТ ЛОГИКИ (fake Telegram)")
        print(f"БД: {'PostgreSQL (TEST_DATABASE_URL)' if test_url else 'SQLite'}")
        print("=" * 70)

        print("\n[1] /start: заведение, допуск, разделение ролей")
        session.sent.clear()
        await harness.text("/start")
        check(
            "спрашивает название заведения",
            any("Как называется ваше заведение" in text for text in harness.texts(CHAT_ID)),
            str(harness.texts(CHAT_ID))[:200],
        )
        session.sent.clear()
        await harness.text("Тестовая кухня")
        check(
            "новый повар ждёт подтверждения",
            any("Ждём подтверждения" in text for text in harness.texts(CHAT_ID)),
            str(harness.texts(CHAT_ID))[:200],
        )
        acc_data, acc_msg = harness.find_button("acc:ok:", MANAGER_ID)
        check("менеджер получил заявку", acc_data is not None)
        assert acc_data and acc_msg
        session.sent.clear()
        await harness.callback(acc_data, acc_msg, user=MANAGER)
        check(
            "повару сообщили о допуске",
            any("Доступ подтверждён" in text for text in harness.texts(CHAT_ID)),
            str(harness.texts(CHAT_ID))[:200],
        )
        check(
            "доступ сохранён",
            (await db.get_user(CHAT_ID))["access"] == "approved",
        )
        check(
            "заведение сохранено",
            (await db.get_user(CHAT_ID))["venue"] == "Тестовая кухня",
        )

        session.sent.clear()
        await harness.text("/start")
        check("клиентское приветствие", any("Напишите заказ" in text for text in harness.texts()))
        check("клиентское меню", harness.find_button("cl:price")[0] is not None)
        session.calls.clear()
        price_data, price_msg = harness.find_button("cl:price", CHAT_ID)
        check("кнопка прайса", price_data is not None and price_msg is not None)
        assert price_data and price_msg
        await harness.callback(price_data, price_msg)
        check(
            "прайс меняет сообщение, а не шлёт новое",
            not any(type(call).__name__ == "SendMessage" for call in session.calls),
            str([type(call).__name__ for call in session.calls]),
        )
        check("прайс показан", any("Наш список" in text for text in harness.texts(CHAT_ID)))
        session.calls.clear()
        menu_data, menu_msg = harness.find_button("cl:menu", CHAT_ID)
        check("кнопка возврата", menu_data is not None and menu_msg is not None)
        assert menu_data and menu_msg
        await harness.callback(menu_data, menu_msg)
        check(
            "возврат меняет сообщение",
            not any(type(call).__name__ == "SendMessage" for call in session.calls),
            str([type(call).__name__ for call in session.calls]),
        )
        check("снова старт", any("Напишите заказ" in text for text in harness.texts(CHAT_ID)))
        session.sent.clear()
        await harness.text("/start", user=MANAGER, chat_id=MANAGER_ID)
        panel_text = "\n".join(harness.texts(MANAGER_ID))
        check("панель менеджера", "Панель менеджера" in panel_text, panel_text[:200])
        check("без клиентского текста", "Напишите заказ" not in panel_text)
        check("кнопка «Входящие»", harness.find_button("mp:inbox:0", MANAGER_ID)[0] is not None)
        check("кнопка «История»", harness.find_button("mp:history:0", MANAGER_ID)[0] is not None)

        print("\n[1b] Приветствие: повар и менеджер")
        session.sent.clear()
        await harness.text("привет")
        check(
            "повар: приветствие отвечает",
            any("Напишите заказ" in text for text in harness.texts(CHAT_ID)),
            str(harness.texts(CHAT_ID))[:200],
        )
        check(
            "повар: меню после приветствия",
            harness.find_button("cl:price", CHAT_ID)[0] is not None,
        )
        check("повар: заказ не создан", len(await db.list_orders(10)) == 0)
        session.sent.clear()
        await harness.text("привет", user=MANAGER, chat_id=MANAGER_ID)
        manager_greeting = "\n".join(harness.texts(MANAGER_ID))
        check(
            "менеджер: не принимает заказ на себя",
            "Вы менеджер" in manager_greeting,
            manager_greeting[:200],
        )
        check(
            "менеджер: кнопки панели",
            harness.find_button("mp:inbox:0", MANAGER_ID)[0] is not None,
        )
        check("менеджер: заказ не создан", len(await db.list_orders(10)) == 0)

        print("\n[2] Заказ из ТЗ → сразу менеджеру")
        session.sent.clear()
        await harness.text(
            "бекона коробку, моцареллы два пакета, сметану двадцатку пять штук, завтра к десяти"
        )
        summary = "\n".join(harness.texts(CHAT_ID))
        check("итог 14 150 ₽", "14 150 ₽" in summary, summary[:300])
        check("3 позиции", summary.count(" = ") == 3, str(summary.count(" = ")))
        check("нет кнопки подтверждения", harness.find_button("confirm:", CHAT_ID)[0] is None)
        orders = await db.list_orders(10)
        check("заказ сохранён", len(orders) == 1, str(orders))
        check("сумма заказа 14 150 ₽", orders[0]["total"] == 14150, str(orders[0]["total"]))
        check(
            "менеджер получил заказ",
            any("Новый заказ" in text for text in harness.texts(MANAGER_ID)),
            str(harness.texts(MANAGER_ID))[:300],
        )

        print("\n[3] Неоднозначность «масло» → выбор кнопкой")
        session.sent.clear()
        await harness.text("масло 2 кг")
        candidate_text = "\n".join(harness.texts(CHAT_ID))
        check("вопрос по маслу", "это что именно" in candidate_text, candidate_text[:300])
        pick_data, pick_msg = harness.find_button("pick:", CHAT_ID)
        check("кнопки выбора", pick_data is not None)
        assert pick_data and pick_msg
        await harness.callback(pick_data, pick_msg)
        summary = "\n".join(harness.texts(CHAT_ID))
        check("заказ отправлен после выбора", "Заказ отправлен" in summary, summary[:400])
        orders = await db.list_orders(10)
        check("второй заказ сохранён", len(orders) == 2, str(len(orders)))
        check("сумма масла 1 960 ₽", orders[0]["total"] == 1960, str(orders[0]["total"]))
        check(
            "менеджер получил второй заказ",
            any("Новый заказ №<b>2</b>" in text for text in harness.texts(MANAGER_ID)),
            str(harness.texts(MANAGER_ID))[:300],
        )
        aliases = await db.get_aliases(CHAT_ID)
        picked_id = int(pick_data.split(":")[3])
        check(
            "выбор кандидата сохранён в aliases",
            aliases.get("масло") == picked_id,
            str(aliases),
        )
        order2 = await db.get_order(orders[0]["id"])
        check("вопрос выбора учтён", order2["questions"] == 1, str(order2["questions"]))

        print("\n[4] Заказ без количества → кнопки количества")
        session.sent.clear()
        await harness.text("бекон 2")
        qty_text = "\n".join(harness.texts(CHAT_ID))
        check("вопрос о количестве", "Сколько нужно" in qty_text, qty_text[:300])
        qty_data, qty_msg = harness.find_button("qty:", CHAT_ID)
        check("кнопки количества", qty_data is not None)
        assert qty_data and qty_msg
        qty_five = next(
            (
                button.callback_data
                for row in qty_msg.reply_markup.inline_keyboard
                for button in row
                if button.callback_data and button.callback_data.endswith(":5")
            ),
            None,
        )
        check("кнопка «5»", qty_five is not None)
        assert qty_five
        await harness.callback(qty_five, qty_msg)
        summary = "\n".join(harness.texts(CHAT_ID))
        check("заказ отправлен после количества", "Заказ отправлен" in summary, summary[:400])
        orders = await db.list_orders(10)
        check("третий заказ сохранён", len(orders) == 3, str(len(orders)))
        check("бекон 5 коробок = 39 000 ₽", orders[0]["total"] == 39000, str(orders[0]["total"]))
        check(
            "менеджер получил третий заказ",
            any("Новый заказ №<b>3</b>" in text for text in harness.texts(MANAGER_ID)),
            str(harness.texts(MANAGER_ID))[:300],
        )
        order3 = await db.get_order(orders[0]["id"])
        check("вопрос количества учтён", order3["questions"] == 1, str(order3["questions"]))

        print("\n[5] Команды менеджера и прайс")
        session.sent.clear()
        await harness.text("/orders", user=USER, chat_id=CHAT_ID)
        check(
            "повару /orders запрещена",
            any("только менеджеру" in text for text in harness.texts()),
            str(harness.texts())[:200],
        )
        session.sent.clear()
        await harness.text("/manager", user=USER, chat_id=CHAT_ID)
        check(
            "/manager удалена",
            any("Не знаю такую команду" in text for text in harness.texts()),
            str(harness.texts())[:200],
        )
        session.sent.clear()
        await harness.text("/orders", user=MANAGER, chat_id=MANAGER_ID)
        check("список заказов", any("Входящие заказы" in text for text in harness.texts(MANAGER_ID)))
        session.sent.clear()
        await harness.text("/price")
        check("прайс отправлен", any("Наш список" in text for text in harness.texts()))
        session.sent.clear()
        await harness.text("/id")
        check("chat_id", any("Ваш чат" in text for text in harness.texts()))

        print("\n[6] Статус заказа менеджером")
        session.sent.clear()
        order = await db.get_order(orders[0]["id"])
        # заказ создан до очистки; отправим /order заново
        await harness.text(f"/order {order['id']}", user=MANAGER, chat_id=MANAGER_ID)
        status_data, status_msg = harness.find_button("ost:", MANAGER_ID)
        check("кнопки статуса", status_data is not None)
        if status_data and status_msg:
            await harness.callback(status_data, status_msg, user=MANAGER)
            updated = await db.get_order(order["id"])
            check("статус изменён", updated["status"] in ("accepted", "done", "cancelled"), str(updated["status"]))

        print("\n[7] Панель менеджера: история и входящие")
        session.sent.clear()
        await harness.text("/start", user=MANAGER, chat_id=MANAGER_ID)
        history_data, history_msg = harness.find_button("mp:history:0", MANAGER_ID)
        check("кнопка «История»", history_data is not None)
        assert history_data and history_msg
        await harness.callback(history_data, history_msg, user=MANAGER)
        history_text = "\n".join(harness.texts(MANAGER_ID))
        check(
            "история содержит все 3 заказа",
            all(f"№<b>{i}</b>" in history_text for i in (1, 2, 3)),
            history_text[-500:],
        )
        inbox_data, inbox_msg = harness.find_button("mp:inbox:0", MANAGER_ID)
        check("кнопка «Входящие»", inbox_data is not None)
        assert inbox_data and inbox_msg
        await harness.callback(inbox_data, inbox_msg, user=MANAGER)
        inbox_text = "\n".join(harness.texts(MANAGER_ID))
        check("входящие заказы", "Входящие заказы" in inbox_text, inbox_text[-400:])

        orders_before = await db.count_orders()
        session.sent.clear()
        await harness.text("бекон 2", user=MANAGER, chat_id=MANAGER_ID)
        check(
            "менеджер не оформляет заказы",
            any("Вы менеджер" in text for text in harness.texts(MANAGER_ID)),
            str(harness.texts(MANAGER_ID))[:200],
        )
        check("новый заказ не создан", await db.count_orders() == orders_before)

        print("\n[8] Нет в прайсе → /missing")
        session.sent.clear()
        orders_before_missing = await db.count_orders()
        await harness.text("трюфель 1 кг")
        none_data, none_msg = harness.find_button("none:", CHAT_ID)
        check("кнопка «Нет в прайсе»", none_data is not None)
        assert none_data and none_msg
        await harness.callback(none_data, none_msg)
        check(
            "заказ с неизвестной позицией сохранён",
            await db.count_orders() == orders_before_missing + 1,
        )
        session.sent.clear()
        await harness.text("/missing", user=MANAGER, chat_id=MANAGER_ID)
        missing_text = "\n".join(harness.texts(MANAGER_ID))
        check("менеджеру показана фраза", "трюфель" in missing_text, missing_text[:300])
        check("менеджеру показан счётчик", "1" in missing_text, missing_text[:300])
        session.sent.clear()
        await harness.text("/missing", user=USER, chat_id=CHAT_ID)
        check(
            "повару /missing запрещена",
            any("только менеджеру" in text for text in harness.texts(CHAT_ID)),
            str(harness.texts(CHAT_ID))[:200],
        )

        print("\n[9] /quality")
        session.sent.clear()
        await harness.text("/quality", user=MANAGER, chat_id=MANAGER_ID)
        quality_text = "\n".join(harness.texts(MANAGER_ID))
        check("отчёт качества", "Качество" in quality_text, quality_text[:300])
        check("процент без вопросов", "25%" in quality_text, quality_text[:300])
        check("топ фраз", "масло" in quality_text.lower(), quality_text[:300])

        print("\n[10] Повторить и отменить")
        session.sent.clear()
        source = await db.get_order(orders[0]["id"])
        await harness.text(f"/order {source['id']}")
        repeat_data, repeat_msg = harness.find_button("repeat:", CHAT_ID)
        check("кнопка «Повторить»", repeat_data is not None)
        assert repeat_data and repeat_msg
        before_repeat = await db.count_orders()
        await harness.callback(repeat_data, repeat_msg)
        check("повтор создал заказ", await db.count_orders() == before_repeat + 1)
        latest = (await db.list_orders(1))[0]
        repeated = await db.get_order(latest["id"])
        tomorrow = (
            datetime.now(settings.timezone).date() + timedelta(days=1)
        ).isoformat()
        check("доставка завтра", repeated["delivery_date"] == tomorrow, str(repeated["delivery_date"]))
        check("позиции повторились", len(repeated["items"]) == len(source["items"]))
        check("сумма повторилась", repeated["total"] == source["total"], str(repeated["total"]))
        check("вопросов нет", repeated["questions"] == 0, str(repeated["questions"]))

        cancel_data, cancel_msg = harness.find_button("ccancel:", CHAT_ID)
        check("кнопка «Отменить»", cancel_data is not None)
        assert cancel_data and cancel_msg
        session.calls.clear()
        await harness.callback(cancel_data, cancel_msg)
        cancelled = await db.get_order(repeated["id"])
        check("заказ отменён", cancelled["status"] == "cancelled", str(cancelled["status"]))
        check(
            "менеджер уведомлён об отмене",
            any(
                f"Повар отменил заказ №{repeated['id']}" in text
                for text in harness.texts(MANAGER_ID)
            ),
            str(harness.texts(MANAGER_ID))[-300:],
        )
        deleted = [
            (call.chat_id, call.message_id)
            for call in session.calls
            if type(call).__name__ == "DeleteMessage"
        ]
        check(
            "карточки обеих сторон удалены",
            any(chat == CHAT_ID for chat, _ in deleted)
            and any(chat == MANAGER_ID for chat, _ in deleted),
            str(deleted),
        )
        check(
            "повару короткое сообщение об отмене",
            any(
                text.strip() == f"Заказ №{repeated['id']} отменён"
                for text in harness.texts(CHAT_ID)
            ),
            str(harness.texts(CHAT_ID))[-300:],
        )

        await db.update_order_status(source["id"], "accepted")
        session.calls.clear()
        await harness.callback(f"ccancel:{source['id']}", repeat_msg)
        accepted = await db.get_order(source["id"])
        check("заказ в работе не отменяется", accepted["status"] == "accepted")
        answers = [
            call for call in session.calls if type(call).__name__ == "AnswerCallbackQuery"
        ]
        check(
            "подсказка про менеджера",
            any("уже в работе" in (getattr(call, "text", "") or "") for call in answers),
            str([getattr(call, "text", "") for call in answers]),
        )

        print("\n[11] Как обычно")
        session.sent.clear()
        await harness.text("/start", user=USER, chat_id=CHAT_ID)
        usual_data, usual_msg = harness.find_button("usual:", CHAT_ID)
        check("кнопки «как обычно»", usual_data is not None)
        assert usual_data and usual_msg
        before_usual = await db.count_orders()
        await harness.callback(usual_data, usual_msg)
        check("заказ «как обычно» создан", await db.count_orders() == before_usual + 1)
        usual_order = await db.get_order((await db.list_orders(1))[0]["id"])
        check("доставка завтра", usual_order["delivery_date"] == tomorrow, str(usual_order["delivery_date"]))
        check("одна позиция", len(usual_order["items"]) == 1)
        check("без вопросов", usual_order["questions"] == 0, str(usual_order["questions"]))
        check("количество из истории", usual_order["items"][0]["qty"] in (1, 5), str(usual_order["items"][0]["qty"]))

        print("\n[12] Не-заказы без AI")
        session.sent.clear()
        orders_before_intents = await db.count_orders()
        await harness.text("привет")
        check("приветствие", any("Здравствуйте" in text for text in harness.texts(CHAT_ID)))
        await harness.text("спасибо")
        check("благодарность", any("Пожалуйста" in text for text in harness.texts(CHAT_ID)))
        await harness.text("где мой заказ")
        check("статус заказа", any("Статус:" in text for text in harness.texts(CHAT_ID)))
        session.sent.clear()
        await harness.text("отмените")
        check(
            "отмена показывает кнопку",
            harness.find_button("ccancel:", CHAT_ID)[0] is not None,
        )
        await harness.text("покажи прайс")
        check("прайс", any("Наш список" in text for text in harness.texts(CHAT_ID)))
        await harness.text("помощь")
        check(
            "помощь",
            any("Напишите заказ одним сообщением" in text for text in harness.texts(CHAT_ID)),
        )
        check("не-заказы не создают заказ", await db.count_orders() == orders_before_intents)

        print("\n[13] /health")
        session.sent.clear()
        await harness.text("/health", user=MANAGER, chat_id=MANAGER_ID)
        health_text = "\n".join(harness.texts(MANAGER_ID))
        check("отчёт здоровья", "Здоровье" in health_text, health_text[:300])
        check("ждут отправки", "Ждут отправки" in health_text, health_text[:300])
        session.sent.clear()
        await harness.text("/health", user=USER, chat_id=CHAT_ID)
        check(
            "повару /health запрещена",
            any("только менеджеру" in text for text in harness.texts(CHAT_ID)),
            str(harness.texts(CHAT_ID))[:200],
        )

        print("\n[14] DeepSeek недоступен")
        class BrokenAI:
            async def parse(self, text, now):
                raise TimeoutError("deepseek down")

            async def close(self):
                return None

        dp["ai"] = BrokenAI()
        session.sent.clear()
        before_broken = await db.count_orders()
        await harness.text("филе 2 кг, завтра")
        check(
            "локальный разбор при недоступном AI",
            await db.count_orders() == before_broken + 1,
        )
        dp["ai"] = ai

        print("\n[15] Telegram не доставил заказ менеджеру")
        session.sent.clear()
        session.fail_manager = True
        before_timeout = await db.count_orders()
        await harness.text("сметана 5 штук")
        check(
            "заказ сохранён при таймауте",
            await db.count_orders() == before_timeout + 1,
        )
        latest = (await db.list_orders(1))[0]
        pending_order = await db.get_order(latest["id"])
        check("заказ ждёт досылки", pending_order["notified_at"] is None)
        check(
            "повар видит сохранение",
            any("Заказ сохранён" in text for text in harness.texts(CHAT_ID)),
            str(harness.texts(CHAT_ID))[-300:],
        )
        session.fail_manager = False
        await _resend_unnotified(bot, db, settings)
        pending_order = await db.get_order(latest["id"])
        check("заказ дослан", pending_order["notified_at"] is not None)
        check(
            "менеджер получил досылку",
            any(f"№<b>{latest['id']}</b>" in text for text in harness.texts(MANAGER_ID)),
            str(harness.texts(MANAGER_ID))[-300:],
        )

        print("\n[16] Лимит 20 заказов в час")
        original_counter = db.count_orders_since

        async def fake_counter(chef_id, minutes=60):
            return 20

        db.count_orders_since = fake_counter
        session.sent.clear()
        before_limit = await db.count_orders()
        await harness.text("филе 2 кг, завтра")
        check("лимит не создаёт заказ", await db.count_orders() == before_limit)
        check(
            "вежливый отказ",
            any("20 заказов" in text for text in harness.texts(CHAT_ID)),
            str(harness.texts(CHAT_ID))[-200:],
        )
        db.count_orders_since = original_counter
        await db.close()
        await bot.session.close()
        await ai.close()

    print("\n" + "=" * 70)
    print(f"Пройдено: {passed}   Провалено: {len(failed)}")
    if failed:
        print("Провалы: " + ", ".join(failed))
        return 1
    print("СКВОЗНОЙ ТЕСТ ПРОЙДЕН")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
