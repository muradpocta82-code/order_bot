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
from orderbot.catalog import Catalog, diff_catalogs  # noqa: E402
from orderbot.db import Database, reset_test_database  # noqa: E402
from orderbot.drafts import (  # noqa: E402
    all_resolved,
    build_draft,
    draft_total,
    order_items_payload,
)
from orderbot.formatting import format_draft  # noqa: E402
def _latest_schema_version() -> int:
    """Версия схемы = последний номер файла в migrations/*.sql."""
    files = sorted(
        int(p.name[:4])
        for p in (Path(__file__).resolve().parent.parent / "migrations").glob("*.sql")
        if p.name[:4].isdigit()
    )
    return files[-1] if files else 0


LATEST_VERSION = _latest_schema_version()
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


def test_ledger_math() -> None:
    print("\n[книга расчётов: зачёт FIFO]")
    from orderbot.ledger import balances, differences, fifo_allocate, open_amount

    check("остаток по заказу", open_amount({"total": 100, "paid_amount": 40}) == 60.0)
    check("оплаченный заказ — ноль", open_amount({"total": 100, "paid_amount": 100}) == 0.0)
    check(
        "отменённый не в счёт",
        open_amount({"total": 100, "paid_amount": 0, "status": "cancelled"}) == 0.0,
    )
    orders = [
        {"id": 1, "total": 50, "paid_amount": 0},
        {"id": 2, "total": 30, "paid_amount": 10},
        {"id": 3, "total": 40, "paid_amount": 40},
    ]
    allocations, advance = fifo_allocate(60, orders)
    check("первый заказ закрыт", (1, 50.0) in allocations, str(allocations))
    check("второй частично", (2, 10.0) in allocations, str(allocations))
    check("оплаченный не трогаем", all(order_id != 3 for order_id, _ in allocations), str(allocations))
    check("аванса нет", advance == 0.0, str(advance))
    allocations2, advance2 = fifo_allocate(200, orders)
    check("всё закрыто", sum(amount for _, amount in allocations2) == 70.0, str(allocations2))
    check("осталось авансом", advance2 == 130.0, str(advance2))
    check("пустой платёж", fifo_allocate(0, orders) == ([], 0.0))
    cancelled = [{"id": 9, "total": 100, "paid_amount": 0, "status": "cancelled"}]
    check("отменённые пропускаются", fifo_allocate(50, cancelled) == ([], 50.0))

    result = balances({1: 100.0}, {1: 40.0}, {1: 10.0})
    check("долг с зачётом", result == {1: 50.0}, str(result))
    empty = balances({}, {})
    check("пустые балансы", empty == {})
    diff = differences({1: 50.0, 2: 0.0}, {1: 40.0, 2: 0.0})
    check("расхождение найдено", diff == {1: (50.0, 40.0)}, str(diff))
    check("мелкая разница — не расхождение", differences({1: 50.0}, {1: 50.005}) == {})


async def test_ledger_db() -> None:
    print("\n[книга расчётов: платёж одной суммой за несколько заказов]")
    if not TEST_URL:
        print("  — пропущено: нужна тестовая PostgreSQL-база")
        return
    from orderbot.db import reset_test_database

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "unused.db", TEST_URL)
        await db.connect()
        try:
            await reset_test_database(db)
            client = 9101
            await db.ensure_user(client, "Опт Тест")
            ids = []
            for total in (40.0, 30.0, 20.0):
                draft = {
                    "id": f"ledger-{total}",
                    "chat_id": client,
                    "user_id": client,
                    "delivery_date": "2026-11-22",
                    "delivery_time": "09:00–12:00",
                    "source": "test",
                }
                items = [{"catalog_id": 4, "name": "Куриное филе", "qty": total / 10,
                          "unit": "кг", "price": 10.0, "total": total}]
                ids.append(await db.create_order(draft, items, None))
            result = await db.add_payment(client, 60.0, method="transfer", note="за неделю")
            check("платёж записан", result["payment_id"] > 0, str(result))
            check(
                "раскидан по двум заказам FIFO",
                result["allocated"] == [(ids[0], 40.0), (ids[1], 20.0)],
                str(result["allocated"]),
            )
            first = await db.get_order(ids[0])
            second = await db.get_order(ids[1])
            third = await db.get_order(ids[2])
            check("первый закрыт", first["payment_status"] == "paid", str(first["payment_status"]))
            check("второй частично", float(second["paid_amount"] or 0) == 20.0, str(second["paid_amount"]))
            check("третий не тронут", float(third["paid_amount"] or 0) == 0.0)
            result2 = await db.add_payment(client, 50.0, method="cash")
            check("вторая сумма закрыла второй заказ", result2["allocated"][0] == (ids[1], 10.0), str(result2))
            check("остаток ушёл в третий", (ids[2], 20.0) in result2["allocated"], str(result2))
            check("аванс 20", result2["advance"] == 20.0, str(result2["advance"]))
            totals = await db.ledger_totals()
            check("итоги книги: оплачено", totals["paid"][client] == 110.0, str(totals["paid"]))

            # шаг 2: долг по книге считается только по доставленным и неоплаченным
            debts_before = await db.ledger_client_debts()
            check(
                "в долгах по книге — только доставленное и неоплаченное",
                all(float(row["open"]) > 0 for row in debts_before),
                str(debts_before),
            )
            book_before = await db.client_book_debt(client)
            check(
                "долг по книге = выставлено − оплачено (тут аванс)",
                book_before["open"] == -20.0,
                str(book_before),
            )
            await db.add_credit_note(client, 15.0, "недовес")
            book_after = await db.client_book_debt(client)
            check(
                "кредит-нота уменьшила долг",
                book_after["open"] == -35.0 and book_after["credited"] == 15.0,
                str(book_after),
            )
            credit_sum = await db.credit_orders_sum(client)
            check("сумма «в долг» для /settle", credit_sum >= 0.0, str(credit_sum))
        finally:
            await db.close()


