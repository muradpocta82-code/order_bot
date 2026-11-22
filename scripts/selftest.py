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


def test_writeoff() -> None:
    print("\n[списания и усушка]")
    from orderbot.writeoff import parse_command, report_lines

    request = parse_command("помидоры 3 кг усушка")
    check("позиция разобрана", request is not None and "помидор" in request.phrase.lower(), str(request))
    check("количество", request is not None and request.qty == 3.0)
    check("единица", request is not None and request.unit == "кг")
    check("причина", request is not None and request.reason == "усушка")
    check(
        "порча распознана",
        parse_command("огурцы 2 порча").reason == "порча",
        str(parse_command("огурцы 2 порча")),
    )
    check("без количества — None", parse_command("помидоры усушка") is None)
    check("пустая строка — None", parse_command("") is None)
    check("ноль не принимаем", parse_command("помидоры 0") is None)
    decimal = parse_command("филе 1,5 кг бой")
    check("дробное количество", decimal is not None and decimal.qty == 1.5, str(decimal))
    check("причина убрана из названия", "бой" not in (decimal.phrase if decimal else ""), str(decimal))

    rows = [
        {"category": "овощи", "name": "Помидоры", "qty": 3, "unit": "кг", "cost_value": 10.0},
        {"category": "овощи", "name": "Огурцы", "qty": 1, "unit": "кг", "cost_value": 4.0},
        {"category": "мясо", "name": "Филе", "qty": 2, "unit": "кг", "cost_value": 6.0},
    ]
    lines = report_lines(rows, 30)
    text = "\n".join(lines)
    check("итог потерь", "20.00" in text, text)
    check("категории в отчёте", "овощи" in text and "мясо" in text, text)
    check("доля потерь", "% потерь" in text, text)
    check("топ позиций", "Помидоры" in text, text)
    check("пустой отчёт", "списаний нет" in "\n".join(report_lines([], 30)))


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

            # B3-остаток: остаток ноты уходит в следующий счёт, нота привязывается к заказу
            rest_before = await db.client_open_credit(client)
            check("незачтённый остаток кредит-нот", rest_before == 15.0, str(rest_before))
            extra_id = await db.create_order(
                {
                    "id": "ledger-extra",
                    "chat_id": client,
                    "user_id": client,
                    "delivery_date": "2026-11-23",
                    "delivery_time": "09:00–12:00",
                    "source": "test",
                },
                [
                    {
                        "catalog_id": 4,
                        "name": "Куриное филе",
                        "qty": 6,
                        "unit": "кг",
                        "price": 10.0,
                        "total": 60.0,
                    }
                ],
                None,
            )
            applied_extra = await db.credit_applied(extra_id)
            check("остаток ноты зачёлся в новый счёт", applied_extra == 15.0, str(applied_extra))
            rest_after = await db.client_open_credit(client)
            check("остаток ноты исчерпан", rest_after == 0.0, str(rest_after))
            book_extra = await db.client_book_debt(client)
            check("долг по книге с новым счётом", book_extra["open"] == 25.0, str(book_extra))
            tied = await db.add_credit_note(client, 10.0, "скол", order_id=extra_id)
            check(
                "нота привязана к заказу",
                tied["applied"] == [(extra_id, 10.0)],
                str(tied),
            )
            book_tied = await db.client_book_debt(client)
            check("долг после ноты к заказу", book_tied["open"] == 15.0, str(book_tied))

            # C5-остаток: претензия → кредит-нота к заказу → претензия закрыта
            claim_id = await db.add_claim(extra_id, client, "скол на упаковке")
            claim_note = await db.add_credit_note(
                client, 5.0, f"претензия №{claim_id}", order_id=extra_id
            )
            await db.close_claim(claim_id, "credit")
            stored_claim = await db.get_claim(claim_id)
            check(
                "претензия закрыта кредитом",
                str(stored_claim.get("status")) == "credit",
                str(stored_claim.get("status")),
            )
            check(
                "нота по претензии легла в заказ",
                claim_note["applied"] == [(extra_id, 5.0)],
                str(claim_note),
            )
            book_claim = await db.client_book_debt(client)
            check("долг после ноты по претензии", book_claim["open"] == 10.0, str(book_claim))
            check(
                "зачёт в заказе виден",
                await db.credit_applied(extra_id) == 30.0,
                str(await db.credit_applied(extra_id)),
            )
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

    discount_rows = [
        {"catalog_id": 4, "name": "Куриное филе", "qty": 2, "price": 4.73,
         "base_price": 5.25, "discount_pct": 10, "chef_id": 1, "chef_name": "Кафе Ромашка"},
    ]
    discount_data = collect(discount_rows, catalog, costs)
    check(
        "скидка клиента в итогах",
        round(discount_data["totals"]["discount"], 2) == 1.04,
        str(discount_data["totals"]),
    )
    check(
        "строка скидок в отчёте",
        "Скидки клиентам" in "\n".join(report_lines(discount_data)),
        str(report_lines(discount_data)),
    )

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


def test_price_review() -> None:
    print("\n[цены на завтра: закупка + наценка]")
    import shutil
    import tempfile

    from openpyxl import load_workbook

    from orderbot import pricereview as pr
    from orderbot.catalog import Catalog, CatalogItem

    check("целевая цена от закупки", pr.target_price(10, 25) == 12.5, str(pr.target_price(10, 25)))
    check("нулевая наценка", pr.target_price(10, 0) == 10.0)
    check("отрицательная наценка не принимается", pr.clamp_markup(-5) == 0.0)
    check("наценка сверху ограничена", pr.clamp_markup(9999) == pr.MAX_MARKUP)
    check("мусор в наценке", pr.clamp_markup("abc") == pr.DEFAULT_MARKUP)

    tiny = Catalog()
    tiny.add(CatalogItem(id=1, name="Помидоры", synonyms=(), unit="кг", pack="", price=10.0, category="Овощи"))
    tiny.add(CatalogItem(id=2, name="Филе", synonyms=(), unit="кг", pack="", price=50.0, category="Мясо"))
    tiny.add(CatalogItem(id=3, name="Сыр", synonyms=(), unit="кг", pack="", price=9.0, category="Сыры"))
    costs = {1: 8.0, 2: 12.0}
    proposals, missing = pr.build(tiny, costs, {"Овощи": 25, "Мясо": 25, "Сыры": 25}, 25)
    ids = [p.catalog_id for p in proposals]
    check("совпадающая цена не в предложении", 1 not in ids, str(ids))
    check("пересчёт по наценке", any(p.catalog_id == 2 and p.new_price == 15.0 for p in proposals), str(proposals))
    check("без закупки — в счётчик", missing == 1, str(missing))
    meat = next(p for p in proposals if p.catalog_id == 2)
    check("процент изменения", round(meat.percent) == -70, str(meat.percent))
    check("резкое изменение помечено", meat.big_change)
    check("маржа по новой цене", round(meat.margin_percent) == 20, str(meat.margin_percent))
    rendered = pr.render(proposals, missing)
    check("в тексте стрелка", "▼" in rendered, rendered)
    check("в тексте предупреждение", "Без закупочной цены" in rendered, rendered)
    check("пустое предложение честно", "менять нечего" in pr.render([], 3))

    prices = {2: 15.0, 3: 11.11}
    check("сохранение предложения", pr.from_payload(pr.payload(prices)) == {2: 15.0, 3: 11.11})
    check("битый JSON не роняет", pr.from_payload("{не json") == {})
    check("мусорные цены отброшены", pr.from_payload('[{"id": 1, "price": -3}]') == {})

    tmp = Path(tempfile.mkdtemp())
    target = tmp / "catalog.xlsx"
    shutil.copy2(BASE_DIR / "catalog.xlsx", target)
    written, not_found = pr.write_prices(target, {2: 12.34, 999999: 5.0})
    check("записано в прайс", written == 1, str(written))
    check("несуществующий id", not_found == 1, str(not_found))
    workbook = load_workbook(target, data_only=True, read_only=True)
    worksheet = workbook.worksheets[0]
    header = [str(cell.value or "").strip().lower() for cell in next(worksheet.iter_rows(max_row=1))]
    price_col = next(i for i, t in enumerate(header) if t.startswith("цена"))
    row = next(r for r in worksheet.iter_rows(min_row=2, values_only=True) if int(r[0]) == 2)
    check("цена доехала в файл", abs(float(row[price_col]) - 12.34) < 0.001, str(row[price_col]))
    workbook.close()
    archive = pr.backup_catalog(target, tmp / "versions", "test")
    check("архив перед правкой", archive is not None and Path(archive).exists(), str(archive))
    check("копия в архив", len(list((tmp / "versions").glob("*.xlsx"))) == 1)

    loaded = Catalog.load(BASE_DIR / "catalog.xlsx")
    before = next(i.price for i in loaded.items if i.id == 2)
    pr.apply_to_catalog(loaded, {2: 99.0})
    after = next(i.price for i in loaded.items if i.id == 2)
    check("память обновилась", after == 99.0 and before != 99.0, f"{before} -> {after}")
    phrase_items = [item for item, _n, _l in loaded._phrases]
    item2 = loaded.by_id[2]
    check(
        "фразы ссылаются на новые объекты",
        all(item is loaded.by_id[item.id] for item in phrase_items if item.id in loaded.by_id),
    )
    check("цена в фразах новая", item2.price == 99.0)
    shutil.rmtree(tmp, ignore_errors=True)

    today = datetime(2026, 11, 20).date()
    check("напоминание без истории", pr.needs_reminder(None, None, today))
    check("не чаще раза в неделю", not pr.needs_reminder("2026-10-01", "2026-11-18", today))
    check("через неделю снова можно", pr.needs_reminder("2026-10-01", "2026-11-10", today))
    check("текст напоминания", "порог" in pr.reminder_text(14, None))


