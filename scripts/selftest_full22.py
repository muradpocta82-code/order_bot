"""Самопроверка ядра бота: прайс, разбор, суммы, БД, форматирование.

Запуск:  venv/bin/python scripts/selftest.py
Не требует Telegram-токена и AI-ключей.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from orderbot.ai import _is_quota_error, _retry_delay_seconds  # noqa: E402
from orderbot.backup import backup_database  # noqa: E402
from orderbot.catalog import Catalog, diff_catalogs  # noqa: E402
from orderbot.db import Database, reset_test_database  # noqa: E402
from orderbot.drafts import (  # noqa: E402
    all_resolved,
    build_draft,
    draft_total,
    order_items_payload,
)
from orderbot.formatting import format_draft  # noqa: E402
from orderbot.migrations import LATEST_VERSION  # noqa: E402
from orderbot.nlu import detect_intent, parse_local, parse_number  # noqa: E402

TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=TZ)
CATALOG = Catalog.load(BASE_DIR / "catalog.xlsx")
TEST_URL = os.getenv("TEST_DATABASE_URL", "").strip()

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


def draft_for(text: str, aliases: dict[str, int] | None = None):
    parsed = parse_local(text, TZ, now=NOW)
    return parsed, build_draft(
        parsed, CATALOG, 1, 1, "Тест", None, "text", None,
        accept_threshold=88, candidate_threshold=55, aliases=aliases,
    )


def test_core() -> None:
    print("\n[1] Ключевой сценарий из ТЗ")
    parsed, draft = draft_for(
        "бекона коробку, моцареллы два пакета, сметану двадцатку пять штук, завтра к десяти"
    )
    check("3 позиции", len(draft["items"]) == 3, str(len(draft["items"])))
    check("дата завтра", parsed.delivery_date == date(2026, 11, 4), str(parsed.delivery_date))
    check("время 10:00", parsed.delivery_time == "10:00", str(parsed.delivery_time))
    ids = [item["catalog_id"] for item in draft["items"]]
    check("сопоставление 1/2/3", ids == [1, 2, 3], str(ids))
    qty = [item["final_qty"] for item in draft["items"]]
    check("количества 1/2/5", qty == [1, 2, 5], str(qty))
    units = [item["final_unit"] for item in draft["items"]]
    check("единицы коробка/пакет/ведро", units == ["коробка", "пакет", "ведро"], str(units))
    check("итого 14 150 ₽", draft_total(draft) == 14150, str(draft_total(draft)))
    check("всё сопоставлено", all_resolved(draft))

    print("\n[2] Разные формулировки")
    _, d2 = draft_for("2 кг куриного филе и 5 бутылок молока послезавтра")
    check("филе + молоко", [i["catalog_id"] for i in d2["items"]] == [4, 9])
    check("молоко пересчитано в литры", d2["items"][1]["final_qty"] == 5 and d2["items"][1]["final_unit"] == "л")
    check("сумма филе+молоко", draft_total(d2) == 1315, str(draft_total(d2)))
    check("послезавтра", parse_local("послезавтра", TZ, now=NOW).delivery_date == date(2026, 11, 5))

    _, d3 = draft_for("нужна моцарелла 3 уп, пармезан 1 кг, масло сливочное 2 кг")
    check("3 позиции по синонимам", [i["catalog_id"] for i in d3["items"]] == [2, 7, 10])
    check("упаковка→пакет", d3["items"][0]["final_unit"] == "пакет")
    check("сумма 7 960 ₽", draft_total(d3) == 7960, str(draft_total(d3)))

    _, d4 = draft_for("сметану двадцатку пять штук")
    check("сметана 20% по слову «двадцатка»", d4["items"][0]["catalog_id"] == 3)
    check("5 штук→5 ведер", d4["items"][0]["final_qty"] == 5 and d4["items"][0]["final_unit"] == "ведро")

    _, d5 = draft_for("5 упаковок моцареллы")
    check("5 упаковок моцареллы", d5["items"][0]["catalog_id"] == 2 and d5["items"][0]["final_qty"] == 5)

    _, d6 = draft_for("двадцать пять штук моцареллы")
    check("составное числительное 25", d6["items"][0]["qty"] == 25, str(d6["items"][0]["qty"]))

    _, d7 = draft_for("полтора кг куриного филе")
    check("полтора кг", d7["items"][0]["final_qty"] == 1.5 and draft_total(d7) == 630)

    print("\n[3] Неоднозначности")
    _, d8 = draft_for("масло 2 кг")
    check("масло требует выбора", d8["items"][0]["status"] == "needs_choice")
    check("варианты масла 10/17", {10, 17}.issubset(set(d8["items"][0]["candidates"])), str(d8["items"][0]["candidates"]))
    check("подтверждение заблокировано", not all_resolved(d8))

    _, d9 = draft_for("сыр 1 кг")
    check("сыр требует выбора", d9["items"][0]["status"] == "needs_choice")
    check("варианты сыра 6/7", {6, 7}.issubset(set(d9["items"][0]["candidates"])), str(d9["items"][0]["candidates"]))

    _, d8_alias = draft_for("масло 2 кг", {"масло": 10})
    check(
        "alias «масло» → сливочное",
        d8_alias["items"][0]["catalog_id"] == 10
        and d8_alias["items"][0]["status"] == "ok",
        str(d8_alias["items"][0]),
    )
    _, d9_alias = draft_for("сыр 1 кг", {"сыр": 7})
    check(
        "alias «сыр» → пармезан",
        d9_alias["items"][0]["catalog_id"] == 7
        and d9_alias["items"][0]["status"] == "ok",
        str(d9_alias["items"][0]),
    )

    _, d_full = draft_for("сыр пармезан 1 кг, масло растительное 1 канистра")
    check(
        "полное название «сыр пармезан» без выбора",
        d_full["items"][0]["catalog_id"] == 7 and d_full["items"][0]["status"] == "ok",
        str(d_full["items"][0]),
    )
    check(
        "полное название «масло растительное» без выбора",
        d_full["items"][1]["catalog_id"] == 17 and d_full["items"][1]["status"] == "ok",
        str(d_full["items"][1]),
    )

    _, d10 = draft_for("бекон 2")
    check("бекон без единицы → вопрос о количестве", d10["items"][0]["status"] == "needs_qty")
    check("бекон сопоставлен", d10["items"][0]["catalog_id"] == 1)

    _, d11 = draft_for("2 кг бекона 3 кг курицы")
    check("бекон в кг несовместим с коробкой", d11["items"][0]["unit_warning"] is True)
    check("курица 3 кг посчитана", d11["items"][1]["sum"] == 1260)

    print("\n[4] Даты, время, числа")
    parsed = parse_local("4 ноября к 10:30", TZ, now=NOW)
    check("дата 04.11", parsed.delivery_date == date(2026, 11, 4), str(parsed.delivery_date))
    check("время 10:30", parsed.delivery_time == "10:30", str(parsed.delivery_time))
    parsed = parse_local("на пятницу к 9:00", TZ, now=NOW)
    check("пятница", parsed.delivery_date == date(2026, 11, 6), str(parsed.delivery_date))
    parsed = parse_local("завтра к десяти утра", TZ, now=NOW)
    check("к десяти", parsed.delivery_time == "10:00", str(parsed.delivery_time))
    check("parse_number 7", parse_number("7") == 7)
    check("parse_number семь", parse_number("семь") == 7)
    check("parse_number 1,5", parse_number("1,5") == 1.5)

    print("\n[4b] Не-заказы")
    check("приветствие", detect_intent("Привет!") == "greeting")
    check("благодарность", detect_intent("спасибо") == "thanks")
    check("статус заказа", detect_intent("где мой заказ?") == "status")
    check("отмена", detect_intent("отмените") == "cancel")
    check("прайс", detect_intent("покажи прайс") == "price")
    check("помощь", detect_intent("помощь") == "help")
    check("заказ не считается приветствием", detect_intent("привет, 2 кг филе") is None)

    print("\n[4c] Хаос")
    with tempfile.TemporaryDirectory() as tmp:
        broken = Path(tmp) / "broken.xlsx"
        broken.write_text("это не таблица", encoding="utf-8")
        try:
            Catalog.load(broken)
            rejected = False
        except Exception:
            rejected = True
        check("битый прайс отклонён", rejected)
    check("429 распознаётся", _is_quota_error(Exception("429 RESOURCE_EXHAUSTED")))
    check(
        "пауза после 429 не меньше минуты",
        _retry_delay_seconds(Exception("retryDelay: 30s")) >= 60,
    )

    print("\n[4d] Обновление прайса")
    changed = [
        replace(item, price=item.price + 10) if item.id == 4 else item
        for item in CATALOG.items
    ]
    new_catalog = Catalog()
    for item in changed:
        new_catalog.add(item)
    diff = diff_catalogs(CATALOG, new_catalog)
    check(
        "дифф: изменилась одна цена",
        len(diff["price_changes"]) == 1 and diff["price_changes"][0][0].id == 4,
        str(diff["price_changes"]),
    )
    check(
        "дифф: новых и убранных нет",
        not diff["added"] and not diff["removed"],
    )

    print("\n[5] Форматирование")
    text = format_draft(draft)
    check("итог в сообщении", "14 150 ₽" in text)
    check("дата в сообщении", "04.11.2026 к 10:00" in text)
    safe = dict(draft)
    safe["user_name"] = "<script>alert(1)</script>"
    check("HTML экранируется", "<script>" not in format_draft(safe))


async def test_db() -> None:
    print("\n[6] База данных")
    with tempfile.TemporaryDirectory() as tmp:
        db = (
            Database(Path(tmp) / "unused.db", TEST_URL)
            if TEST_URL
            else Database(Path(tmp) / "test.db")
        )
        await db.connect()
        if TEST_URL:
            await reset_test_database(db)
        await db.init()
        parsed, draft = draft_for("бекона коробку, моцареллы два пакета, завтра к 10:00")
        await db.create_draft(draft)
        loaded = await db.get_draft(draft["id"])
        check("черновик сохраняется", loaded is not None and len(loaded["items"]) == 2)
        loaded["summary_sent"] = True
        loaded["questions"] = 2
        await db.save_draft(loaded)
        check("черновик обновляется", (await db.get_draft(draft["id"]))["summary_sent"] is True)

        order_id = await db.create_order(loaded, order_items_payload(loaded), None)
        order = await db.get_order(order_id)
        check("заказ создан", order is not None and order["id"] == order_id)
        check("позиции заказа", len(order["items"]) == 2, str(len(order["items"])))
        check("сумма заказа", order["total"] == 10700, str(order["total"]))
        check("вопросы заказа сохранены", order["questions"] == 2, str(order["questions"]))
        check("список заказов", len(await db.list_orders(5)) == 1)
        check("всего заказов", await db.count_orders() == 1)
        check("постраничный список", len(await db.list_orders_page(limit=10, offset=0)) == 1)
        await db.update_order_status(order_id, "accepted")
        check("статус обновлён", (await db.get_order(order_id))["status"] == "accepted")
        check(
            "повторный статус не меняет заказ",
            await db.update_order_status(order_id, "accepted") is False,
        )
        check("входящие (new+accepted)", await db.count_orders(("new", "accepted")) == 1)
        await db.set_setting("test_key", "123")
        check("настройки", await db.get_setting("test_key") == "123")
        await db.save_alias("масло", 10)
        await db.save_alias("масло", 17)
        check("alias сохраняется и обновляется", await db.get_aliases() == {"масло": 17})

        # личные алиасы поваров не мешают друг другу
        await db.save_alias("сыр", 7, chef_id=501)
        await db.save_alias("сыр", 6, chef_id=502)
        chef1_aliases = await db.get_aliases(501)
        chef2_aliases = await db.get_aliases(502)
        check("alias повара 1: сыр → пармезан", chef1_aliases.get("сыр") == 7, str(chef1_aliases))
        check("alias повара 2: сыр → гауда", chef2_aliases.get("сыр") == 6, str(chef2_aliases))
        check(
            "общий alias не появился от двух поваров",
            (await db.get_aliases()).get("сыр") is None,
        )
        _, chef1_draft = draft_for("сыр 1 кг", chef1_aliases)
        _, chef2_draft = draft_for("сыр 1 кг", chef2_aliases)
        check("повар 1 разбирает сыр как пармезан", chef1_draft["items"][0]["catalog_id"] == 7)
        check("повар 2 разбирает сыр как гауду", chef2_draft["items"][0]["catalog_id"] == 6)

        # три разных повара подтверждают одно и то же → общий alias
        await db.save_alias("бекон", 1, chef_id=601)
        await db.save_alias("бекон", 1, chef_id=602)
        check("двух поваров мало для общего", (await db.get_aliases()).get("бекон") is None)
        await db.save_alias("бекон", 1, chef_id=603)
        check("три повара создают общий alias", (await db.get_aliases()).get("бекон") == 1)
        for _ in range(3):
            await db.save_alias("мука", 15, chef_id=604)
        check("три раза один повар — не общий", (await db.get_aliases()).get("мука") is None)

        await db.record_missing("трюфель")
        await db.record_missing("трюфель")
        await db.record_missing("икра")
        missing = await db.list_missing()
        check(
            "missing_items считает повторы",
            missing[0]["phrase"] == "трюфель" and missing[0]["count"] == 2,
            str(missing),
        )
        await db.conn.execute(
            "INSERT INTO missing_items(phrase, count, last_seen) "
            "VALUES('старое', 5, '2020-01-01 00:00:00')"
        )
        await db.conn.commit()
        check(
            "missing_items не показывает старое",
            "старое" not in {row["phrase"] for row in await db.list_missing()},
            str(await db.list_missing()),
        )

        await db.log_question("масло", "choice")
        await db.log_question("масло", "qty")
        await db.log_question("сыр", "choice")
        stats = await db.quality_stats()
        check("quality: заказов", stats["orders"] == 1, str(stats))
        check("quality: без вопросов", stats["clean"] == 0, str(stats))
        check("quality: среднее", abs(stats["avg"] - 2) < 0.01, str(stats))
        check(
            "quality: топ фраз",
            stats["top"][0]["phrase"] == "масло" and stats["top"][0]["c"] == 2,
            str(stats),
        )

        # Telegram id больше int4 (8841849268) — регрессия 22003 в PostgreSQL.
        big_id = 8841849268
        await db.save_alias("томатный", 5, chef_id=big_id)
        check(
            "alias с большим Telegram id",
            (await db.get_aliases(big_id)).get("томатный") == 5,
        )
        _, big_draft = draft_for("кетчуп томатный 2 шт")
        big_draft["chat_id"] = big_id
        big_draft["user_id"] = big_id
        await db.create_draft(big_draft)
        big_order = await db.create_order(
            big_draft, order_items_payload(big_draft), None
        )
        check(
            "заказ с большим Telegram id",
            (await db.get_order(big_order))["chef_id"] == big_id,
        )
        if db.is_postgres:
            cursor = await db.conn.execute(
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name IN "
                "('chef_id', 'user_id', 'chat_id', 'manager_chat_id')"
            )
            wrong = [
                (row["table_name"], row["column_name"], row["data_type"])
                for row in await cursor.fetchall()
                if row["data_type"] != "bigint"
            ]
            check("все Telegram id в схеме — BIGINT", not wrong, str(wrong))
            cursor = await db.conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'orders'"
            )
            columns = {row["column_name"] for row in await cursor.fetchall()}
            check(
                "колонки карточек заказа",
                {"chef_card_message_id", "manager_card_message_id"} <= columns,
                str(sorted(columns)),
            )

        usual = await db.usual_items(1)
        check("usual: две позиции", len(usual) == 2, str(usual))
        check(
            "usual: бекон 1 коробка",
            usual[0]["catalog_id"] == 1
            and usual[0]["qty"] == 1
            and usual[0]["unit"] == "коробка",
            str(usual),
        )
        usual_one = await db.usual_for_item(1, 1)
        check(
            "usual: количество позиции",
            usual_one == {"qty": 1.0, "unit": "коробка"},
            str(usual_one),
        )

        check("заказ за последний час", await db.count_orders_since(1) == 1)
        await db.conn.execute(
            "UPDATE orders SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
            (order_id,),
        )
        await db.conn.commit()
        check(
            "старый неотправленный заказ найден",
            order_id in await db.old_unnotified_orders(15),
        )
        if db.is_postgres:
            print("  — бэкап SQLite пропущен (тест на PostgreSQL)")
        else:
            backup_path = backup_database(db.path, Path(tmp) / "backups", 2)
            check("бэкап БД создан", backup_path.exists() and backup_path.stat().st_size > 0)

        await db.conn.execute(
            "UPDATE drafts SET status = 'cancelled', "
            "updated_at = '2020-01-01 00:00:00' WHERE id = ?",
            (draft["id"],),
        )
        await db.conn.commit()
        purged = await db.purge_closed_drafts(7)
        check(
            "старый черновик удалён",
            purged >= 1 and await db.get_draft(draft["id"]) is None,
        )
        await db.vacuum()
        check("VACUUM не ломает БД", len(await db.list_orders(1)) == 1)
        await db.close()

        db2 = (
            Database(Path(tmp) / "unused.db", TEST_URL)
            if TEST_URL
            else Database(Path(tmp) / "test.db")
        )
        await db2.connect()
        await db2.init()
        check("данные переживают перезапуск", (await db2.get_order(order_id)) is not None)
        check("alias переживает перезапуск", (await db2.get_aliases()).get("масло") == 17)
        await db2.close()


async def test_start_and_greeting() -> None:
    """Регрессия: /start и «привет» у повара и менеджера не должны падать.

    Ловит поломки в цепочке cmd_start -> set_setting/usual_items и в текстах,
    из-за которых бот отвечал «Что-то пошло не так».
    """
    print("\n[6b] /start и приветствие (повар и менеджер)")
    from types import SimpleNamespace

    from orderbot.config import load_settings
    from orderbot.handlers import MANAGER_ONLY_TEXT, START_TEXT, _intent_reply, cmd_start

    class FakeMessage:
        def __init__(self, chat_id: int, user_id: int) -> None:
            self.chat = SimpleNamespace(id=chat_id)
            self.from_user = SimpleNamespace(
                id=user_id, full_name="Тест Повар", username="test_chef", is_bot=False
            )
            self.bot = None
            self.sent: list[tuple[str, object]] = []

        async def answer(self, text, reply_markup=None):
            self.sent.append((text, reply_markup))
            return self

    chef_id = 3001
    manager_id = 9002
    with tempfile.TemporaryDirectory() as tmp:
        settings = replace(
            load_settings(),
            db_path=Path(tmp) / "start.db",
            data_dir=Path(tmp) / "data",
            manager_chat_id=manager_id,
            database_url="",
        )
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db = (
            Database(settings.db_path, TEST_URL)
            if TEST_URL
            else Database(settings.db_path)
        )
        await db.connect()
        await db.init()

        chef_message = FakeMessage(chef_id, chef_id)
        await cmd_start(chef_message, db, settings, CATALOG)
        check("/start повара отвечает", len(chef_message.sent) == 1, str(chef_message.sent))
        text, keyboard = chef_message.sent[0]
        check("/start повара: текст приветствия", text == START_TEXT, text[:120])
        check("/start повара: кнопки меню", keyboard is not None)
        check(
            "/start повара: сбрасывает ожидание количества",
            await db.get_setting(f"qty_input:{chef_id}") == "",
        )

        manager_message = FakeMessage(manager_id, manager_id)
        await cmd_start(manager_message, db, settings, CATALOG)
        panel = "\n".join(item[0] for item in manager_message.sent)
        check("/start менеджера: панель", "Панель менеджера" in panel, panel[:200])
        check("/start менеджера: без клиентского меню", START_TEXT not in panel)

        greeting = await _intent_reply(
            "greeting", chef_id, chef_id, db, settings, CATALOG
        )
        check("привет повара: ответ есть", greeting is not None)
        check(
            "привет повара: текст приветствия",
            greeting is not None and greeting[0] == START_TEXT,
            str(greeting)[:120],
        )
        check(
            "привет повара: кнопки меню",
            greeting is not None and greeting[1] is not None,
        )
        # У менеджера приветствие перехватывается раньше и даёт панель.
        check("текст для менеджера задан", bool(MANAGER_ONLY_TEXT))

        await db.close()


async def test_failure_safety() -> None:
    """Сбой обработки не теряет текст заказа, тревога менеджеру — с лимитом."""
    print("\n[6c] Сбой не теряет заказ")
    from orderbot.config import load_settings
    from orderbot.handlers import _remember_retry, _report_failure, _take_retry

    class FakeBot:
        def __init__(self) -> None:
            self.sent: list[tuple[int, str]] = []

        async def send_message(self, chat_id, text, **kwargs):
            self.sent.append((chat_id, text))

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "retry.db", TEST_URL) if TEST_URL else Database(Path(tmp) / "retry.db")
        await db.connect()
        await db.init()
        await _remember_retry(db, 555, "сыр пармезан 2 кг", "text", None)
        data = await _take_retry(db, 555)
        check(
            "текст заказа сохранён для повтора",
            bool(data) and data["text"] == "сыр пармезан 2 кг",
            str(data),
        )
        check("повтор одноразовый", await _take_retry(db, 555) is None)

        settings = replace(load_settings(), manager_chat_id=777, database_url="")
        bot = FakeBot()
        error = ValueError("boom")
        for _ in range(3):
            await _report_failure(bot, db, settings, "order", error)
        check(
            "тревога не чаще раза в 10 минут",
            len(bot.sent) == 1,
            str(bot.sent),
        )
        if bot.sent:
            check(
                "в тревоге handler и тип ошибки",
                "order" in bot.sent[0][1] and "ValueError" in bot.sent[0][1],
                bot.sent[0][1][:120],
            )
        await db.close()


async def test_alias_migration() -> None:
    print("\n[7] Миграция старых алиасов")
    if TEST_URL:
        print("  — пропущено: проверка старой SQLite-базы")
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE aliases (phrase TEXT PRIMARY KEY, catalog_id INTEGER NOT NULL, "
            "hits INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);"
            "INSERT INTO aliases VALUES('масло', 10, 2, '2026-01-01 00:00:00');"
        )
        conn.commit()
        conn.close()
        db = Database(path)
        await db.connect()
        await db.init()
        check("старый alias стал общим", (await db.get_aliases()).get("масло") == 10)
        check(
            "старый alias виден любому повару",
            (await db.get_aliases(777)).get("масло") == 10,
        )
        await db.close()


async def test_migrations_on_copy() -> None:
    print("\n[8] Миграции на копии боевой базы")
    if TEST_URL:
        print("  — пропущено: копия SQLite-базы")
        return
    live = BASE_DIR / "data" / "orderbot.db"
    if not live.exists():
        print("  — боевой базы нет, проверка пропущена")
        return
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "copy.db"
        source = sqlite3.connect(f"file:{live}?mode=ro", uri=True, timeout=30)
        destination = sqlite3.connect(copy)
        try:
            source.backup(destination)
        finally:
            source.close()
            destination.close()

        db = Database(copy)
        await db.connect()
        await db.init()
        cursor = await db.conn.execute("SELECT MAX(version) AS v FROM schema_version")
        row = await cursor.fetchone()
        check("версия схемы применена", row["v"] == LATEST_VERSION, str(row["v"]))

        cursor = await db.conn.execute("PRAGMA table_info(aliases)")
        columns = {item["name"] for item in await cursor.fetchall()}
        check("алиасы с chef_id", "chef_id" in columns, str(columns))

        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        tables = {item["name"] for item in await cursor.fetchall()}
        check("таблица missing_items", "missing_items" in tables, str(tables))

        await db.init()
        cursor = await db.conn.execute("SELECT COUNT(*) AS c FROM schema_version")
        row = await cursor.fetchone()
        check("миграции не применяются повторно", row["c"] == LATEST_VERSION, str(row["c"]))
        await db.close()


async def test_locked_db() -> None:
    print("\n[9] БД временно заблокирована")
    if TEST_URL:
        print("  — пропущено: блокировка SQLite")
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "locked.db"
        db = Database(path)
        await db.connect()
        await db.init()
        locker = sqlite3.connect(path, timeout=0.2)
        locker.execute("BEGIN EXCLUSIVE")
        task = asyncio.create_task(db.set_setting("after_lock", "ok"))
        await asyncio.sleep(0.2)
        locker.rollback()
        locker.close()
        try:
            await asyncio.wait_for(task, timeout=10)
            survived = True
        except Exception:
            survived = False
        check("запись переживает блокировку", survived)
        check("данные после блокировки", await db.get_setting("after_lock") == "ok")
        await db.close()


async def test_lock_cleanup() -> None:
    print("\n[11] Уборка блокировок")
    from orderbot.handlers import _draft_locks, _order_locks, prune_locks

    _draft_locks["old-draft"] = asyncio.Lock()
    _order_locks[999999] = asyncio.Lock()
    removed = prune_locks()
    check(
        "завершённые блокировки удалены",
        removed >= 2
        and "old-draft" not in _draft_locks
        and 999999 not in _order_locks,
        str(removed),
    )


async def test_allowed_middleware() -> None:
    print("\n[10] ALLOWED_CHAT_IDS")
    from types import SimpleNamespace

    from main import AllowedChatsMiddleware

    middleware = AllowedChatsMiddleware({555}, 777)

    async def handler(event, data):
        return "ok"

    blocked = await middleware(
        handler, SimpleNamespace(chat=SimpleNamespace(id=999)), {}
    )
    allowed = await middleware(
        handler, SimpleNamespace(chat=SimpleNamespace(id=555)), {}
    )
    manager = await middleware(
        handler, SimpleNamespace(chat=SimpleNamespace(id=777)), {}
    )
    check("чужой чат игнорируется", blocked is None, str(blocked))
    check("разрешённый чат проходит", allowed == "ok")
    check("менеджер проходит", manager == "ok")


async def main() -> int:
    print("=" * 70)
    print("Самопроверка Order bot")
    print(f"Прайс: {len(CATALOG.items)} позиций")
    print(f"БД:    {'PostgreSQL (TEST_DATABASE_URL)' if TEST_URL else 'SQLite'}")
    print("=" * 70)
    test_core()
    await test_db()
    await test_start_and_greeting()
    await test_failure_safety()
    await test_alias_migration()
    await test_migrations_on_copy()
    await test_locked_db()
    await test_lock_cleanup()
    await test_allowed_middleware()
    print("\n" + "=" * 70)
    print(f"Пройдено: {passed}   Провалено: {len(failed)}")
    if failed:
        print("Провалы: " + ", ".join(failed))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
