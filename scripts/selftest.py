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
    print("\n[1] Ключевой сценарий: одна позиция прайса")
    parsed, draft = draft_for("2 кг куриного филе, завтра к десяти")
    check("1 позиция", len(draft["items"]) == 1, str(len(draft["items"])))
    check("дата завтра", parsed.delivery_date == date(2026, 11, 4), str(parsed.delivery_date))
    check("время 10:00", parsed.delivery_time == "10:00", str(parsed.delivery_time))
    ids = [item["catalog_id"] for item in draft["items"]]
    check("сопоставление с филе", ids == [4], str(ids))
    check(
        "количество 2 кг",
        draft["items"][0]["final_qty"] == 2 and draft["items"][0]["final_unit"] == "кг",
        str(draft["items"][0]),
    )
    check("итого $10.50", draft_total(draft) == 10.5, str(draft_total(draft)))
    check("всё сопоставлено", all_resolved(draft))

    print("\n[2] Разные формулировки")
    _, d2 = draft_for("полтора кг куриного филе послезавтра")
    check("филе по названию", [i["catalog_id"] for i in d2["items"]] == [4])
    check("полтора кг", d2["items"][0]["final_qty"] == 1.5 and draft_total(d2) == 7.88)
    check("послезавтра", parse_local("послезавтра", TZ, now=NOW).delivery_date == date(2026, 11, 5))

    _, d3 = draft_for("курица 3 кг")
    check("синоним «курица»", d3["items"][0]["catalog_id"] == 4)
    check("сумма $15.75", draft_total(d3) == 15.75, str(draft_total(d3)))

    _, d4 = draft_for("двадцать пять кг куриного филе")
    check("составное числительное 25", d4["items"][0]["qty"] == 25, str(d4["items"][0]["qty"]))

    _, d5 = draft_for("куриное филе 5 штук")
    check("5 штук — не кг, нужен выбор фасовки", d5["items"][0]["unit_warning"] is True)
    check("статус ok до вопроса о фасовке", d5["items"][0]["status"] == "ok")

    _, d6 = draft_for("5 упаковок куриного филе")
    check("5 упаковок — тоже вопрос о фасовке", d6["items"][0]["unit_warning"] is True)

    print("\n[3] Неоднозначности, алиасы и чужие позиции")
    _, d7 = draft_for("трюфель 1 кг")
    check("неизвестная позиция требует решения", d7["items"][0]["status"] == "needs_choice")
    check("подтверждение заблокировано", not all_resolved(d7))

    _, d8 = draft_for("куриное филе 2")
    check("филе без единицы → вопрос о количестве", d8["items"][0]["status"] == "needs_qty")

    _, d9 = draft_for("куриное филе 2 литра")
    check("литры несовместимы с кг", d9["items"][0]["unit_warning"] is True)

    _, d10 = draft_for("мясо 2 кг", {"мясо": 4})
    check(
        "alias «мясо» → филе",
        d10["items"][0]["catalog_id"] == 4 and d10["items"][0]["status"] == "ok",
        str(d10["items"][0]),
    )
    _, d11 = draft_for("мясо 2 кг", {"мясо": 999})
    check(
        "alias на удалённую позицию пропускается",
        d11["items"][0]["catalog_id"] is None,
        str(d11["items"][0]),
    )

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
    check("итог в сообщении", "$10.50" in text, text[:200])
    check("дата в сообщении", "04.11.2026 к 10:00" in text)
    safe = dict(draft)
    safe["user_name"] = "<script>alert(1)</script>"
    check("HTML экранируется", "<script>" not in format_draft(safe))

    print("\n[5b] Правка заказа, сводки и голос")
    from orderbot.extras import (
        correct_voice_text,
        items_summary,
        parse_edit,
        tomorrow_summary,
        weekly_report,
    )

    check("правка: добавить", parse_edit("добавь филе 2 кг") == {"action": "add", "text": "филе 2 кг"})
    check("правка: убрать", (parse_edit("убери филе") or {}).get("action") == "remove")
    check(
        "правка: заменить",
        (parse_edit("вместо филе 2 кг сделай филе 3 кг") or {}).get("action") == "replace",
    )
    check("правка: обычный текст не правка", parse_edit("филе 2 кг завтра") is None)
    check("голос: число словом", correct_voice_text("пять кг филе", CATALOG) == "5 кг филе")
    check(
        "голос: словарь прайса",
        "филе" in correct_voice_text("куриное филе два кг", CATALOG),
    )
    summary_items = items_summary(draft["items"])
    check("сводка позиций", "Куриное филе" in summary_items and "$10.50" in summary_items, summary_items)
    day = tomorrow_summary(
        [
            {
                "id": 1,
                "chef_name": "Тест",
                "total": 10.5,
                "delivery_time": "10:00",
                "items": [{"name": "Куриное филе", "qty": 2, "unit": "кг"}],
            }
        ],
        "04.11.2026",
    )
    check("сводка на завтра", "Сводка на 04.11.2026" in day and "Куриное филе" in day, day[:150])
    report = weekly_report([], {}, ["Кухня"], [], "28.10", "04.11")
    check("недельный отчёт", "Отчёт за неделю" in report and "Кухня" in report)