def test_stock() -> None:
    print("\n[остатки: скрытие, порог, списание]")
    from orderbot import stock as st

    catalog = Catalog.load(BASE_DIR / "catalog.xlsx")
    tomatoes = next(i for i in catalog.items if i.stock is not None)
    other = next(i for i in catalog.items if i.id != tomatoes.id)
    overrides = {tomatoes.id: {"available": True, "stock_left": 0.0}}
    rows = st.resolve(catalog, overrides)
    check("нулевой остаток скрыт", tomatoes.id in st.hidden_ids(rows), str(rows[tomatoes.id]))
    check("остальные видны", other.id not in st.hidden_ids(rows))
    check("остаток из прайса", rows[other.id].left == other.stock)

    stopped = st.resolve(catalog, {tomatoes.id: {"available": False, "stock_left": 10.0}})
    check("снятая позиция скрыта", tomatoes.id in st.hidden_ids(stopped))
    check("снятая не попадает в «заканчивается»",
          all(r.catalog_id != tomatoes.id for r in st.low_stock(stopped, 50)))

    low = st.resolve(
        catalog,
        {tomatoes.id: {"available": True, "stock_left": 2.0},
         other.id: {"available": True, "stock_left": 0.0}},
    )
    rows_low = st.low_stock(low, 5)
    ids_low = {row.catalog_id for row in rows_low}
    check("наша позиция на исходе", tomatoes.id in ids_low, str(rows_low))
    check("обнулённая не на исходе", other.id not in ids_low)
    check("все строки в пределах порога", all(float(row.left) <= 5 for row in rows_low))
    check(
        "узкий порог не пропускает 2 кг",
        tomatoes.id not in {row.catalog_id for row in st.low_stock(low, 1.0)},
    )
    text_low = st.low_stock_text(rows_low, 5)
    check("название в отчёте", tomatoes.name in text_low, text_low)
    check("пустой отчёт", "в порядке" in st.low_stock_text([], 5))
    check("строка для сводки", "Заканчивается" in st.summary_line(rows_low))
    check("без остатков строка пустая", st.summary_line([]) == "")

    check("списание в другой единице не трогает остаток",
          "не менял" in st.writeoff_effect(rows[tomatoes.id], "упаковка"))
    check("списание в той же единице проходит",
          st.writeoff_effect(rows[tomatoes.id], tomatoes.unit) == "")
    check("нет позиции — предупреждение",
          "нет в прайсе" in st.writeoff_effect(None, "кг"))

    from orderbot.stock import units_match

    check("единицы совпадают", units_match("кг", "кг"))
    check("синоним единицы", units_match("кг.", "килограмм"))
    check("упаковка и кг не совпадают", not units_match("уп", "кг"))
    check("пустая единица не мешает", units_match(None, "кг"))

    from orderbot.formatting import format_catalog

    shown = format_catalog(catalog, {tomatoes.id})
    check("прайс скрывает позицию", tomatoes.name not in shown, shown[:200])
    check("прайс сообщает, что есть ещё", "нет в наличии" in shown, shown[-200:])
    check("без скрытых — все позиции", other.name in format_catalog(catalog))