def test_route_plan() -> None:
    print("\n[маршрут курьера]")
    from orderbot.route import (
        distance_km,
        maps_link,
        parse_coords,
        plan,
        total_distance_km,
    )

    check("координаты из адреса", parse_coords("📍 41.311081, 69.240562") == (41.311081, 69.240562))
    check("координаты из текста", parse_coords("Ташкент 41.300000, 69.200000 дом 5") is not None)
    check("без координат — None", parse_coords("улица Навои 1") is None)
    check("мусор — None", parse_coords("📍 999.0, 999.0") is None)
    centre = (41.311081, 69.240562)
    check("расстояние до себя — 0", distance_km(centre, centre) == 0.0)
    other = (41.321081, 69.240562)
    check("1 км по широте", 0.5 < distance_km(centre, other) < 1.5, str(distance_km(centre, other)))

    orders = [
        {"id": 1, "delivery_time": "15:00–18:00", "delivery_address": "📍 41.350000, 69.300000"},
        {"id": 2, "delivery_time": "09:00–12:00", "delivery_address": "📍 41.320000, 69.250000"},
        {"id": 3, "delivery_time": "09:00–12:00", "delivery_address": "📍 41.311500, 69.240600"},
        {"id": 4, "delivery_time": "09:00–12:00", "delivery_address": "улица без геопина"},
    ]
    planned = plan(orders, start=centre)
    check("утро раньше вечера", planned[0]["delivery_time"].startswith("09"), str([o["id"] for o in planned]))
    check("ближайший первым", planned[0]["id"] == 3, str([o["id"] for o in planned]))
    check("без геопина в конце слота", planned[2]["id"] == 4, str([o["id"] for o in planned]))
    check("вечерний последним", planned[-1]["id"] == 1, str([o["id"] for o in planned]))
    check("ничего не потеряно", len(planned) == 4)
    check("порядок устойчив", [o["id"] for o in plan(orders, start=centre)] == [o["id"] for o in planned])

    link = maps_link(planned)
    check("ссылка на карты", link.startswith("https://yandex.com/maps/?rtext="), link)
    check("в ссылке все точки", link.count("~") == 2, link)
    check("без координат ссылки нет", maps_link([{"delivery_address": "адрес"}]) == "")
    check("длина маршрута считается", total_distance_km(planned, centre) > 0)
    check("пустой день", plan([]) == [] and total_distance_km([]) == 0)


def test_settings_router() -> None:
    print("\n[команды расписания вынесены и подключены]")
    from orderbot.handlers import router as main_router
    from orderbot.handlers_settings import router as settings_router

    check("свой роутер расписания", settings_router.name == "orderbot_settings", settings_router.name)
    commands = [
        handler.callback.__name__
        for handler in settings_router.message.handlers
        if hasattr(handler, "callback")
    ]
    check(
        "три команды в роутере",
        {"cmd_slots", "cmd_cutoff", "cmd_weekend"} <= set(commands),
        str(commands),
    )
    names = [getattr(r, "name", "") for r in main_router.sub_routers]
    check("роутер подключён к основному", "orderbot_settings" in names, str(names))
    check(
        "подключён раньше хвоста",
        names.index("orderbot_settings") < names.index("orderbot_tail"),
        str(names),
    )

    from orderbot.handlers_stock import router as stock_router

    stock_commands = [
        handler.callback.__name__
        for handler in stock_router.message.handlers
        if hasattr(handler, "callback")
    ]
    check(
        "складские команды в своём роутере",
        {"cmd_missing", "cmd_quality", "cmd_stop_item", "cmd_start_item"}
        <= set(stock_commands),
        str(stock_commands),
    )
    check("роутер склада подключён", "orderbot_stock" in names, str(names))
    check(
        "склад раньше хвоста",
        names.index("orderbot_stock") < names.index("orderbot_tail"),
        str(names),
    )


