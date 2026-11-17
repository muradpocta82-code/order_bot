"""Сквозной тест бота на прайсе из одной позиции (Куриное филе).

Фейковая Telegram-сессия, без сети. Покрывает: заведение и допуск, текст,
голос, уточнение количества, выбор фасовки, правку заказа, повтор, отмену
с удалением карточек, остатки, отсечку/выходные, /missing, /quality, /health,
/export, напоминание и кнопку повтора после сбоя.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.types import InlineKeyboardMarkup  # noqa: E402
from aiogram.types import (  # noqa: E402, InlineKeyboardMarkup
    CallbackQuery,
    Chat,
    File,
    Message,
    Update,
    User,
    Voice,
)

from orderbot.ai import AIClient  # noqa: E402
from orderbot.catalog import Catalog  # noqa: E402
from orderbot.config import load_settings  # noqa: E402
from orderbot.db import Database, reset_test_database  # noqa: E402
from orderbot.handlers import router  # noqa: E402

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
            self._message_id += 1
            chat = Chat(id=getattr(method, "chat_id", CHAT_ID), type="private")
            message = Message(
                message_id=self._message_id,
                date=datetime.now(),
                chat=chat,
                from_user=User(id=1, is_bot=True, first_name="TestBot", username="test_bot"),
                text=getattr(method, "text", None),
                reply_markup=(
                getattr(method, "reply_markup", None)
                if isinstance(getattr(method, "reply_markup", None), InlineKeyboardMarkup)
                else None
            ),
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
                reply_markup=(
                getattr(method, "reply_markup", None)
                if isinstance(getattr(method, "reply_markup", None), InlineKeyboardMarkup)
                else None
            ),
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

    async def _auto_slot(self, user: User = USER, chat_id: int = CHAT_ID) -> bool:
        """Если бот спросил интервал доставки — клиент выбирает первый.

        Один и тот же интервал нажимаем один раз: кнопки остаются в истории
        сообщений, а нам нужен только свежий вопрос.
        """
        pressed = getattr(self, "_pressed_slots", None)
        if pressed is None:
            pressed = self._pressed_slots = set()
        for message in reversed(self.session.sent):
            if message.chat.id != chat_id or message.reply_markup is None:
                continue
            for row in message.reply_markup.inline_keyboard:
                for button in row:
                    data = button.callback_data or ""
                    if not data.startswith("slot:") or data in pressed:
                        continue
                    pressed.add(data)
                    await self.callback(data, message, user=user)
                    return True
        return False

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
        await self._auto_slot(user, chat_id)

    async def voice(self, duration: int = 3, user: User = USER, chat_id: int = CHAT_ID) -> None:
        self.update_id += 1
        message = Message(
            message_id=self.update_id,
            date=datetime.now(),
            chat=Chat(id=chat_id, type="private"),
            from_user=user,
            voice=Voice(file_id="voice-1", file_unique_id="uniq-1", duration=duration, file_size=1000),
        ).as_(self.bot)
        await self.dp.feed_update(self.bot, Update(update_id=self.update_id, message=message))
        await self._auto_slot(user, chat_id)
 
    async def callback(
        self, data: str, message: Message, user: User = USER, chat_id: int | None = None
    ) -> None:
        self.update_id += 1
        query = CallbackQuery(
            id=str(self.update_id),
            from_user=user,
            chat_instance="test",
            message=message,
            data=data,
        )
        await self.dp.feed_update(self.bot, Update(update_id=self.update_id, callback_query=query))
        if query.data and not query.data.startswith("slot:"):
            await self._auto_slot(user, query.message.chat.id if query.message else chat_id)

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

    def last_to(self, chat_id: int) -> Message | None:
        for message in reversed(self.session.sent):
            if message.chat.id == chat_id:
                return message
        return None


class FakeTranscriber:
    def __init__(self, text: str) -> None:
        self.text = text

    async def transcribe(self, path):
        return SimpleNamespace(text=self.text)


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
        transcriber = FakeTranscriber("пять кг куриного филе")
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
        print("СКВОЗНОЙ ТЕСТ (одна позиция прайса, fake Telegram)")
        print(f"Прайс: {len(catalog.items)} позиция · БД: {'PostgreSQL' if test_url else 'SQLite'}")
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
            "спрашивает телефон",
            any("Телефон для доставщика" in text for text in harness.texts(CHAT_ID)),
            str(harness.texts(CHAT_ID))[:200],
        )
        session.sent.clear()
        await harness.text("+993 12 345678")
        check(
            "спрашивает адрес",
            any("Адрес доставки" in text for text in harness.texts(CHAT_ID)),
            str(harness.texts(CHAT_ID))[:200],
        )
        session.sent.clear()
        await harness.text("Ашхабад, ул. Тестовая, 1")
        check(
            "новый клиент ждёт подтверждения",
            any("Ждём подтверждения" in text for text in harness.texts(CHAT_ID)),
            str(harness.texts(CHAT_ID))[:200],
        )
        acc_data, acc_msg = harness.find_button("acc:ok:", MANAGER_ID)
        check("менеджер получил заявку", acc_data is not None)
        assert acc_data and acc_msg
        session.sent.clear()
        await harness.callback(acc_data, acc_msg, user=MANAGER)
        check(
            "клиенту сообщили о допуске",
            any("Доступ подтверждён" in text for text in harness.texts(CHAT_ID)),
            str(harness.texts(CHAT_ID))[:200],
        )
        saved_user = await db.get_user(CHAT_ID)
        check("доступ сохранён", saved_user["access"] == "approved")
        check("заведение сохранено", saved_user["venue"] == "Тестовая кухня")
        check("телефон сохранён", saved_user["phone"] == "+993 12 345678")
        check("адрес сохранён", "Ашхабад" in (saved_user["address"] or ""))

        session.sent.clear()
        await harness.text("/start")
        check("клиентское приветствие", any("Напишите заказ" in text for text in harness.texts()))
        check("клиентское меню", harness.find_button("cl:price")[0] is not None)
        session.sent.clear()
        await harness.text("/start", user=MANAGER, chat_id=MANAGER_ID)
        panel_text = "\n".join(harness.texts(MANAGER_ID))
        check("панель менеджера", "Панель менеджера" in panel_text, panel_text[:200])
        check("кнопка «Входящие»", harness.find_button("mp:inbox:0", MANAGER_ID)[0] is not None)
        session.sent.clear()
        await harness.text("куриное филе 2 кг", user=MANAGER, chat_id=MANAGER_ID)
        check(
            "менеджер не оформляет заказ",
            any("Вы менеджер" in text for text in harness.texts(MANAGER_ID)),
            str(harness.texts(MANAGER_ID))[:200],
        )

        print("\n[2] Текстовый заказ → сразу менеджеру")
        session.sent.clear()
        await harness.text("2 кг куриного филе, завтра к десяти")
        summary = "\n".join(harness.texts(CHAT_ID))
        check("итог $10.50", "$10.50" in summary, summary[:300])
        check("одна позиция", summary.count(" = ") == 1, str(summary.count(" = ")))
        orders = await db.list_orders(10)
        check("заказ сохранён", len(orders) == 1, str(orders))
        check("сумма заказа $10.50", orders[0]["total"] == 10.5, str(orders[0]["total"]))
        check(
            "менеджер получил заказ",
            any("Новый заказ" in text for text in harness.texts(MANAGER_ID)),
            str(harness.texts(MANAGER_ID))[:300],
        )

        print("\n[3] Заказ без количества → кнопки")
        session.sent.clear()
        await harness.text("куриное филе 2")
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
        orders = await db.list_orders(10)
        check("заказ 5 кг = $26.25", orders[0]["total"] == 26.25, str(orders[0]["total"]))
        qty_order = await db.get_order(orders[0]["id"])
        check("вопрос количества учтён", qty_order["questions"] == 1, str(qty_order["questions"]))

        print("\n[4] Фасовка: «5 штук» → кнопки и запоминание")
        session.sent.clear()
        await harness.text("куриное филе 5 штук")
        unit_text = "\n".join(harness.texts(CHAT_ID))
        check("вопрос о фасовке", "продаётся" in unit_text, unit_text[:300])
        unit_data, unit_msg = harness.find_button("unit:", CHAT_ID)
        check("кнопки фасовки", unit_data is not None)
        assert unit_data and unit_msg
        before = await db.count_orders()
        await harness.callback(unit_data, unit_msg)
        check("заказ после выбора фасовки", await db.count_orders() == before + 1)
        latest = await db.get_order((await db.list_orders(1))[0]["id"])
        check("фасовка 1 кг", latest["items"][0]["qty"] == 1 and latest["items"][0]["unit"] == "кг",
              str(latest["items"][0]))
        check(
            "выбор запомнен для повара",
            await db.get_chat_state(CHAT_ID, "unit_habit:4") in ("pack1", "pack2", "exact"),
        )
        session.sent.clear()
        await harness.text("куриное филе 3 штуки")
        check("привычная фасовка без вопроса", harness.find_button("unit:", CHAT_ID)[0] is None)
        latest = (await db.list_orders(1))[0]
        check("заказ по привычке $5.25", latest["total"] == 5.25, str(latest["total"]))

        print("\n[5] Правка заказа текстом")
        session.sent.clear()
        await harness.text("куриное филе 2 кг")
        target = (await db.list_orders(1))[0]
        session.sent.clear()
        await harness.text("добавь филе 1 кг")
        preview = "\n".join(harness.texts(CHAT_ID))
        check("показано изменение", "будет так" in preview, preview[:300])
        edit_data, edit_msg = harness.find_button("editok:yes", CHAT_ID)
        check("кнопка «Да»", edit_data is not None)
        assert edit_data and edit_msg
        await harness.callback(edit_data, edit_msg)
        updated = await db.get_order(target["id"])
        check("заказ изменён", updated["total"] == 15.75, str(updated["total"]))
        check("менеджеру пометка «изменён»", any("изменён" in text.lower() for text in harness.texts(MANAGER_ID)))
        session.sent.clear()
        await harness.text("убери филе")
        check("показано удаление", "будет так" in "\n".join(harness.texts(CHAT_ID)))
        edit_data, edit_msg = harness.find_button("editok:yes", CHAT_ID)
        assert edit_data and edit_msg
        await harness.callback(edit_data, edit_msg)
        updated = await db.get_order(target["id"])
        check("позиция убрана", updated["total"] == 5.25, str(updated["total"]))

        print("\n[6] Повторить и отменить")
        session.sent.clear()
        await harness.text(f"/order {target['id']}")
        repeat_data, repeat_msg = harness.find_button("repeat:", CHAT_ID)
        check("кнопка «Повторить»", repeat_data is not None)
        assert repeat_data and repeat_msg
        before_repeat = await db.count_orders()
        await harness.callback(repeat_data, repeat_msg)
        check("повтор создал заказ", await db.count_orders() == before_repeat + 1)
        repeated = await db.get_order((await db.list_orders(1))[0]["id"])
        tomorrow = (datetime.now(settings.timezone).date() + timedelta(days=1)).isoformat()
        check("доставка завтра", repeated["delivery_date"] == tomorrow, str(repeated["delivery_date"]))
        check("позиции повторились", len(repeated["items"]) == 1 and repeated["total"] == 5.25)

        cancel_data, cancel_msg = harness.find_button("ccancel:", CHAT_ID)
        check("кнопка «Отменить»", cancel_data is not None)
        assert cancel_data and cancel_msg
        session.calls.clear()
        await harness.callback(cancel_data, cancel_msg)
        cancelled = await db.get_order(repeated["id"])
        check("заказ отменён", cancelled["status"] == "cancelled", str(cancelled["status"]))
        check(
            "менеджер уведомлён об отмене",
            any(f"Повар отменил заказ №{repeated['id']}" in text for text in harness.texts(MANAGER_ID)),
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
            "повару короткое сообщение",
            any(text.strip() == f"Заказ №{repeated['id']} отменён" for text in harness.texts(CHAT_ID)),
            str(harness.texts(CHAT_ID))[-300:],
        )
        await db.update_order_status(target["id"], "accepted")
        session.calls.clear()
        await harness.callback(f"ccancel:{target['id']}", repeat_msg)
        answers = [call for call in session.calls if type(call).__name__ == "AnswerCallbackQuery"]
        check(
            "заказ в работе не отменяется",
            any("уже в работе" in (getattr(call, "text", "") or "") for call in answers),
            str([getattr(call, "text", "") for call in answers]),
        )

        print("\n[7] Голос с подставным текстом")
        session.sent.clear()
        before_voice = await db.count_orders()
        await harness.voice()
        check("голосовой заказ создан", await db.count_orders() == before_voice + 1)
        voice_order = await db.get_order((await db.list_orders(1))[0]["id"])
        check("голос: 5 кг филе = $26.25", voice_order["total"] == 26.25, str(voice_order["total"]))
        check("источник voice", voice_order["source"] == "voice", str(voice_order["source"]))

        print("\n[8] Сбой разбора: текст не теряется, кнопка повтора")
        original_create = db.create_draft

        async def broken_create(payload):
            raise RuntimeError("simulated db failure")

        db.create_draft = broken_create
        session.sent.clear()
        before_retry = await db.count_orders()
        await harness.text("куриное филе 1 кг")
        check(
            "текст сохранён для повтора",
            "Попробовать ещё раз" in "\n".join(harness.texts(CHAT_ID)),
            str(harness.texts(CHAT_ID))[-300:],
        )
        retry_data, retry_msg = harness.find_button("retry:order", CHAT_ID)
        check("кнопка «Попробовать ещё раз»", retry_data is not None)
        db.create_draft = original_create
        assert retry_data and retry_msg
        await harness.callback(retry_data, retry_msg)
        check("повтор создал заказ", await db.count_orders() == before_retry + 1)

        print("\n[9] /missing и остатки")
        session.sent.clear()
        before_missing = await db.count_orders()
        await harness.text("трюфель 1 кг")
        none_data, none_msg = harness.find_button("none:", CHAT_ID)
        check("кнопка «Нет в прайсе»", none_data is not None)
        assert none_data and none_msg
        await harness.callback(none_data, none_msg)
        check("заказ с неизвестной позицией сохранён", await db.count_orders() == before_missing + 1)
        session.sent.clear()
        await harness.text("/missing", user=MANAGER, chat_id=MANAGER_ID)
        missing_text = "\n".join(harness.texts(MANAGER_ID))
        check("менеджеру показана фраза", "трюфель" in missing_text, missing_text[:300])

        session.sent.clear()
        await harness.text("/stop филе", user=MANAGER, chat_id=MANAGER_ID)
        check("товар остановлен", any("нет в наличии" in t for t in harness.texts(MANAGER_ID)))
        session.sent.clear()
        before_stock = await db.count_orders()
        await harness.text("куриное филе 2 кг")
        stock_text = "\n".join(harness.texts(CHAT_ID))
        check("бот предупредил об остатке", "сегодня нет" in stock_text, stock_text[:300])
        skip_data, skip_msg = harness.find_button("skip:", CHAT_ID)
        check("кнопка убрать позицию", skip_data is not None)
        assert skip_data and skip_msg
        await harness.callback(skip_data, skip_msg)
        check("заказ без позиций не создан", await db.count_orders() == before_stock)
        session.sent.clear()
        await harness.text("/start_item филе", user=MANAGER, chat_id=MANAGER_ID)
        check("товар возвращён", any("снова в наличии" in t for t in harness.texts(MANAGER_ID)))
        session.sent.clear()
        await harness.text("куриное филе 2 кг")
        check("после возврата заказ создан", await db.count_orders() == before_stock + 1)

        print("\n[10] Отсечка и выходные")
        session.sent.clear()
        await harness.text("/cutoff 00:00", user=MANAGER, chat_id=MANAGER_ID)
        check("отсечка задана", any("Отсечка" in t for t in harness.texts(MANAGER_ID)))
        session.sent.clear()
        await harness.text("куриное филе 1 кг, завтра")
        shifted = await db.get_order((await db.list_orders(1))[0]["id"])
        check(
            "после отсечки — следующий день",
            shifted["delivery_date"] == (datetime.now(settings.timezone).date() + timedelta(days=2)).isoformat(),
            str(shifted["delivery_date"]),
        )
        shift_data, shift_msg = harness.find_button("shift:yes", CHAT_ID)
        check("кнопка подтверждения даты", shift_data is not None)
        if shift_data and shift_msg:
            await harness.callback(shift_data, shift_msg)
        await harness.text("/cutoff off", user=MANAGER, chat_id=MANAGER_ID)

        tomorrow = datetime.now(settings.timezone).date() + timedelta(days=1)
        session.sent.clear()
        await harness.text(f"/weekend {tomorrow.weekday()}", user=MANAGER, chat_id=MANAGER_ID)
        check("выходной задан", any("Выходные дни" in t for t in harness.texts(MANAGER_ID)))
        session.sent.clear()
        await harness.text("куриное филе 1 кг, завтра")
        weekend_order = await db.get_order((await db.list_orders(1))[0]["id"])
        check("выходной сдвигает дату", weekend_order["delivery_date"] != tomorrow.isoformat())
        await harness.text("/weekend off", user=MANAGER, chat_id=MANAGER_ID)

        print("\n[11] Напоминание: «Не напоминать»")
        msg = harness.last_to(CHAT_ID)
        assert msg is not None
        await harness.callback("rday:off:0", msg, user=USER)
        check("напоминания отключены", await db.get_chat_state(CHAT_ID, "remind_off") == "1")

        print("\n[12] /quality, /health, /export")
        session.sent.clear()
        await harness.text("/quality", user=MANAGER, chat_id=MANAGER_ID)
        check("отчёт качества", any("Качество" in t for t in harness.texts(MANAGER_ID)))
        session.sent.clear()
        await harness.text("/health", user=MANAGER, chat_id=MANAGER_ID)
        health_text = "\n".join(harness.texts(MANAGER_ID))
        check("отчёт здоровья", "Здоровье" in health_text, health_text[:300])
        check("время ответа БД", "ответ" in health_text and "мс" in health_text, health_text[:300])
        session.sent.clear()
        await harness.text("/health", user=USER, chat_id=CHAT_ID)
        check("повару /health запрещена", any("только менеджеру" in t for t in harness.texts(CHAT_ID)))
        session.sent.clear()
        await harness.text("/export 7", user=MANAGER, chat_id=MANAGER_ID)
        check(
            "Excel отправлен",
            any(type(call).__name__ == "SendDocument" for call in session.calls),
            str([type(call).__name__ for call in session.calls]) + " | " + str(harness.texts(MANAGER_ID)[-2:]),
        )

        print("\n[13] «Как обычно»")
        session.sent.clear()
        await harness.text("/start")
        usual_data, usual_msg = harness.find_button("usual:", CHAT_ID)
        check("кнопка «как обычно»", usual_data is not None)
        assert usual_data and usual_msg
        before_usual = await db.count_orders()
        await harness.callback(usual_data, usual_msg)
        check("заказ «как обычно» создан", await db.count_orders() == before_usual + 1)
        usual_order = await db.get_order((await db.list_orders(1))[0]["id"])
        check("одна позиция", len(usual_order["items"]) == 1)
        check("без вопросов", usual_order["questions"] == 0, str(usual_order["questions"]))

        print("\\n[14] Прайс из 100 позиций")
        check("в прайсе 100 товаров", len(catalog.items) == 100, str(len(catalog.items)))
        check(
            "Куриное филе ID 4 на месте",
            catalog.get(4) is not None and catalog.get(4).name == "Куриное филе",
        )
        check(
            "старый алиас на отсутствующий id не ломает прайс",
            await db.save_alias("трюфель", 999) is None,
        )
        session.sent.clear()
        await harness.text("икра осетровая 1 кг", user=USER, chat_id=CHAT_ID)
        check(
            "товара нет в прайсе — заказ не создан молча",
            harness.find_button("none:", CHAT_ID)[0] is not None
            or "Не нашёл" in "\n".join(harness.texts(CHAT_ID)),
            str(harness.texts(CHAT_ID))[-200:],
        )

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


async def _send_text(harness: Harness, text: str, user: User, chat_id: int) -> bool:
    await harness.text(text, user=user, chat_id=chat_id)
    return True


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))