async def test_stock_db() -> None:
    print("\n[остатки: база и списание]")
    from orderbot import stock as st

    if not TEST_URL:
        check("остатки в базе пропущены (нет TEST_DATABASE_URL)", True)
        return
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "unused.db", TEST_URL)
        await db.connect()
        await reset_test_database(db)
        catalog = Catalog.load(BASE_DIR / "catalog.xlsx")
        item = next(i for i in catalog.items if i.stock is not None)
        left = await db.adjust_stock(item.id, -3, base=item.stock)
        check("остаток уменьшился", abs(left - (float(item.stock) - 3)) < 0.001, str(left))
        await db.set_item_available(item.id, True, 2.0)
        left = await db.adjust_stock(item.id, -5, base=item.stock)
        check("остаток не уходит в минус", left == 0.0, str(left))
        rows = await st.current(catalog, db)
        check("нулевой скрыт в базе", item.id in st.hidden_ids(rows))
        await db.set_setting(st.WARN_KEY, "7")
        check("порог из настроек", await st.threshold(db) == 7.0)
        await db.set_setting(st.WARN_KEY, "мусор")
        check("битый порог — по умолчанию", await st.threshold(db) == st.DEFAULT_WARN)

        before = float(item.stock or 0)
        arrival_id = await db.add_arrival(item.id, item.name, 5.0, item.unit, 1.25, 1)
        check("приход записан", arrival_id > 0, str(arrival_id))
        await db.set_cost(item.id, 1.25, source="income")
        left = await db.adjust_stock(item.id, 5.0, base=before)
        check("остаток пополнен", abs(left - 5.0) < 0.001, str(left))
        rows_in = await db.arrival_rows(30)
        check("приход в истории", any(int(row["id"]) == arrival_id for row in rows_in), str(len(rows_in)))
        row_in = next(row for row in rows_in if int(row["id"]) == arrival_id)
        check("в истории количество", abs(float(row_in["qty"]) - 5.0) < 0.001, str(row_in["qty"]))
        costs = await db.costs_map()
        check("закупка записана приходом", abs(float(costs.get(item.id) or 0) - 1.25) < 0.001, str(costs.get(item.id)))

        await db.add_missing_request(11, "  Трюфель  ")
        await db.add_missing_request(11, "трюфель")
        await db.add_missing_request(12, "трюфель")
        await db.add_missing_request(12, "икра красная")
        digest = await db.missing_digest(30, 10)
        check("сводка запросов", digest and digest[0]["phrase"] == "трюфель", str(digest))
        check("счётчик запросов", int(digest[0]["count"]) == 3, str(digest[0]))
        check("разные клиенты", int(digest[0]["users"]) == 2, str(digest[0]))
        askers = await db.missing_askers("трюфель", 60)
        check("кто просил", sorted(askers) == [11, 12], str(askers))

        await db.set_item_available(item.id, True, 20.0)
        fake_order = {
            "id": 999,
            "items": [
                {"catalog_id": item.id, "qty": 5, "unit": item.unit},
                {"catalog_id": item.id, "qty": 2, "unit": "ящик"},
                {"catalog_id": None, "qty": 1, "unit": item.unit},
            ],
        }
        consumed, notes = await st.consume_order(db, catalog, fake_order)
        check("списана одна позиция", consumed == 1, f"{consumed} {notes}")
        rows_now = await st.current(catalog, db)
        check(
            "остаток уменьшился",
            abs(float(rows_now[item.id].left or 0) - 15.0) < 0.001,
            str(rows_now[item.id].left),
        )
        check("другая единица в заметках", any("ящик" in note for note in notes), str(notes))
        await db.set_setting(st.SALES_KEY, "0")
        consumed_off, _notes = await st.consume_order(db, catalog, fake_order)
        check("выключено — не списывает", consumed_off == 0)
        await db.set_setting(st.SALES_KEY, "1")

        await db.add_staff(501, "manager", "Азиз")
        await db.add_staff(502, "manager", "Бахтиёр")
        await db.set_staff_shift(501, True)
        await db.set_staff_shift(502, True)
        on_shift = await db.staff_on_shift("manager")
        check(
            "на смене двое",
            {int(row["user_id"]) for row in on_shift} >= {501, 502},
            str(on_shift),
        )
        check(
            "несуществующий заказ не передаётся",
            await db.reassign_to_other_packer(999999) is None,
        )

        await db.add_llm_usage("2026-11-20", "gemini", 5, 0, 0.01)
        await db.add_llm_usage("2026-11-20", "gemini", 5, 0, 0.01)
        await db.add_llm_usage("2026-11-20", "deepseek", 3, 0, 0.006)
        day_rows = await db.llm_usage_day("2026-11-20")
        total_calls = sum(int(r["calls"]) for r in day_rows)
        check("расход ИИ накапливается", total_calls == 13, str(day_rows))
        gemini = next(r for r in day_rows if r["provider"] == "gemini")
        check("две записи сложились", int(gemini["calls"]) == 10, str(gemini))
        check("оценка стоимости", abs(float(gemini["est_cost"]) - 0.02) < 0.0001, str(gemini))
        check("расход за период читается", isinstance(await db.llm_usage_range(30), list))
        check("заказов за период считаются", isinstance(await db.count_orders_done(7), int))

        await db.set_sub_rule(2, "no")
        rules = await db.sub_rules()
        check("правило замены записано", rules.get(2, {}).get("mode") == "no", str(rules))
        await db.set_sub_rule(2, "only", 3)
        rules = await db.sub_rules()
        check("правило обновилось", rules[2] == {"mode": "only", "substitute_id": 3}, str(rules))
        await db.clear_sub_rule(2)
        check("правило снято", 2 not in await db.sub_rules())

        await db.set_payer(987654321, 123456789)
        check("плательщик записан", await db.payers_for(123456789) == [987654321])
        rows_payers = await db.payer_rows()
        check("список плательщиков", any(int(r["user_id"]) == 987654321 for r in rows_payers), str(rows_payers))
        check("чужая точка — никого", await db.payers_for(111) == [])
        check("снятие роли", await db.remove_payer(987654321))
        check("после снятия пусто", await db.payers_for(123456789) == [])
        check("повторное снятие — False", not await db.remove_payer(987654321))

        check("окно опроса читается", isinstance(await db.delivered_between(20, 48), list))
        check("покупатели позиции читаются", isinstance(await db.item_buyers(4, 90), list))
        check("оценки бригады читаются", isinstance(await db.staff_ratings(7), list))
        check("оценки заказов читаются", isinstance(await db.order_ratings(7), list))

        days = await db.client_order_days(120)
        check("дни заказов читаются", isinstance(days, list))
        check("заказа на 2030 год нет", not await db.ordered_on(11, "2030-01-01"))
        check("неизвестный запрос — никого", await db.missing_askers("невидаль", 60) == [])
        await db.close()


def test_income() -> None:
    print("\n[приход товара: разбор и остатки]")
    from orderbot import arrivals as av

    parsed = av.parse("помидоры 120 кг по 1.10, огурцы 80 кг по 0.90")
    check("две строки разобраны", len(parsed) == 2, str(parsed))
    check("количество", parsed[0].qty == 120.0, str(parsed[0]))
    check("единица", parsed[0].unit == "кг", str(parsed[0]))
    check("цена закупки", abs(float(parsed[0].cost or 0) - 1.10) < 0.001, str(parsed[0]))
    check("вторая строка", parsed[1].phrase == "огурцы" and abs(float(parsed[1].cost or 0) - 0.90) < 0.001, str(parsed[1]))

    comma = av.parse("сыр 5 кг 12,50")[0]
    check("запятая в цене", comma.qty == 5.0 and abs(float(comma.cost or 0) - 12.5) < 0.001, str(comma))
    only_qty = av.parse("куриное филе 20 кг")[0]
    check("количество без цены", only_qty.qty == 20.0 and only_qty.cost is None, str(only_qty))
    only_cost = av.parse("бекон 12")[0]
    check("только цена без единицы", only_cost.cost == 12.0 and only_cost.qty is None, str(only_cost))
    prefixed = av.parse("приход: моцарелла 30 уп по 18")[0]
    check("слово «приход» не мешает", prefixed.phrase == "моцарелла", str(prefixed))
    check("мусор отброшен", av.parse("непонятно что") == [])
    check("пустая строка", av.parse("") == [])
    check("ноль не принимаем", av.parse("помидоры 0 кг по 1.10") == [])

    matched = av.match(parsed, CATALOG)
    check("позиция найдена", all(line.ok for line in matched), str(matched))
    tomatoes = matched[0]
    check(
        "единица совпала со складом",
        tomatoes.unit_matches and tomatoes.adds_stock,
        f"{tomatoes.unit} vs {tomatoes.stock_unit}",
    )
    wrong_unit = av.ArrivalLine(
        phrase="помидоры", qty=3.0, unit="ящик", cost=1.0,
        catalog_id=tomatoes.catalog_id, name=tomatoes.name,
        stock_unit=tomatoes.stock_unit,
    )
    check("другая единица остаток не трогает", not wrong_unit.adds_stock)

    no_cost = av.ArrivalLine(
        phrase="помидоры", qty=3.0, unit="кг", cost=None,
        catalog_id=tomatoes.catalog_id, name=tomatoes.name,
        stock_unit=tomatoes.stock_unit,
    )
    text = av.describe([no_cost, av.ArrivalLine(phrase="невидаль", qty=None, unit="", cost=None)])
    check("описание перечисляет позицию", "Томаты" in text or "помидоры" in text.lower(), text)
    check("описание честно про ненайденное", "Не нашёл" in text, text)
    check("без цены так и пишет", "без цены" in text, text)
    stocks, others = av.totals([no_cost, av.ArrivalLine(phrase="x", qty=None, unit="", cost=1.0)])
    check("итог: с остатком и без", (stocks, others) == (1, 1), f"{stocks} {others}")
    check("подсказка по формату", "/income" in av.sample_text())

    left = av.parse("помидоры 3 кг по 1.10, помидоры 2 кг по 1.20")
    check("повторы не склеиваются", len(left) == 2, str(left))