def test_costvoice() -> None:
    print("\n[закупки голосом: разбор цен]")
    from orderbot.costvoice import as_payload, describe, match_lines, parse_pairs

    pairs = parse_pairs("помидоры 8500, лук 3200, огурцы 9000")
    check("три пары", len(pairs) == 3, str(pairs))
    check("первая пара", pairs[0][0].lower().startswith("помидор") and pairs[0][1] == 8500, str(pairs[0]))
    check("цена через запятую", parse_pairs("филе 1,10")[0][1] == 1.10, str(parse_pairs("филе 1,10")))
    check("двоеточие не мешает", len(parse_pairs("помидоры: 8500; лук: 3200")) == 2)
    check("без цифр пусто", parse_pairs("просто текст без цен") == [])
    check("и в начале фразы отброшено", "и" != parse_pairs("и лук 3200")[0][0].lower())

    lines = match_lines([("куриное филе", 3.5), ("невиданный овощ", 10.0)], CATALOG)
    check("позиция найдена", lines[0].ok and lines[0].catalog_id == 4, str(lines[0]))
    check("неизвестное не привязано", not lines[1].ok, str(lines[1]))
    text = describe(lines)
    check("в тексте подтверждения обе строки", "Не нашёл в прайсе" in text, text)
    payload = as_payload(lines)
    check("в запись идут только найденные", len(payload) == 1 and payload[0]["catalog_id"] == 4, str(payload))
    check("округляет цену", payload[0]["cost"] == 3.5)


def test_statement() -> None:
    print("\n[акт сверки]")
    import tempfile
    from pathlib import Path

    from orderbot.statement import build_statement, totals

    orders = [
        {"id": 1, "created_at": "2026-11-10 10:00:00", "total": 100.0, "status": "done",
         "payment_status": "paid", "paid_amount": 100.0},
        {"id": 2, "created_at": "2026-11-15 10:00:00", "total": 55.5, "status": "done",
         "payment_status": "credit", "paid_amount": None},
        {"id": 3, "created_at": "2026-11-16 10:00:00", "total": 20.0, "status": "cancelled",
         "payment_status": "unpaid", "paid_amount": None},
    ]
    numbers = totals(orders, [{"amount": 5.5}])
    check("выставлено без отменённых", numbers["billed"] == 155.5, str(numbers))
    check("оплачено", numbers["paid"] == 100.0, str(numbers))
    check("кредит-нота вычтена", numbers["credit"] == 5.5, str(numbers))
    check("сальдо", round(numbers["balance"], 2) == 50.0, str(numbers))
    check("пустой период", totals([], []) == {
        "billed": 0.0, "paid": 0.0, "credit": 0.0, "balance": 0.0,
    })
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "act.pdf"
        build_statement(
            {"venue": "Кафе Тест", "phone": "+998 90 000 00 00"},
            orders,
            target,
            "20.10.2026",
            "19.11.2026",
            [{"order_id": 2, "created_at": "2026-11-17 12:00:00", "text": "недовес", "amount": 5.5}],
        )
        check("PDF создан", target.exists() and target.stat().st_size > 1000, str(target))
        check("PDF начинается как PDF", target.read_bytes()[:4] == b"%PDF")


def test_client_card() -> None:
    print("\n[карточка клиента]")
    from orderbot.clientcard import card_text, frequency_text

    check("один заказ — мало данных", "мало данных" in frequency_text(["2026-11-19 10:00"]))
    check(
        "раз в два дня",
        "через день" in frequency_text(["2026-11-19 10:00", "2026-11-17 10:00"]),
        frequency_text(["2026-11-19 10:00", "2026-11-17 10:00"]),
    )
    check(
        "раз в неделю",
        "каждые 7" in frequency_text(
            ["2026-11-19 10:00", "2026-11-12 10:00", "2026-11-05 10:00"]
        ),
        frequency_text(["2026-11-19 10:00", "2026-11-12 10:00", "2026-11-05 10:00"]),
    )
    user = {
        "user_id": 5,
        "venue": "Кафе Ромашка",
        "name": "Повар Иван",
        "access": "approved",
        "phone": "+998 90 000 00 00",
        "address": "улица Навои 1",
        "credit_days": 7,
        "credit_limit": 200,
    }
    stats = {"orders": 4, "revenue": 100.0, "last": "2026-11-19 10:00", "dates": [
        "2026-11-19 10:00", "2026-11-15 10:00", "2026-11-11 10:00",
    ]}
    text = card_text(
        user,
        stats,
        {"sum": 30.0, "count": 2, "age": 12},
        [{"name": "Куриное филе", "qty": 2, "unit": "кг"}],
        note="вход со двора",
        referrer="Кафе Люкс",
        claims=1,
        rating=4.5,
    )
    check("заведение в заголовке", "Кафе Ромашка" in text, text)
    check("id показан", "5" in text)
    check("выручка и средний чек", "$100" in text and "$25" in text, text)
    check("долг с возрастом", "старейший 12 дн." in text, text)
    check("условия отсрочки", "срок 7 дн." in text and "лимит" in text, text)
    check("любимая позиция", "Куриное филе — 2 кг" in text, text)
    check("претензия и оценка", "Претензий: 1" in text and "4.5 из 5" in text, text)
    check("заметка", "вход со двора" in text, text)
    check("кто привёл", "Кафе Люкс" in text, text)
    check("без долга — так и пишет", "Долга нет" in card_text(
        {**user, "credit_days": None, "credit_limit": None},
        {"orders": 0, "revenue": 0, "last": "", "dates": []},
        {"sum": 0, "count": 0, "age": None},
        [],
    ))
    check("новичок без заказов", "не было" in card_text(
        {"user_id": 6, "access": "pending"},
        {"orders": 0, "revenue": 0, "last": "", "dates": []},
        {"sum": 0, "count": 0, "age": None},
        [],
    ))