def test_slots() -> None:
    print("\n[слоты доставки]")
    from datetime import datetime as _dt

    from orderbot.slots import default_time, parse_slots, slot_labels

    check("слоты по умолчанию", slot_labels(parse_slots(None)) == [
        "09:00–12:00", "12:00–15:00", "15:00–18:00",
    ], str(slot_labels(parse_slots(None))))
    check("слоты из строки", slot_labels(parse_slots("9-12, 12-15 ,18:00-21:00")) == [
        "09:00–12:00", "12:00–15:00", "18:00–21:00",
    ])
    check("мусор в настройке не ломает", len(parse_slots("абракадабра")) == 3)
    check("перевёрнутый интервал отброшен", parse_slots("15-12,9-12") == [("09:00", "12:00")],
          str(parse_slots("15-12,9-12")))
    check("подсказка слота днём", default_time(parse_slots(None), _dt(2026, 11, 14, 10, 30)) == "12:00")
    check("подсказка слота вечером", default_time(parse_slots(None), _dt(2026, 11, 14, 19, 0)) == "09:00")


def test_module_names() -> None:
    """Каждый модуль должен быть «самодостаточным»: без забытых импортов.

    Такая проверка ловит ошибки, которые обычные тесты не видят: например,
    вынесли код в новый файл, а импорт добавить забыли — падает только тот
    обработчик, до которого в тестах не дошли.
    """
    print("\n[целостность модулей]")
    import ast
    import builtins
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "orderbot"
    problems: list[str] = []
    for path in sorted(root.glob("*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        defined: set[str] = set()
        annotation_names: set[str] = set()

        def add_annotation(node) -> None:
            if node is None:
                return
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    annotation_names.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    pass

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
                add_annotation(getattr(node, "returns", None))
                for arg in list(getattr(node, "args", None).args if getattr(node, "args", None) else []):
                    add_annotation(arg.annotation)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    defined.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    defined.add(alias.asname or alias.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler):
                if node.name:
                    defined.add(node.name)
            elif isinstance(node, ast.AnnAssign):
                add_annotation(node.annotation)
            elif isinstance(node, ast.Lambda):
                for arg in list(node.args.args):
                    defined.add(arg.arg)

        used = {
            n.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        module_globals = {"__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__"}
        unknown = sorted(
            used - defined - annotation_names - set(dir(builtins)) - module_globals
        )
        if unknown:
            problems.append(f"{path.name}: {', '.join(unknown)}")

    check("нет забытых импортов", not problems, "; ".join(problems))


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
        parsed, draft = draft_for("2 кг куриного филе, завтра к 10:00")
        # Внешний ключ orders.chef_id -> users: клиент должен существовать.
        await db.ensure_user(int(draft["user_id"]), "Тест Повар", None)
        await db.create_draft(draft)
        loaded = await db.get_draft(draft["id"])
        check("черновик сохраняется", loaded is not None and len(loaded["items"]) == 1)
        loaded["summary_sent"] = True
        loaded["questions"] = 2
        await db.save_draft(loaded)
        check("черновик обновляется", (await db.get_draft(draft["id"]))["summary_sent"] is True)

        order_id = await db.create_order(loaded, order_items_payload(loaded), None)
        order = await db.get_order(order_id)
        check("заказ создан", order is not None and order["id"] == order_id)
        check("позиции заказа", len(order["items"]) == 1, str(len(order["items"])))
        check("сумма заказа", order["total"] == 10.5, str(order["total"]))
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
        await db.save_alias("мясо", 4)
        await db.save_alias("мясо", 4)
        check("alias сохраняется и обновляется", await db.get_aliases() == {"мясо": 4})

        # личные алиасы поваров не мешают друг другу
        await db.save_alias("кура", 4, chef_id=501)
        await db.save_alias("курятина", 4, chef_id=502)
        chef1_aliases = await db.get_aliases(501)
        chef2_aliases = await db.get_aliases(502)
        check("alias повара 1: кура → филе", chef1_aliases.get("кура") == 4, str(chef1_aliases))
        check("alias повара 2: курятина → филе", chef2_aliases.get("курятина") == 4, str(chef2_aliases))
        check(
            "общий alias не появился от двух поваров",
            (await db.get_aliases()).get("кура") is None,
        )
        _, chef1_draft = draft_for("кура 1 кг", chef1_aliases)
        _, chef2_draft = draft_for("курятина 1 кг", chef2_aliases)
        check("повар 1 разбирает куру как филе", chef1_draft["items"][0]["catalog_id"] == 4)
        check("повар 2 разбирает курятину как филе", chef2_draft["items"][0]["catalog_id"] == 4)

        # F4: по умолчанию общий alias не создаётся, только личный
        db.alias_auto_global = False
        await db.save_alias("птица", 4, chef_id=601)
        await db.save_alias("птица", 4, chef_id=602)
        await db.save_alias("птица", 4, chef_id=603)
        check("без флага общий alias не создаётся", (await db.get_aliases()).get("птица") is None)
        # с явно включённым флагом — прежнее поведение
        db.alias_auto_global = True
        await db.save_alias("птица2", 4, chef_id=611)
        await db.save_alias("птица2", 4, chef_id=612)
        await db.save_alias("птица2", 4, chef_id=613)
        check("с флагом три повара дают общий", (await db.get_aliases()).get("птица2") == 4)
        db.alias_auto_global = False
        for _ in range(3):
            await db.save_alias("мясцо", 4, chef_id=604)
        check("три раза один повар — не общий", (await db.get_aliases()).get("мясцо") is None)

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
        _, big_draft = draft_for("филе 2 кг")
        big_draft["chat_id"] = big_id
        big_draft["user_id"] = big_id
        await db.ensure_user(big_id, "Большой Клиент", None)
        await db.create_draft(big_draft)
        big_order = await db.create_order(
            big_draft, order_items_payload(big_draft), None
        )
        check(
            "заказ с большим Telegram id",
            (await db.get_order(big_order))["chef_id"] == big_id,
        )
        # Новый поток: упаковщик → доставщик → оплата → доставлено.
        flow_user = 9001
        await db.ensure_user(flow_user, "Тест", None)
        await db.set_user_phone(flow_user, "+993 12 345678")
        await db.set_user_address(flow_user, "Ашхабад, ул. Тестовая, 1")
        check("телефон сохранён", (await db.get_user(flow_user) or {}).get("phone") == "+993 12 345678")
        check("адрес сохранён", "Ашхабад" in ((await db.get_user(flow_user) or {}).get("address") or ""))
        _, flow_draft = draft_for("филе 2 кг")
        flow_draft["delivery_address"] = "Ашхабад, ул. Тестовая, 1"
        flow_draft["delivery_phone"] = "+993 12 345678"
        flow_draft["payment_method"] = "cash"
        flow_order_id = await db.create_order(
            flow_draft, order_items_payload(flow_draft), None
        )
        flow = await db.get_order(flow_order_id)
        check("адрес в заказе", flow["delivery_address"].startswith("Ашхабад"))
        check("оплата наличными по умолчанию", flow["payment_method"] == "cash")
        check("заказ ждёт упаковщика", flow["status"] == "new")
        check("заказ не оплачен", flow["payment_status"] == "unpaid")
        check("нельзя доставить без оплаты", await db.mark_order_delivered(flow_order_id) is False)
        check("упаковщик собрал", await db.mark_order_ready(flow_order_id, 3003))
        flow = await db.get_order(flow_order_id)
        check("статус ready", flow["status"] == "ready" and flow["packed_at"])
        check("доставщик назначен", flow["courier_chat_id"] == 3003)
        check("выехал", await db.mark_out_for_delivery(flow_order_id))
        flow = await db.get_order(flow_order_id)
        check("статус delivering", flow["status"] == "delivering" and flow["delivering_at"])
        check(
            "оплата наличными зафиксирована",
            await db.mark_order_paid(flow_order_id, "cash", 10.5),
        )
        flow = await db.get_order(flow_order_id)
        check("оплата paid", flow["payment_status"] == "paid" and flow["paid_at"])
        check("нельзя оплатить дважды", await db.mark_order_paid(flow_order_id, "cash", 10.5) is False)
        check("доставлено", await db.mark_order_delivered(flow_order_id))
        flow = await db.get_order(flow_order_id)
        check("статус done", flow["status"] == "done" and flow["delivered_at"])

        _, unpaid_draft = draft_for("филе 1 кг")
        unpaid_id = await db.create_order(
            unpaid_draft, order_items_payload(unpaid_draft), None
        )
        await db.mark_order_ready(unpaid_id, 3003)
        check("без оплаты done запрещён", await db.mark_order_delivered(unpaid_id) is False)
        check("сбой доставки", await db.mark_order_failed(unpaid_id))
        check("статус failed", (await db.get_order(unpaid_id))["status"] == "failed")

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
        check("usual: одна позиция", len(usual) == 1, str(usual))
        check(
            "usual: филе 2 кг",
            usual[0]["catalog_id"] == 4
            and usual[0]["qty"] == 2
            and usual[0]["unit"] == "кг",
            str(usual),
        )
        usual_one = await db.usual_for_item(1, 4)
        check(
            "usual: количество позиции",
            usual_one == {"qty": 2.0, "unit": "кг"},
            str(usual_one),
        )

        check("заказ за последний час", await db.count_orders_since(1) >= 1)
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

        # Версии прайса: храним 5 последних, старые файлы удаляются.
        version_dir = Path(tmp) / "price_versions"
        version_dir.mkdir(parents=True, exist_ok=True)
        version_paths = []
        for index in range(6):
            version_file = version_dir / f"catalog-{index}.xlsx"
            version_file.write_bytes(b"x")
            version_paths.append(version_file)
            await db.add_price_version(str(version_file), f"v{index}", 1)
        versions = await db.list_price_versions(5)
        check("история прайса: 5 версий", len(versions) == 5, str(len(versions)))
        check("история прайса: последняя сверху", versions[0]["summary"] == "v5", str(versions[0]))
        removed_versions = await db.prune_price_versions(5)
        check("история прайса: файл старейшей удалён", str(version_paths[0]) in removed_versions)
        check("история прайса: осталось 5", len(await db.list_price_versions(9)) == 5)
        await db.close()

        db2 = (
            Database(Path(tmp) / "unused.db", TEST_URL)
            if TEST_URL
            else Database(Path(tmp) / "test.db")
        )
        await db2.connect()
        await db2.init()
        check("данные переживают перезапуск", (await db2.get_order(order_id)) is not None)
        check("alias переживает перезапуск", (await db2.get_aliases()).get("мясо") == 4)
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
        check(
            "/start повара спрашивает заведение",
            any("Как называется ваше заведение" in text for text, _ in chef_message.sent),
            str(chef_message.sent),
        )

        # Повар указал заведение, менеджер допустил.
        await db.set_user_venue(chef_id, "Тестовая кухня")
        await db.set_user_phone(chef_id, "+993 12 000001")
        await db.set_user_address(chef_id, "Ашхабад, Тестовая, 1")
        await db.set_user_access(chef_id, "approved")
        chef_message.sent.clear()
        await cmd_start(chef_message, db, settings, CATALOG)
        check(
            "/start повара после допуска",
            len(chef_message.sent) in (1, 2),
            f"сообщений: {len(chef_message.sent)}",
        )
        text, keyboard = chef_message.sent[0]
        check("/start повара: текст приветствия", START_TEXT in text, text[:120])
        check("/start повара: кнопки меню", keyboard is not None)
        check("/start повара: название заведения", "Тестовая кухня" in text, text[:120])
        check(
            "/start повара: сбрасывает ожидание количества",
            await db.get_chat_state(chef_id, "qty_input") is None,
        )

        # Старый клиент: профиль создаём до заказа — внешний ключ orders.chef_id
        # больше не даёт завести заказ на несуществующего пользователя.
        await db.ensure_user(424242, "Старый Повар", None)
        await db.conn.execute(
            "INSERT INTO orders(created_at, chef_id, chef_name, total, status) VALUES(?,?,?,?,?)",
            ("2026-10-01 10:00:00", 424242, "Старый Повар", 0, "done"),
        )
        await db.conn.commit()
        old_user = await db.ensure_user(424242, "Старый Повар", None)
        check("старый клиент: профиль цел", old_user["user_id"] == 424242, str(old_user))

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

    middleware = AllowedChatsMiddleware({555}, 777, 888)

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
    courier = await middleware(
        handler, SimpleNamespace(chat=SimpleNamespace(id=888)), {}
    )
    check("чужой чат игнорируется", blocked is None, str(blocked))
    check("разрешённый чат проходит", allowed == "ok")
    check("менеджер проходит", manager == "ok")
    check("доставщик проходит", courier == "ok")


async def main() -> int:
    print("=" * 70)
    print("Самопроверка Order bot")
    print(f"Прайс: {len(CATALOG.items)} позиций")
    print(f"БД:    {'PostgreSQL (TEST_DATABASE_URL)' if TEST_URL else 'SQLite'}")
    print("=" * 70)
    test_core()
    test_slots()
    test_module_names()
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