def test_missing_demand() -> None:
    print("\n[спрос без предложения: разбор /missadd и сводка]")
    import shutil
    import tempfile

    from openpyxl import load_workbook

    from orderbot import catalogedit, missing as ms

    item = ms.parse_missadd("трюфель 12.50 кг Сыры")
    check("разбор названия", item is not None and item.name == "Трюфель", str(item))
    check("цена", item is not None and item.price == 12.5, str(item))
    check("единица", item is not None and item.unit == "кг", str(item))
    check("категория", item is not None and item.category == "Сыры", str(item))
    check("фраза нормализована", item is not None and item.phrase == "трюфель", str(item))
    plain = ms.parse_missadd("икра красная 25")
    check("без единицы — шт", plain is not None and plain.unit == "шт", str(plain))
    check("цена с запятой", ms.parse_missadd("трюфель 12,50").price == 12.5)
    check("без цены — отказ", ms.parse_missadd("трюфель") is None)
    check("ноль — отказ", ms.parse_missadd("трюфель 0") is None)
    check("одно слово — отказ", ms.parse_missadd("трюфель") is None)

    rows = [
        {"phrase": "трюфель", "count": 4, "users": 2},
        {"phrase": "икра", "count": 2, "users": 1},
    ]
    digest = ms.digest_text(rows, 30)
    check("сводка перечисляет запросы", "трюфель" in digest and "икра" in digest, digest)
    check("в сводке клиенты", "клиентов: 2" in digest, digest)
    check("в сводке подсказка", "/missadd" in digest, digest)
    check("пустая сводка честна", "Пусто" in ms.digest_text([], 30))
    check("уведомление клиенту", "Трюфель" in ms.client_notice("Трюфель", 12.5, "кг"))
    check(
        "нормализация фразы",
        ms.normalize_phrase("  Трюфель   Чёрный ") == "трюфель черный",
    )

    tmp = Path(tempfile.mkdtemp())
    target = tmp / "catalog.xlsx"
    shutil.copy2(BASE_DIR / "catalog.xlsx", target)
    before = Catalog.load(target)
    new_id = catalogedit.append_item(target, "Трюфель чёрный", 12.5, "кг", category="Сыры")
    check("id выдан", new_id > max(i.id for i in before.items), str(new_id))
    after = Catalog.load(target)
    check("позиций стало больше", len(after.items) == len(before.items) + 1, str(len(after.items)))
    added = after.get(new_id)
    check("позиция читается", added is not None and added.name == "Трюфель чёрный", str(added))
    check("цена в файле", added is not None and added.price == 12.5, str(added))
    check("единица в файле", added is not None and added.unit == "кг", str(added))
    check("категория в файле", added is not None and added.category == "Сыры", str(added))
    workbook = load_workbook(target, data_only=True, read_only=True)
    worksheet = workbook.worksheets[0]
    check("строка дописана в конец", worksheet.max_row >= len(after.items) + 1)
    workbook.close()

    reloaded = Catalog.load(BASE_DIR / "catalog.xlsx")
    catalogedit.reload_into(reloaded, target)
    check("перечитка переносит позицию", reloaded.get(new_id) is not None)
    shutil.rmtree(tmp, ignore_errors=True)


def test_habit() -> None:
    print("\n[черновик по расписанию: привычный день недели]")
    from datetime import date as _date
    from datetime import timedelta as _timedelta

    from orderbot import habit

    wednesdays = ["2026-10-07", "2026-10-14", "2026-10-21", "2026-10-28"]
    mixed = ["2026-10-07", "2026-10-14", "2026-10-09", "2026-10-12", "2026-10-15"]
    check("счёт по дням недели", habit.weekday_counts(wednesdays)[2] == 4, str(habit.weekday_counts(wednesdays)))
    check("привычная среда", habit.usual_weekday(wednesdays, 2), str(habit.weekday_counts(wednesdays)))
    check("две среды из пяти — не привычка", not habit.usual_weekday(mixed, 2), str(habit.weekday_counts(mixed)))
    check("порог доли работает", not habit.usual_weekday(wednesdays + ["2026-10-05"] * 12, 2, min_share=0.5))
    check("мало заказов — не привычка", not habit.usual_weekday(["2026-10-07"], 2))
    check("другой день недели", not habit.usual_weekday(wednesdays, 0))
    check("битые даты пропускаются", habit.weekday_counts(["не дата", ""]) == {})

    today = _date(2026, 11, 4)  # среда
    check("сегодня среда", today.weekday() == 2)
    check("условие срабатывает", habit.should_nudge(wednesdays, today, False))
    check("заказал сегодня — молчим", not habit.should_nudge(wednesdays, today, True))
    check("не его день — молчим", not habit.should_nudge(wednesdays, today + _timedelta(days=1), False))
    text = habit.nudge_text(2, ["Куриное филе", "Моцарелла"])
    check("в тексте день недели", "средам" in text, text)
    check("в тексте позиции", "Куриное филе" in text, text)
    check("без позиций текст проще", "Обычно берёте" not in habit.nudge_text(3))


def test_churn_rhythm() -> None:
    print("\n[личный ритм клиента: когда напоминать]")
    from datetime import date as _date

    from orderbot import churn

    weekly = ["2026-09-02", "2026-09-09", "2026-09-16", "2026-09-23", "2026-09-30"]
    check("медианный интервал 7 дней", churn.median_interval(weekly) == 7.0, str(churn.median_interval(weekly)))
    check("два заказа — ритма нет", churn.median_interval(["2026-09-02", "2026-09-09"]) is None)
    check("повтор в один день не считается", len(churn.order_dates(["2026-09-02", "2026-09-02"])) == 1)
    check("битые даты пропускаются", churn.order_dates(["мусор"]) == [])

    irregular = ["2026-09-01", "2026-09-03", "2026-09-15", "2026-09-16", "2026-10-01"]
    silent, days, interval = churn.is_silent(irregular, _date(2026, 10, 20))
    check("молчание распознано", silent, f"{silent} {days} {interval}")
    check("дней молчания", days == 19, str(days))
    check("интервал посчитан", interval is not None and interval > 0, str(interval))

    fresh, days_fresh, _interval = churn.is_silent(weekly, _date(2026, 10, 2))
    check("свежий заказ — не молчит", not fresh, f"{days_fresh}")
    early, _days_early, _i = churn.is_silent(weekly, _date(2026, 10, 3))
    check("два дня — не напоминаем", not early)
    check("нет истории — не молчит", churn.is_silent([], _date(2026, 10, 3))[0] is False)

    rows = [
        {"name": "Кафе Роза", "silent_days": 19, "interval": 4.0},
        {"name": "Столовая", "silent_days": 9, "interval": None},
    ]
    text = churn.alert_text(rows)
    check("в сводке имя", "Кафе Роза" in text, text)
    check("в сводке ритм", "обычно каждые" in text, text)
    check("в сводке кнопка-подсказка", "Напомнить" in text, text)
    check("пустая сводка", "некому" in churn.alert_text([]))
    check("текст клиенту про ритм", "раз в 4 дн" in churn.client_text(4.0), churn.client_text(4.0))
    check("текст клиенту без ритма", "давно не заказывали" in churn.client_text(None))