def test_margin_slices() -> None:
    print("\n[маржа по срезам]")
    from orderbot.margins import collect, report_lines

    class FakeItem:
        def __init__(self, name, category, purchase=None):
            self.name = name
            self.category = category
            self.purchase = purchase

    class FakeCatalog:
        def __init__(self, items):
            self._items = items

        def get(self, item_id):
            return self._items.get(int(item_id))

    catalog = FakeCatalog(
        {
            4: FakeItem("Куриное филе", "мясо"),
            5: FakeItem("Помидоры", "овощи"),
            6: FakeItem("Моцарелла", "сыр", purchase=4.0),
        }
    )
    rows = [
        {"catalog_id": 4, "name": "Куриное филе", "qty": 2, "price": 5.25, "chef_id": 1, "chef_name": "Кафе Ромашка"},
        {"catalog_id": 5, "name": "Помидоры", "qty": 10, "price": 1.0, "chef_id": 2, "chef_name": "Плов-хаус"},
        {"catalog_id": 6, "name": "Моцарелла", "qty": 1, "price": 5.0, "chef_id": 1, "chef_name": "Кафе Ромашка"},
    ]
    costs = {4: 3.5, 5: 1.4}  # по помидорам закупка выше продажи — убыток
    data = collect(rows, catalog, costs)
    check("выручка посчитана", round(data["totals"]["revenue"], 2) == 25.5, str(data["totals"]))
    check("закупка посчитана", round(data["totals"]["cost"], 2) == 25.0, str(data["totals"]))
    check("маржа", round(data["margin"], 2) == 0.5, str(data["margin"]))
    check("три категории", len(data["categories"]) == 3, str([c[0] for c in data["categories"]]))
    check("два клиента", len(data["clients"]) == 2, str(data["clients"]))
    check("убыточная позиция найдена", [e["name"] for e in data["lossy"]] == ["Помидоры"], str(data["lossy"]))
    check(
        "запасная закупка из прайса работает",
        any(e["name"] == "Моцарелла" and e["known"] for e in data["positions"]),
        str(data["positions"]),
    )
    unknown_data = collect(
        [{"catalog_id": 99, "name": "Трюфель", "qty": 1, "price": 50.0, "chef_id": 3}],
        catalog,
        {},
    )
    check(
        "неизвестная закупка помечена",
        unknown_data["totals"]["unknown"] == 1
        and all(not e["known"] for e in unknown_data["positions"]),
        str(unknown_data["totals"]),
    )
    check(
        "без закупки позиция не считается убыточной",
        unknown_data["lossy"] == [],
        str(unknown_data["lossy"]),
    )
    lines = report_lines(data)
    text = "\n".join(lines)
    check("категории в отчёте", "Категории" in text, text)
    check("клиенты в отчёте", "Клиенты" in text, text)
    check("убыточные в отчёте", "Убыточные позиции" in text, text)
    check("помидоры в убытке", "Помидоры" in text, text)

    empty = collect([], catalog, {})
    check("пустые данные не падают", empty["margin"] == 0.0 and report_lines(empty) == [])