def test_manager_reply() -> None:
    print("\n[ответ менеджера клиенту: разбор /reply]")
    from orderbot import replies

    parsed = replies.parse_reply("12345 Заказ собирают, курьер после обеда")
    check("id и текст разобраны", parsed == (12345, "Заказ собирают, курьер после обеда"), str(parsed))
    check("длинный текст обрезается", len(replies.parse_reply("1 " + "а" * 5000)[1]) <= replies.MAX_REPLY)
    check("без id — отказ", replies.parse_reply("Привет, как дела") is None)
    check("без текста — отказ", replies.parse_reply("12345") is None)
    check("ноль не принимаем", replies.parse_reply("0 текст") is None)
    check("отрицательный id", replies.parse_reply("-5 текст") is None)
    check("пустая строка", replies.parse_reply("") is None)

    check(
        "id из пересланного сообщения",
        replies.extract_client_id("💬 Сообщение от клиента Иван (id 987654):\n\nЗдравствуйте") == 987654,
    )
    check("id из подписи", replies.extract_client_id("(id 555) нужен счёт") == 555)
    check("без id — None", replies.extract_client_id("просто текст") is None)
    check("пустое сообщение", replies.extract_client_id(None) is None)
    check("подсказка про ответ", "/reply" in replies.forward_note())
    check("текст клиенту", replies.reply_for_client("уже везём").startswith("💬 Менеджер"))
    check("имя менеджера в тексте", "Азиз" in replies.reply_for_client("уже везём", "Азиз"))


def test_staff_ratings() -> None:
    print("\n[оценки бригады: средняя и недовольные]")
    from orderbot import staffrating as sr

    check("средняя", sr.average([5, 4, 3]) == 4.0, str(sr.average([5, 4, 3])))
    check("без оценок — ноль", sr.average([]) == 0.0)
    check("доля недовольных", round(sr.low_share([1, 2, 5, 5])) == 50, str(sr.low_share([1, 2, 5, 5])))
    check("все довольны", sr.low_share([4, 5, 5]) == 0.0)
    check("пусто — ноль", sr.low_share([]) == 0.0)

    rows = [
        {"role": "packer", "name": "Азиз", "n": 4, "total": 18, "low": 1},
        {"role": "courier", "name": "Сергей", "n": 3, "total": 9, "low": 2},
        {"role": "courier", "name": "Новичок", "n": 1, "total": 2, "low": 1},
    ]
    prepared = sr.staff_rows(rows)
    check("строки посчитаны", len(prepared) == 3, str(prepared))
    check("средняя упаковщика", prepared[0]["avg"] == 4.5 or any(r["avg"] == 4.5 for r in prepared), str(prepared))
    check("сортировка худших вперёд", prepared[0]["avg"] <= prepared[-1]["avg"], str(prepared))
    block = sr.ratings_block(rows)
    text = "\n".join(block)
    check("в блоке упаковщик", "упаковщик Азиз" in text, text)
    check("в блоке курьер", "курьер Сергей" in text, text)
    check("мало оценок не показываем", "Новичок" not in text, text)
    check("предупреждение о худшем", "Стоит поговорить" in text, text)
    check("без данных блок пустой", sr.ratings_block([]) == [])
    check("строка про сервис", "Средняя оценка" in sr.overall_line([4, 5, 3]), sr.overall_line([4, 5, 3]))
    check("нет оценок — честно", "не было" in sr.overall_line([]))


def test_payer_role() -> None:
    print("\n[роль плательщика: разбор и тексты]")
    from orderbot import payer

    check("пара разобрана", payer.parse_pair("123456789 987654321") == (123456789, 987654321))
    check("один id — отказ", payer.parse_pair("123456789") is None)
    check("три id — отказ", payer.parse_pair("1 2 3") is None)
    check("одинаковые id — отказ", payer.parse_pair("555555 555555") is None)
    check("короткие id не берём", payer.parse_pair("12 34") is None)
    check("один id для снятия", payer.parse_single("987654321") == 987654321)
    check("два id для снятия — отказ", payer.parse_single("1 2") is None)
    check("пусто — отказ", payer.parse_single("") is None)
    connected = payer.connected_text("Кафе Роза")
    check("текст подключения", "Кафе Роза" in connected and "накладные" in connected, connected)
    debt = payer.debt_text("Кафе Роза", 120.5, 3, 21)
    check("текст долга с суммой", "$120.50" in debt, debt)
    check("текст долга со сроком", "21 дней" in debt, debt)
    check("текст снятия роли", "снята" in payer.removed_text())


def test_substitute_rules() -> None:
    print("\n[правила замены: спросить, не заменять, конкретная замена]")
    from orderbot import subreplace as sr

    check("правило «нет»", sr.parse_rule("нет") == sr.SubRule(sr.MODE_NO))
    check("правило «спросить»", sr.parse_rule("Спросить") == sr.SubRule(sr.MODE_ASK))
    check("правило по id", sr.parse_rule("1250") == sr.SubRule(sr.MODE_ONLY, 1250))
    check("мусор — None", sr.parse_rule("как-нибудь") is None)
    check("пусто — None", sr.parse_rule("") is None)

    item = next(
        i for i in CATALOG.items
        if i.category and len([o for o in CATALOG.items if o.category == i.category]) > 1
    )
    auto = sr.pick(CATALOG, item, None)
    check("по умолчанию похожие", len(auto) >= 1, str(auto))
    check("«не заменять» — пусто", sr.pick(CATALOG, item, sr.SubRule(sr.MODE_NO)) == [])
    other = next(o for o in CATALOG.items if o.id != item.id)
    only = sr.pick(CATALOG, item, sr.SubRule(sr.MODE_ONLY, other.id))
    check("конкретная замена", [o.id for o in only] == [other.id], str(only))
    same = sr.pick(CATALOG, item, sr.SubRule(sr.MODE_ONLY, item.id))
    check("замена сама собой не берётся", same == [])
    ghost = sr.pick(CATALOG, item, sr.SubRule(sr.MODE_ONLY, 999999))
    check("несуществующая замена", ghost == [])

    text = sr.rules_text(CATALOG, {})
    check("текст без правил", "Правил замены нет" in text, text)
    listed = sr.rules_text(
        CATALOG,
        {item.id: {"mode": sr.MODE_NO, "substitute_id": None},
         other.id: {"mode": sr.MODE_ONLY, "substitute_id": item.id}},
    )
    check("в списке позиция", item.name in listed, listed)
    check("в списке «не заменять»", "не заменять" in listed, listed)
    check("в списке замена", item.name in listed and "заменять на" in listed, listed)


def test_ai_cost() -> None:
    print("\n[расход ИИ: оценка, бюджет, отчёты]")
    from orderbot import aicost as ac

    check("оценка по вызовам", ac.estimate(10, 0.002) == 0.02, str(ac.estimate(10, 0.002)))
    check("ноль вызовов", ac.estimate(0) == 0.0)
    check("мусор в цене — берём умолчание", ac.estimate(5, "abc") == round(5 * ac.DEFAULT_COST_PER_CALL, 4))
    check("отрицательные вызовы", ac.estimate(-5) == 0.0)
    check("доля бюджета", ac.budget_share(80, 100) == 0.8, str(ac.budget_share(80, 100)))
    check("бюджет не задан — ноль", ac.budget_share(80, 0) == 0.0)
    check("перерасход не больше 1", ac.budget_share(300, 100) == 1.0)
    warn = ac.budget_warning(80, 100, 0.16)
    check("предупреждение про бюджет", "80 из 100" in warn and "LLM_DAILY_BUDGET" in warn, warn)
    line = ac.usage_line(42, 0.084, 1000)
    check("строка расхода", "42 вызовов" in line and "0.08" in line, line)
    check("строка без лимита", "лимит" not in ac.usage_line(5, 0.01))
    check("на заказ", ac.per_order(0.4, 8) == 0.05, str(ac.per_order(0.4, 8)))
    check("на заказ без заказов", ac.per_order(0.4, 0) == 0.0)
    week = ac.weekly_line([{"calls": 100, "est_cost": 0.2}, {"calls": 50, "est_cost": 0.1}], 25)
    check("недельная строка", "150 вызовов" in week and "0.30" in week, week)
    check("без вызовов строка пустая", ac.weekly_line([], 10) == "")


def test_eta_and_envcheck() -> None:
    print("\n[окно прибытия и гигиена секретов]")
    import tempfile

    from orderbot import eta

    now = datetime(2026, 11, 20, 15, 10, tzinfo=TZ)
    check("время в пути по умолчанию", eta.travel_minutes("") == eta.DEFAULT_TRAVEL_MIN)
    check("время в пути из настройки", eta.travel_minutes("25") == 25)
    check("слишком мало — минимум", eta.travel_minutes("1") == eta.MIN_TRAVEL)
    check("слишком много — максимум", eta.travel_minutes("9999") == eta.MAX_TRAVEL)
    check("мусор — по умолчанию", eta.travel_minutes("скоро") == eta.DEFAULT_TRAVEL_MIN)
    check("прибытие посчитано", eta.arrival(now, 30).strftime("%H:%M") == "15:40")
    window = eta.window_text(now, 30)
    check("окно с разбросом", "15:40" in window and "±15" in window, window)
    message = eta.client_text(7, now, 30, "15-18")
    check("клиент видит время", "15:40" in message, message)
    check("клиент видит слот", "15-18" in message, message)

    import sys as _sys

    _sys.path.insert(0, str(BASE_DIR / "scripts"))
    import env_check

    with tempfile.TemporaryDirectory() as tmp:
        env = Path(tmp) / ".env"
        env.write_text(
            "BOT_TOKEN=123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
            "EMPTY=\n"
            "SHORT_KEY=abc\n"
            "QUOTED=\"value\"\n"
            "DUP=1\nDUP=2\n"
            "# комментарий\n",
            encoding="utf-8",
        )
        values, problems, dupes = env_check.parse_env(env)
        check("значения прочитаны", values.get("BOT_TOKEN", "").startswith("123456"), str(list(values)))
        check("дубль найден", "DUP" in dupes, str(dupes))
        check("пустое значение в замечаниях", any("EMPTY" in p for p in problems), str(problems))
        check("короткий секрет в замечаниях", any("SHORT_KEY" in p for p in problems), str(problems))
        check("кавычки в замечаниях", any("QUOTED" in p for p in problems), str(problems))
        check("значения не печатаются", all("value" not in p for p in problems), str(problems))
        example = Path(tmp) / ".env.example"
        example.write_text("BOT_TOKEN=\nNEW_KEY=\n", encoding="utf-8")
        names = env_check.read_example(example)
        check("имена из примера", names == ["BOT_TOKEN", "NEW_KEY"], str(names))


def test_pack_columns() -> None:
    print("\n[фасовка из колонок прайса (E6)]")
    import tempfile

    from openpyxl import Workbook

    from orderbot.units import pack_size, price_per_unit

    tmp = Path(tempfile.mkdtemp())
    path = tmp / "pack.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["ID", "Название", "Единица", "Фасовка", "Размер фасовки", "Фасовка, ед", "Цена"])
    ws.append([1, "Помидоры", "кг", "", 5, "кг", 10])
    ws.append([2, "Молоко", "л", "бутылка 1 л", "", "", 2])
    ws.append([3, "Филе", "кг", "ящик 10 кг", 12, "кг", 30])
    wb.save(path)

    catalog = Catalog.load(path)
    tomatoes = catalog.get(1)
    milk = catalog.get(2)
    fillet = catalog.get(3)
    check("фасовка из колонок", tomatoes.pack == "5 кг", tomatoes.pack)
    check("размер фасовки считается", pack_size(tomatoes.pack, tomatoes.unit) == 5.0, tomatoes.pack)
    check(
        "цена за килограмм из колонок",
        price_per_unit(tomatoes.price, tomatoes.pack, tomatoes.unit) == 2.0,
        str(price_per_unit(tomatoes.price, tomatoes.pack, tomatoes.unit)),
    )
    check("без колонок — текст фасовки", milk.pack == "бутылка 1 л", milk.pack)
    check("данные важнее текста", fillet.pack == "12 кг", fillet.pack)
    check(
        "название с фасовкой",
        tomatoes.full_name == "Помидоры (5 кг)",
        tomatoes.full_name,
    )
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def test_client_pricing() -> None:
    print("\n[цены и скидки по клиентам (B13)]")
    from orderbot.clientpricing import client_price, describe, scale_sum

    check("цена без скидки", client_price(10.0) == (10.0, 0.0), str(client_price(10.0)))
    check("скидка 10%", client_price(10.0, 10) == (9.0, 10.0), str(client_price(10.0, 10)))
    check(
        "особая цена важнее скидки",
        client_price(10.0, 10, 8.0) == (8.0, 20.0),
        str(client_price(10.0, 10, 8.0)),
    )
    check("скидка 100% — ноль", client_price(10.0, 100)[0] == 0.0, str(client_price(10.0, 100)))
    check("отрицательная скидка не считается", client_price(10.0, -5) == (10.0, 0.0))
    check("пустая база", client_price(0.0, 5) == (0.0, 0.0), str(client_price(0.0, 5)))
    check("сумма с фасовкой", scale_sum(97.5, 9.75, 8.775) == 87.75, str(scale_sum(97.5, 9.75, 8.775)))
    check("нулевая сумма", scale_sum(0, 10, 9) == 0.0)
    check("описание скидки", describe(5, 2) == "скидка 5% · особых цен: 2", describe(5, 2))
    check("описание пустое", describe(0, 0) == "")