def test_week_report() -> None:
    print("\n[недельный отчёт: сравнение и выводы]")
    from orderbot.extras import week_advice, week_comparison, week_metrics

    good = [
        {"status": "done", "total": 100.0, "chef_id": 1},
        {"status": "done", "total": 50.0, "chef_id": 2},
        {"status": "cancelled", "total": 999.0, "chef_id": 3},
    ]
    metrics = week_metrics(good)
    check("отменённые не считаются", metrics["orders"] == 2, str(metrics))
    check("выручка", metrics["revenue"] == 150.0, str(metrics["revenue"]))
    check("средний чек", metrics["avg"] == 75.0, str(metrics["avg"]))
    check("клиенты", metrics["clients"] == 2)

    empty = week_metrics([])
    check("пустая неделя без деления на ноль", empty["avg"] == 0.0)

    growth = week_comparison({"revenue": 200.0, "orders": 4, "avg": 50.0, "clients": 3},
                             {"revenue": 100.0, "orders": 2, "avg": 50.0, "clients": 2})
    check("сравнение с прошлой неделей", any("К прошлой неделе" in line for line in growth), str(growth))
    check("рост показан стрелкой", any("🔼" in line for line in growth), str(growth))
    fall = week_comparison({"revenue": 50.0, "orders": 1, "avg": 50.0, "clients": 1},
                           {"revenue": 200.0, "orders": 4, "avg": 50.0, "clients": 3})
    check("падение показано стрелкой", any("🔽" in line for line in fall), str(fall))
    check(
        "без прошлой недели — честная пометка",
        "не с чем" in week_comparison(metrics, week_metrics([]))[0],
    )

    advice = week_advice({"revenue": 50.0, "orders": 1, "avg": 50.0, "clients": 1},
                         {"revenue": 200.0, "orders": 4, "avg": 80.0, "clients": 3},
                         failed=2, claims=1, dropped=1)
    text = "\n".join(advice)
    check("есть выводы", "3 вывода" in text, text)
    check("есть действия", "3 действия" in text, text)
    check("падение выручки замечено", "упала" in text, text)
    check("недоставки замечены", "Недоставок" in text, text)
    conclusions = advice.index("<b>3 действия</b>") if "<b>3 действия</b>" in advice else len(advice)
    numbered = [line for line in advice[:conclusions] if line[:2] in ("1.", "2.", "3.")]
    check("не больше трёх выводов", len(numbered) <= 3, str(advice))
    calm = week_advice({"revenue": 100.0, "orders": 3, "avg": 33.0, "clients": 3},
                       {"revenue": 100.0, "orders": 3, "avg": 33.0, "clients": 3})
    calm_text = "\n".join(calm)
    check(
        "без проблем — не пугаем падением и претензиями",
        "упала" not in calm_text and "Претензий" not in calm_text and "Недоставок" not in calm_text,
        calm_text,
    )