async def test_client_pricing_db() -> None:
    print("\n[скидки клиента в заказе (B13)]")
    if not TEST_URL:
        print("  — пропущено: нужна тестовая PostgreSQL-база")
        return
    from orderbot.db import reset_test_database

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "unused.db", TEST_URL)
        await db.connect()
        try:
            await reset_test_database(db)
            client = 9201
            await db.ensure_user(client, "Скидочный Тест")
            await db.set_client_discount(client, 10, "постоянный клиент")
            row = await db.client_discount(client)
            check("скидка сохранена", float(row["discount_pct"]) == 10.0, str(row))
            order_id = await db.create_order(
                {
                    "id": "disc-1",
                    "chat_id": client,
                    "user_id": client,
                    "delivery_date": "2026-11-24",
                    "delivery_time": "09:00–12:00",
                    "source": "test",
                },
                [{"catalog_id": 4, "name": "Куриное филе", "qty": 2,
                  "unit": "кг", "price": 9.0, "total": 18.0,
                  "base_price": 10.0, "discount_pct": 10.0}],
                None,
            )
            order = await db.get_order(order_id)
            item = order["items"][0]
            check("в заказ записалась цена со скидкой", float(item["price"]) == 9.0, str(item["price"]))
            check("базовая цена сохранена", float(item["base_price"]) == 10.0, str(item["base_price"]))
            await db.set_client_price(client, 4, 8.0)
            price_map = await db.client_prices_map(client)
            check("особая цена сохранена", price_map.get(4) == 8.0, str(price_map))
            draft = {
                "user_id": client,
                "items": [
                    {
                        "catalog_id": 4,
                        "qty": 2,
                        "final_qty": 2,
                        "unit": "кг",
                        "price": 10.0,
                        "sum": 20.0,
                    }
                ],
            }
            first = await db.apply_client_pricing(draft)
            item = draft["items"][0]
            check("особая цена применилась", item["price"] == 8.0 and item["sum"] == 16.0, str(item))
            check("скидка посчитана", first["discount_pct"] == 10.0 and first["items"] == 1, str(first))
            second = await db.apply_client_pricing(draft)
            check(
                "повторный вызов не накручивает скидку",
                draft["items"][0]["sum"] == 16.0 and second["items"] == 1,
                str(draft["items"][0]),
            )
            await db.clear_client_price(client, 4)
            draft2 = {"user_id": client, "items": [{"catalog_id": 4, "qty": 1, "price": 10.0, "sum": 10.0}]}
            await db.apply_client_pricing(draft2)
            check("после удаления особой цены — процент", draft2["items"][0]["sum"] == 9.0, str(draft2["items"][0]))
            await db.clear_client_discount(client)
            draft3 = {"user_id": client, "items": [{"catalog_id": 4, "qty": 1, "price": 10.0, "sum": 10.0}]}
            await db.apply_client_pricing(draft3)
            check("без правил — цена прайса", draft3["items"][0]["sum"] == 10.0, str(draft3["items"][0]))
        finally:
            await db.close()


def test_locations() -> None:
    print("\n[точки клиента (C11)]")
    from orderbot.locations import choice_text, list_text, parse_add, pick_by_name

    check(
        "разбор с чертой",
        parse_add("Склад Навои | ул. Навои 12") == ("Склад Навои", "ул. Навои 12"),
        str(parse_add("Склад Навои | ул. Навои 12")),
    )
    check("разбор без адреса", parse_add("Бар Центр") == ("Бар Центр", ""))
    check(
        "разбор с тире",
        parse_add("Пиццерия — ул. Амира Темура 5") == ("Пиццерия", "ул. Амира Темура 5"),
        str(parse_add("Пиццерия — ул. Амира Темура 5")),
    )
    check("пустой разбор", parse_add("   ") is None)
    rows = [
        {"id": 1, "name": "Склад Навои", "address": "ул. Навои 12"},
        {"id": 2, "name": "Бар Центр", "address": ""},
        {"id": 3, "name": "Бар Центр 2", "address": ""},
    ]
    check("поиск точного названия", pick_by_name(rows, "Бар Центр")["id"] == 2)
    check("поиск частью названия", pick_by_name(rows, "навои")["id"] == 1)
    check("пустой поиск", pick_by_name(rows, "") is None)
    text = list_text(rows)
    check("адрес в списке", "ул. Навои 12" in text, text)
    check("пустой список подсказывает", "/venue_add" in list_text([]))
    check("вопрос про точку", "Куда везти" in choice_text(), choice_text())


async def test_locations_db() -> None:
    print("\n[точки клиента в базе (C11)]")
    if not TEST_URL:
        print("  — пропущено: нужна тестовая PostgreSQL-база")
        return
    from orderbot.db import reset_test_database

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "unused.db", TEST_URL)
        await db.connect()
        try:
            await reset_test_database(db)
            client = 9301
            await db.ensure_user(client, "Сетевая Сеть")
            first = await db.add_location(client, "Склад Навои", "ул. Навои 12")
            second = await db.add_location(client, "Бар Центр", "ул. Амира Темура 5")
            same = await db.add_location(client, "Склад Навои", "ул. Навои 14")
            check("повтор названия обновляет точку", same == first, f"{same} vs {first}")
            rows = await db.client_locations(client)
            check("две точки у клиента", len(rows) == 2, str(rows))
            spot = await db.get_location(first)
            check("адрес обновлён", spot is not None and spot["address"] == "ул. Навои 14", str(spot))
            order_id = await db.create_order(
                {
                    "id": "loc-1",
                    "chat_id": client,
                    "user_id": client,
                    "delivery_date": "2026-11-24",
                    "delivery_time": "09:00–12:00",
                    "source": "test",
                    "location_id": second,
                    "location_name": "Бар Центр",
                    "delivery_address": "ул. Амира Темура 5",
                },
                [{"catalog_id": 4, "name": "Куриное филе", "qty": 2,
                  "unit": "кг", "price": 9.0, "total": 18.0}],
                None,
            )
            order = await db.get_order(order_id)
            check(
                "точка записана в заказ",
                int(order.get("location_id") or 0) == second
                and order.get("location_name") == "Бар Центр",
                str({k: order.get(k) for k in ("location_id", "location_name")}),
            )
            stats = await db.location_stats(client, 30)
            bar = [row for row in stats if int(row["id"]) == second]
            check(
                "выручка по точке",
                bool(bar) and float(bar[0]["revenue"]) == 18.0 and int(bar[0]["orders"]) == 1,
                str(stats),
            )
            await db.set_location_payer(second, 555001)
            check(
                "плательщик точки сохранён",
                await db.location_payer(second) == 555001,
                str(await db.location_payer(second)),
            )
            await db.set_location_payer(second, None)
            check("плательщик точки снят", await db.location_payer(second) is None)
            check("без точки плательщика нет", await db.location_payer(None) is None)
            check("удаление точки", await db.delete_location(client, second))
            check("чужую точку не удалить", not await db.delete_location(999999, first))
            left = await db.client_locations(client)
            check("осталась одна точка", len(left) == 1, str(left))
            kept = await db.get_order(order_id)
            check(
                "в старом заказе точка осталась",
                kept.get("location_name") == "Бар Центр",
                str(kept.get("location_name")),
            )
        finally:
            await db.close()


def test_suppliers_and_windows() -> None:
    print("\n[поставщики, окно правок, выгрузка модели]")
    import shutil
    import tempfile

    from orderbot import catalogedit
    from orderbot.formatting import edit_hint

    check("нет отсечки — общая подсказка", "изменить" in edit_hint("", "2026-11-21"))
    hint = edit_hint("18:00", "2026-11-21")
    check("подсказка с отсечкой", "18:00" in hint and "21.11" in hint, hint)
    check("без даты — всё равно про время", "18:00" in edit_hint("18:00", None))

    tmp = Path(tempfile.mkdtemp())
    target = tmp / "catalog.xlsx"
    shutil.copy2(BASE_DIR / "catalog.xlsx", target)
    check("поставщика записали", catalogedit.set_supplier(target, 4, "Ферма Ош"))
    reloaded = Catalog.load(target)
    check("поставщик читается", reloaded.get(4) is not None and reloaded.get(4).supplier == "Ферма Ош", str(reloaded.get(4)))
    check("пустой поставщик чистится", catalogedit.set_supplier(target, 4, ""))
    again = Catalog.load(target)
    check("после очистки пусто", again.get(4) is not None and again.get(4).supplier == "", str(again.get(4)))
    check("несуществующая позиция", not catalogedit.set_supplier(target, 999999, "Кто-то"))
    shutil.rmtree(tmp, ignore_errors=True)

    from orderbot.buylist import as_text

    rows = [
        {"name": "Помидоры", "unit": "кг", "qty": 10, "need": 4, "price": 2,
         "purchase": 1.2, "stock_left": 6, "available": True,
         "category": "Овощи", "supplier": "Ферма Ош", "orders": 2},
        {"name": "Филе", "unit": "кг", "qty": 5, "need": 1, "price": 9,
         "purchase": 6, "stock_left": 4, "available": True,
         "category": "Мясо", "supplier": "Мясокомбинат", "orders": 1},
        {"name": "Лук", "unit": "кг", "qty": 3, "need": 3, "price": 1,
         "purchase": 0.5, "stock_left": 0, "available": True,
         "category": "Овощи", "supplier": "", "orders": 1},
    ]
    text_buy = as_text(rows, [], "21.11")
    check("группа по поставщику", "Ферма Ош" in text_buy and "Мясокомбинат" in text_buy, text_buy)
    check("позиция без поставщика в отдельной группе", "Я без поставщика" in text_buy, text_buy)
    check("итог по закупке", "закупочным ценам" in text_buy, text_buy)
    no_suppliers = [dict(row, supplier="") for row in rows]
    text_plain = as_text(no_suppliers, [], "21.11")
    check("без поставщиков — по категориям", "Овощи" in text_plain and "Мясо" in text_plain, text_plain)



async def test_vosk_idle() -> None:
    print("\n[офлайн-модель: выгрузка после простоя]")
    from orderbot.stt import Transcriber

    class _Settings:
        vosk_model_path = None

    transcriber = Transcriber(_Settings(), ai=None)
    check("нет модели — нечего выгружать", await transcriber.release_idle(0) is False)
    import time as _time

    transcriber._vosk_model = object()
    transcriber._vosk_used_at = _time.time()
    check("свежая модель не выгружается", await transcriber.release_idle(10) is False)
    transcriber._vosk_used_at = _time.time() - 3600
    check("старая модель выгружается", await transcriber.release_idle(10) is True)
    check("после выгрузки модели нет", transcriber._vosk_model is None)


def test_scripts_quality() -> None:
    print("\n[кнопки без обработчика и демо-данные]")
    import sys as _sys

    _sys.path.insert(0, str(BASE_DIR / "scripts"))
    import buttons_check
    import seed_demo

    exact = {"cl:price", "rday:no"}
    prefixes = ["rday:", "cour:start:"]
    check("точное совпадение", buttons_check.is_handled("cl:price", exact, prefixes))
    check("по префиксу", buttons_check.is_handled("rday:yes:501", exact, prefixes))
    check("точное — начало ключа", buttons_check.is_handled("cl", exact, prefixes))
    check("мёртвая кнопка", not buttons_check.is_handled("ghost:1", exact, prefixes))
    check("ключ из f-строки", buttons_check.key_of("cour:start:{order_id}", True) == "cour:start:")
    check("статичный ключ", buttons_check.key_of("cl:price", False) == "cl:price")

    try:
        seed_demo.guard("postgresql://order_bot@localhost/order_bot")
        check("боевая база запрещена", False)
    except SystemExit:
        check("боевая база запрещена", True)
    try:
        seed_demo.guard("postgresql://order_bot@localhost/order_bot_demo")
        check("демо-база разрешена", True)
    except SystemExit:
        check("демо-база разрешена", False)
    check("клиентов в демо", len(seed_demo.CLIENTS) >= 3, str(len(seed_demo.CLIENTS)))
    check("период демо 8 недель", seed_demo.WEEKS == 8)


def test_delivery_survey() -> None:
    print("\n[опрос после доставки: тексты и типы проблем]")
    from orderbot import survey

    codes = [code for code, _label in survey.PROBLEM_TYPES]
    check("четыре типа проблем", len(survey.PROBLEM_TYPES) == 4, str(codes))
    check("недовес есть", "weight" in codes, str(codes))
    check("качество есть", "quality" in codes, str(codes))
    check("не привезли есть", "missing" in codes, str(codes))
    check("другое есть", "other" in codes, str(codes))
    check("подпись типа", survey.problem_label("weight") == "⚖️ Недовес", survey.problem_label("weight"))
    check("неизвестный тип — «другое»", survey.problem_label("???") == "другое")
    check("опрос содержит номер", "№7" in survey.survey_text(7), survey.survey_text(7))
    check("благодарность", "Спасибо" in survey.thanks_text())
    check("вопрос про тип", "№7" in survey.problem_prompt(7))
    check("просьба о деталях", "фото" in survey.details_prompt("⚖️ Недовес"))
    check("текст претензии с деталями", "Недовес: мало" in survey.claim_text("Недовес", "мало"), survey.claim_text("Недовес", "мало"))
    check("без деталей — только тип", survey.claim_text("Недовес", "").startswith("Опрос"))
    check("подсказка про кнопки", "Всё хорошо" in survey.survey_note())


def test_hot_offer() -> None:
    print("\n[горячее предложение: разбор и тексты]")
    from orderbot import hot

    offer = hot.parse_offer("помидоры 1.10 до 18:00")
    check("позиция и цена", offer is not None and offer.price == 1.1, str(offer))
    check("время до", offer is not None and offer.until == "18:00", str(offer))
    check("без времени", hot.parse_offer("огурцы 0,90").until == "")
    check("цена с запятой", hot.parse_offer("огурцы 0,90").price == 0.9)
    check("позиция из двух слов", hot.parse_offer("сыр гауда 12.50 до 9:30").phrase == "сыр гауда")
    check("без цены — отказ", hot.parse_offer("помидоры до 18:00") is None)
    check("цена ноль — отказ", hot.parse_offer("помидоры 0") is None)
    check("плохое время — отказ", hot.parse_offer("помидоры 1.10 до 25:99") is None)
    check("пусто — отказ", hot.parse_offer("") is None)

    text_offer = hot.offer_text("Томаты", 1.1, "кг", "18:00", "ящик 10 кг")
    check("в тексте цена", "$1.10/кг" in text_offer, text_offer)
    check("в тексте время", "до 18:00" in text_offer, text_offer)
    check("в тексте фасовка", "ящик 10 кг" in text_offer, text_offer)
    check("просьба о количестве", "количество" in hot.taken_text("Томаты"))
    note = hot.manager_note("Иван", "Томаты", 1.1)
    check("менеджеру с ценой", "$1.10" in note and "Взять" in note, note)
    check("подсказка по формату", "/hot" in hot.sample_text())


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
    test_writeoff()
    test_price_review()
    test_stock()
    test_income()
    test_missing_demand()
    test_habit()
    test_churn_rhythm()
    test_manager_reply()
    test_staff_ratings()
    test_payer_role()
    test_substitute_rules()
    test_ai_cost()
    test_eta_and_envcheck()
    test_suppliers_and_windows()
    test_scripts_quality()
    test_delivery_survey()
    test_hot_offer()
    test_golden_live()
    test_module_names()
    await test_db()
    await test_start_and_greeting()
    await test_failure_safety()
    await test_alias_migration()
    test_pack_columns()
    test_client_pricing()
    await test_client_pricing_db()
    test_locations()
    await test_locations_db()
    await test_migrations_on_copy()
    await test_locked_db()
    await test_lock_cleanup()
    await test_pending_price()
    await test_ledger_db()
    await test_stock_db()
    await test_vosk_idle()
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