def test_webapp_light() -> None:
    print("\n[мини-апп: JSON-каталог и подпись]")
    import hashlib
    import hmac
    import json
    import tempfile
    import time
    from pathlib import Path
    from urllib.parse import urlencode

    from orderbot.catalog import Catalog
    from webapp import server as web

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "catalog.json"
        count = CATALOG.dump_json(path)
        check("JSON записан целиком", count == len(CATALOG.items), f"{count}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        first = payload["items"][0]
        check("поля позиции", {"id", "name", "unit", "price"} <= set(first), str(first))
        check("обновление записано", bool(payload.get("updated")))
        check("временный файл убран", not (Path(tmp) / "catalog.json.tmp").exists())

    # подпись Telegram считается без aiogram
    token = "123456:TEST-TOKEN"
    pairs = {"auth_date": str(int(time.time())), "user": '{"id": 42}'}
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    sig = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    good = urlencode({**pairs, "hash": sig})
    check("правильная подпись проходит", web.check_init_data(good, token))
    check("битая подпись не проходит", not web.check_init_data(good, "другойтокен"))
    check("без подписи отказ", not web.check_init_data("auth_date=1&user=%7B%7D", token))
    check("пустые данные отказ", not web.check_init_data("", token))
    old = {"auth_date": str(int(time.time()) - 90000), "user": '{"id": 42}'}
    check_string_old = "\n".join(f"{k}={v}" for k, v in sorted(old.items()))
    sig_old = hmac.new(secret, check_string_old.encode(), hashlib.sha256).hexdigest()
    check(
        "просроченная подпись не проходит",
        not web.check_init_data(urlencode({**old, "hash": sig_old}), token),
    )
    check("каталог читается из JSON", callable(web.load_catalog))


def test_batch_questions() -> None:
    print("\n[пакетные уточнения]")
    from orderbot.batchq import (
        MAX_BATCH,
        batch_group,
        batch_keyboard,
        batch_text,
    )

    def item(idx: int, status: str, **extra) -> dict:
        base = {"idx": idx, "status": status, "name": f"Позиция{idx}", "qty": 3, "unit": "шт"}
        base.update(extra)
        return base

    one = [item(0, "needs_qty")]
    check("одна позиция — обычный вопрос", batch_group(one) == [])
    two = [item(0, "needs_qty"), item(1, "needs_qty")]
    check("две одинаковые — пакет", len(batch_group(two)) == 2)
    mixed = [item(0, "needs_qty"), item(1, "needs_unit"), item(2, "needs_unit")]
    group = batch_group(mixed)
    check("берём самый частый тип", all(i["status"] == "needs_unit" for i in group), str(group))
    many = [item(i, "needs_qty") for i in range(6)]
    check("не больше MAX_BATCH", len(batch_group(many)) == MAX_BATCH)
    check("порядок по idx", [i["idx"] for i in batch_group(list(reversed(two)))] == [0, 1])

    text = batch_text(two)
    check("нумерованный список", "1)" in text and "2)" in text, text)
    markup = batch_keyboard("draft-1", two)
    first_row = markup.inline_keyboard[0]
    check("строка на позицию", len(markup.inline_keyboard) == 3, str(len(markup.inline_keyboard)))
    check(
        "callback количества",
        first_row[0].callback_data == "qty:draft-1:0:1",
        first_row[0].callback_data,
    )
    check("подпись с номером", first_row[0].text.startswith("1)"), first_row[0].text)
    unit_items = [item(0, "needs_unit", catalog_unit="кг", pack="pack1")]
    unit_markup = batch_keyboard("draft-1", unit_items)
    check(
        "callback фасовки",
        unit_markup.inline_keyboard[0][0].callback_data == "unit:draft-1:0:pack1",
    )
    check(
        "есть отмена",
        unit_markup.inline_keyboard[-1][0].callback_data == "dcancel:draft-1",
    )


def test_golden_live() -> None:
    print("\n[золотой набор из боевых]")
    import json
    import tempfile
    from pathlib import Path

    from orderbot.golden import clean_phrase, count, items_payload, remember_example

    check(
        "телефон вырезан",
        "998" not in clean_phrase("филе 2 кг, звонить +998 90 123 45 67"),
        clean_phrase("филе 2 кг, звонить +998 90 123 45 67"),
    )
    check(
        "адрес вырезан",
        "улица" not in clean_phrase("филе 2 кг, улица Навои 14, дом 3").lower(),
        clean_phrase("филе 2 кг, улица Навои 14, дом 3"),
    )
    check("текст заказа остался", "филе" in clean_phrase("филе 2 кг").lower())
    items = items_payload(
        [
            {"catalog_id": 4, "qty": 2, "unit": "кг"},
            {"catalog_id": None, "qty": 1, "unit": "шт"},
            {"catalog_id": 5, "qty": 0, "unit": "кг"},
        ]
    )
    check("только распознанные позиции", len(items) == 1 and items[0]["catalog_id"] == 4, str(items))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "live.jsonl"
        order = {
            "id": 77,
            "raw_text": "филе 2 кг, тел +998 90 111 22 33",
            "delivery_date": "2026-11-20",
            "items": [{"catalog_id": 4, "qty": 2, "unit": "кг"}],
        }
        case = remember_example(order, path=path)
        check("пример сохранён", case is not None and case["id"] == 1000)
        check("файл на месте", count(path) == 1)
        check(
            "телефон не попал в файл",
            "998" not in path.read_text(encoding="utf-8"),
        )
        second = remember_example(order, path=path)
        check("второй пример с новым id", second is not None and second["id"] == 1001)
        check("в файле два примера", count(path) == 2)
        check(
            "заказ без текста не сохраняется",
            remember_example({"id": 78, "raw_text": "", "items": order["items"]}, path=path)
            is None,
        )
        saved = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        check("структура как в золотом наборе", {"id", "phrase", "items"} <= set(saved))


def test_local_money() -> None:
    print("\n[суммы в сумах по курсу заказа]")
    from orderbot.formatting import (
        LOCAL_CURRENCY,
        order_local_money,
        set_local_rate,
    )

    order = {"total": 10.5, "usd_rate_at_order": 12650}
    text = order_local_money(order)
    check("сумма по курсу заказа", "132 825" in text, text)
    check("валюта в подписи", LOCAL_CURRENCY in text, text)
    old = {"total": 10.5, "usd_rate_at_order": 12000}
    set_local_rate(13000)
    try:
        check(
            "старый заказ не пересчитывается по новому курсу",
            "126 000" in order_local_money(old),
            order_local_money(old),
        )
        check(
            "без курса в заказе — текущий курс",
            "136 500" in order_local_money({"total": 10.5}),
            order_local_money({"total": 10.5}),
        )
    finally:
        set_local_rate(0)
    check("курс не задан — пусто", order_local_money({"total": 10.5}) == "")
    check(
        "нулевая сумма — не показываем",
        order_local_money({"total": 0, "usd_rate_at_order": 12650}) == "",
    )


def test_sanity_checks() -> None:
    print("\n[проверки опечаток]")
    from orderbot.sanity import is_duplicate, suspicious_items

    fillet = {"catalog_id": 4, "name": "Куриное филе", "qty": 2, "unit": "кг"}
    fillet5 = {"catalog_id": 4, "name": "Куриное филе", "qty": 5, "unit": "кг"}
    pieces = {"catalog_id": 4, "name": "Куриное филе", "qty": 5, "unit": "шт"}
    check("одинаковые позиции — дубль", is_duplicate([fillet], [dict(fillet)]))
    check(
        "5 кг и 5 штук — не дубль",
        not is_duplicate([pieces], [fillet5]),
        "единица измерения должна различать заказы",
    )
    check("другое количество — не дубль", not is_duplicate([fillet5], [fillet]))
    check("пустой заказ не дубль", not is_duplicate([], [fillet]))
    check(
        "строка без catalog_id не считается",
        not is_duplicate([{"qty": 2}], [{"qty": 2}]),
    )
    usual = {4: 2.0}
    check("2 кг при обычных 2 кг — норма", suspicious_items([fillet], usual) == [])
    check(
        "8 кг при обычных 2 кг — подозрительно",
        len(suspicious_items([{**fillet, "qty": 8}], usual)) == 1,
    )
    check(
        "5 кг при обычных 2 кг — ещё норма",
        suspicious_items([fillet5], usual) == [],
    )
    check(
        "500 кг — подозрительно даже без истории",
        len(suspicious_items([{**fillet, "qty": 500}], {})) == 1,
    )
    check(
        "новая позиция без истории не тревожит",
        suspicious_items([fillet5], {}) == [],
    )


def test_quiet_hours() -> None:
    print("\n[тихие часы]")
    from datetime import datetime as _dt

    from orderbot.quiet import (
        digest_text,
        in_window,
        parse_window,
        queue_keyboard,
        window_text,
    )

    check("окно ночью задано", parse_window("22-8") == (22, 8))
    check("окно можно выключить", parse_window("off") is None)
    check("мусор в настройке = выключено", parse_window("абракадабра") is None)
    check("равные часы = выключено", parse_window("5-5") is None)
    check("23:00 внутри 22-8", in_window(_dt(2026, 11, 18, 23, 0), (22, 8)))
    check("03:00 внутри 22-8", in_window(_dt(2026, 11, 18, 3, 0), (22, 8)))
    check("12:00 вне 22-8", not in_window(_dt(2026, 11, 18, 12, 0), (22, 8)))
    check("11:00 внутри 8-14", in_window(_dt(2026, 11, 18, 11, 0), (8, 14)))
    check("14:00 уже вне 8-14", not in_window(_dt(2026, 11, 18, 14, 0), (8, 14)))
    check("выключено — тишины нет", not in_window(_dt(2026, 11, 18, 23, 0), None))
    check("подпись окна", window_text((22, 8)) == "с 22:00 до 08:00", window_text((22, 8)))
    items = [
        {"id": 47, "name": "Кафе Ромашка", "slot": "09:00–12:00", "total": 42.5},
        {"id": 48, "name": "Плов-хаус", "slot": "12:00–15:00", "total": 10.0},
    ]
    text = digest_text(items)
    check("сводка считает заказы", "2 заказ(ов)" in text, text)
    check("сводка считает сумму", "52.50" in text, text)
    check("сводка перечисляет номера", "№47" in text and "№48" in text, text)
    markup = queue_keyboard()
    check(
        "кнопка «Открыть очередь»",
        markup.inline_keyboard[0][0].callback_data == "quiet:queue",
    )


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
        print("  — бэкап SQLite удалён вместе с SQLite-режимом (I2)")

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


async def test_pending_price() -> None:
    print("\n[подтверждение прайса переживает перезапуск]")
    from orderbot.pending import (
        KEY_PREFIX,
        purge_pending_prices,
        put_pending_price,
        take_pending_price,
    )

    with tempfile.TemporaryDirectory() as tmp:
        db = (
            Database(Path(tmp) / "unused.db", TEST_URL)
            if TEST_URL
            else Database(Path(tmp) / "pending.db")
        )
        await db.connect()
        try:
            if not TEST_URL:
                await db.init()
            price_file = Path(tmp) / "new-price.xlsx"
            price_file.write_bytes(b"xlsx")
            await put_pending_price(db, "token1", price_file, 12)
            check(
                "токен записан в базу",
                bool(await db.get_setting(KEY_PREFIX + "token1", "")),
            )
            taken = await take_pending_price(db, "token1")
            check("токен достаётся", taken is not None and taken["items"] == 12, str(taken))
            check("файл прайса на месте", price_file.exists())
            check("повторно тот же токен не работает", await take_pending_price(db, "token1") is None)
            check("чужой токен — None", await take_pending_price(db, "нет-такого") is None)

            old_file = Path(tmp) / "old-price.xlsx"
            old_file.write_bytes(b"xlsx")
            await put_pending_price(db, "old", old_file, 1)
            import json as _json
            raw = await db.get_setting(KEY_PREFIX + "old", "")
            data = _json.loads(raw)
            data["created"] = 1.0  # час назад и больше
            await db.set_setting(KEY_PREFIX + "old", _json.dumps(data))
            check("просроченный токен не берётся", await take_pending_price(db, "old") is None)
            check("файл просроченного удалён", not old_file.exists())

            gone_file = Path(tmp) / "gone.xlsx"
            await put_pending_price(db, "gone", gone_file, 3)
            check("нет файла — нет токена", await take_pending_price(db, "gone") is None)

            stale = Path(tmp) / "stale.xlsx"
            stale.write_bytes(b"xlsx")
            await put_pending_price(db, "stale", stale, 5)
            raw = await db.get_setting(KEY_PREFIX + "stale", "")
            data = _json.loads(raw)
            data["created"] = 1.0
            await db.set_setting(KEY_PREFIX + "stale", _json.dumps(data))
            removed = await purge_pending_prices(db)
            check("уборка нашла просроченное", removed >= 1, str(removed))
            check("уборка удалила файл", not stale.exists())
        finally:
            await db.close()


async def test_races() -> None:
    print("\n[гонки: двойные нажатия]")
    if not TEST_URL:
        print("  — пропущено: нужна тестовая PostgreSQL-база")
        return
    from orderbot.db import reset_test_database

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "unused.db", TEST_URL)
        await db.connect()
        try:
            await reset_test_database(db)
            user_id = 7001
            await db.ensure_user(user_id, "Гонки Тест")
            draft = {
                "id": "race-1",
                "chat_id": user_id,
                "user_id": user_id,
                "user_name": "Гонки Тест",
                "delivery_date": "2026-11-21",
                "delivery_time": "09:00–12:00",
                "source": "test",
            }
            items = [
                {
                    "catalog_id": 4,
                    "name": "Куриное филе",
                    "qty": 2,
                    "unit": "кг",
                    "price": 5.25,
                    "total": 10.5,
                }
            ]
            order_id = await db.create_order(draft, items, None)

            # 1. Два упаковщика одновременно жмут «Принят»
            results = await asyncio.gather(
                db.transition_order(order_id, ("new",), "accepted"),
                db.transition_order(order_id, ("new",), "accepted"),
            )
            check("принял только один", sorted(results) == [False, True], str(results))
            order = await db.get_order(order_id)
            check("статус accepted", order["status"] == "accepted", str(order["status"]))

            # 2. Курьер жмёт «Собран» дважды
            await db.transition_order(order_id, ("accepted",), "ready")
            twice = await asyncio.gather(
                db.transition_order(order_id, ("ready",), "delivering"),
                db.transition_order(order_id, ("ready",), "delivering"),
            )
            check("в доставку ушёл один раз", sorted(twice) == [False, True], str(twice))

            # 3. Оплата наличными приходит дважды (курьер и менеджер)
            paid = await asyncio.gather(
                db.mark_order_paid(order_id, "cash", 10.5),
                db.mark_order_paid(order_id, "cash", 10.5),
            )
            check("оплата засчитана один раз", sorted(paid) == [False, True], str(paid))
            fresh = await db.get_order(order_id)
            check("статус оплаты paid", fresh["payment_status"] == "paid")
            check(
                "сумма не удвоилась",
                float(fresh.get("paid_amount") or 0) == 10.5,
                str(fresh.get("paid_amount")),
            )

            # 3b. Смены и назначение заказа упаковщику (D4)
            packer_a, packer_b = 8001, 8002
            await db.add_staff(packer_a, "manager", "Пакер А")
            await db.add_staff(packer_b, "manager", "Пакер Б")
            on_shift = {int(row["user_id"]) for row in await db.staff_on_shift("manager")}
            check("оба на смене по умолчанию", {packer_a, packer_b} <= on_shift, str(on_shift))
            first = await db.pick_packer()
            check("упаковщик выбран", first in (packer_a, packer_b), str(first))
            await db.assign_order(order_id, int(first))
            assigned = await db.assigned_staff(order_id)
            check("назначение записано", assigned is not None and int(assigned["user_id"]) == int(first))
            second = await db.pick_packer()
            check(
                "следующий заказ — другому упаковщику",
                second is not None and int(second) != int(first),
                f"{first} → {second}",
            )
            await db.set_staff_shift(int(first), False)
            after_off = {int(row["user_id"]) for row in await db.staff_on_shift("manager")}
            check("снятый со смены не получает", int(first) not in after_off, str(after_off))
            check("смена записана в базе", await db.set_staff_shift(int(first), True) is True)
            for uid in (packer_a, packer_b):
                await db.remove_staff(uid)

            # 4. Перенос недоставленного заказа (D7)
            await db.transition_order(order_id, ("delivering",), "ready")
            check("подготовлено: заказ собран", (await db.get_order(order_id))["status"] == "ready")
            await db.mark_order_failed(order_id)
            failed = await db.get_order(order_id)
            check("недоставка записана", failed["status"] == "failed", str(failed["status"]))
            moved = await db.reschedule_order(order_id, "2026-11-25")
            check("перенос разрешён из failed", moved is True)
            after = await db.get_order(order_id)
            check("дата перенесена", after["delivery_date"] == "2026-11-25", str(after["delivery_date"]))
            check("вернулся в очередь новым", after["status"] == "new", str(after["status"]))
            check("оплата не сброшена", after["payment_status"] == "paid")
            await db.transition_order(order_id, ("new",), "accepted")
            await db.transition_order(order_id, ("accepted",), "ready")
            await db.transition_order(order_id, ("ready",), "delivering")
            await db.transition_order(order_id, ("delivering",), "done")
            check(
                "доставленный перенести нельзя",
                not await db.reschedule_order(order_id, "2026-11-26"),
            )

            # 4. Отмена уже доставленного заказа не проходит
            check(
                "done нельзя отменить переходом",
                not await db.transition_order(order_id, ("new", "accepted"), "cancelled"),
            )
        finally:
            await db.close()


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
    test_quiet_hours()
    test_sanity_checks()
    test_local_money()
    test_batch_questions()
    test_webapp_light()
    test_week_report()
    test_margin_slices()
    test_client_card()
    test_statement()
    test_costvoice()
    test_settings_router()
    test_route_plan()
    test_ledger_math()
    test_golden_live()
    test_module_names()
    await test_db()
    await test_start_and_greeting()
    await test_failure_safety()
    await test_alias_migration()
    await test_migrations_on_copy()
    await test_locked_db()
    await test_lock_cleanup()
    await test_pending_price()
    await test_ledger_db()
    await test_races()
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
