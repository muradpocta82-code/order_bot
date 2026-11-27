"""Команды бота: /start, /help, /order, /health, /export и другие (I1).

Вынесено из handlers.py. Здесь только команды и вспомогательные страницы
списков; поток заказа с вопросами остаётся в ядре. Роутер подключается в
handlers.py перед приёмом заказа, поэтому порядок обработки прежний.

Ядро берёт отсюда `_orders_page`, `_courier_page`, `_ai_usage_line`,
`_remember_invite` и `cmd_start` — импорт в конце handlers.py.
"""
from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timedelta
from math import ceil
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import FSInputFile, Message

from .ai import AIClient
from .catalog import Catalog, diff_catalogs
from .config import Settings
from .db import Database
from .formatting import (
    esc,
    format_catalog,
    format_client_order,
    format_order,
    format_orders_page,
    money,
)
from .keyboards import (
    client_back_keyboard,
    client_menu_keyboard,
    client_order_keyboard,
    client_reply_keyboard,
    courier_back_keyboard,
    courier_panel_keyboard,
    courier_status_keyboard,
    manager_back_keyboard,
    manager_panel_keyboard,
    manager_status_keyboard,
    orders_list_keyboard,
    price_undo_keyboard,
)
from .notify import _courier_panel_text, _manager_panel_text
from .onboarding import _profile_step
from .roles import _back_keyboard, _is_courier, _is_manager, _is_staff
from .sheets import append_rows as sheets_append
from .sheets import ready as sheets_ready
from .route import order_point
from .runtime import BOT_STARTED_AT, _last_error
from .stock import hidden_ids, resolve as resolve_stock
from .texts import (
    COURIER_HELP_TEXT,
    HELP_TEXT,
    MANAGER_HELP_TEXT,
    START_TEXT,
)
from .units import format_qty, plural

log = logging.getLogger(__name__)

router = Router(name="orderbot_commands")


# ---------------------------------------------------------------------------
# команды
# ---------------------------------------------------------------------------


async def _orders_page(db: Database, kind: str, page: int):
    statuses = (
        ("new", "accepted", "ready", "delivering") if kind == "inbox" else None
    )
    total = await db.count_orders(statuses)
    pages = max(1, ceil(total / 10))
    page = max(0, min(page, pages - 1))
    orders = await db.list_orders_page(limit=10, offset=page * 10, statuses=statuses)
    title = "📥 <b>Входящие заказы</b>" if kind == "inbox" else "📜 <b>История заказов</b>"
    return (
        format_orders_page(orders, page, pages, total, title),
        orders_list_keyboard(orders, kind, page, pages),
    )


async def _courier_page(db: Database, page: int):
    statuses = ("ready", "delivering")
    total = await db.count_orders(statuses)
    pages = max(1, ceil(total / 10))
    page = max(0, min(page, pages - 1))
    orders = await db.list_orders_page(limit=10, offset=page * 10, statuses=statuses)
    return (
        format_orders_page(orders, page, pages, total, "🚚 <b>Активные доставки</b>"),
        courier_panel_keyboard(orders, page, pages),
    )


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    db: Database,
    settings: Settings,
    catalog: Catalog,
    command: CommandObject | None = None,
) -> None:
    await db.set_chat_state(message.chat.id, "qty_input", "")
    await _remember_invite(message, db, settings, command)
    if _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer(
            await _manager_panel_text(db), reply_markup=manager_panel_keyboard()
        )
        return
    if _is_courier(settings, message.from_user.id, message.chat.id):
        text, keyboard = await _courier_page(db, 0)
        await message.answer(
            await _courier_panel_text(db) + "\n\n" + text,
            reply_markup=keyboard,
        )
        return
    user = await db.ensure_user(
        message.from_user.id,
        message.from_user.full_name,
        message.from_user.username,
    )
    if user.get("access") == "rejected":
        await message.answer("Доступ закрыт. Напишите менеджеру.")
        return
    if await _profile_step(message, db, user, catalog):
        return
    if user.get("access") == "pending":
        await message.answer("Ждём подтверждения от менеджера.")
        return
    usually = await db.usual_items(message.from_user.id)
    await message.answer(
        f"Здравствуйте! Заведение: <b>{esc(user['venue'])}</b>.\n\n{START_TEXT}",
        reply_markup=client_menu_keyboard(usually, catalog),
    )
    await message.answer(
        "Кнопки внизу помогут быстрее: «Мои заказы», «Как обычно», «Менеджер».",
        reply_markup=client_reply_keyboard(),
    )


async def _remember_invite(
    message: Message,
    db: Database,
    settings: Settings,
    command: CommandObject | None,
) -> None:
    """Ссылка-приглашение: /start ref_<id> запоминает, кто привёл клиента."""
    raw = ((command.args if command else None) or "").strip()
    if not raw.lower().startswith("ref_"):
        return
    referrer = raw[4:].strip()
    if not referrer.lstrip("-").isdigit():
        return
    user_id = int(message.from_user.id)
    if int(referrer) == user_id:
        return
    existing = await db.get_setting(f"ref:{user_id}", "")
    if existing:
        return
    await db.set_setting(f"ref:{user_id}", referrer)
    await db.set_setting(
        f"ref_at:{user_id}", datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    manager = settings.manager_chat_id
    if manager:
        try:
            await message.bot.send_message(
                int(manager),
                f"🤝 Новый клиент пришёл по приглашению {referrer}: "
                f"{esc(message.from_user.full_name)} (id {user_id}).",
            )
        except Exception:
            pass


@router.message(Command("invite"))
async def cmd_invite(message: Message, db: Database, settings: Settings) -> None:
    """Ссылка-приглашение для клиента: привёл друга — видно менеджеру."""
    if _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Ссылки клиентов смотрите командой /refs.")
        return
    me = await message.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
    await message.answer(
        "🤝 Пригласите коллегу из другого заведения:\n"
        f"{link}\n\n"
        "По этой ссылке он сразу попадёт к нам, а мы увидим, кто привёл."
    )


@router.message(Command("help"))
async def cmd_help(message: Message, settings: Settings) -> None:
    if _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer(
            MANAGER_HELP_TEXT, reply_markup=manager_back_keyboard()
        )
        return
    if _is_courier(settings, message.from_user.id, message.chat.id):
        await message.answer(
            COURIER_HELP_TEXT, reply_markup=courier_back_keyboard()
        )
        return
    await message.answer(HELP_TEXT, reply_markup=client_back_keyboard())


@router.message(Command("id"))
async def cmd_id(message: Message, settings: Settings) -> None:
    await message.answer(
        f"Ваш чат: <code>{message.chat.id}</code>\n"
        f"Пользователь: <code>{message.from_user.id}</code>",
        reply_markup=_back_keyboard(settings, message.from_user.id, message.chat.id),
    )


@router.message(Command("price"))
async def cmd_price(
    message: Message, catalog: Catalog, settings: Settings, db: Database
) -> None:
    rows = resolve_stock(catalog, await db.stock_overrides())
    text = format_catalog(catalog, hidden_ids(rows))
    keyboard = _back_keyboard(settings, message.from_user.id, message.chat.id)
    if len(text) <= 4000:
        await message.answer(text, reply_markup=keyboard)
        return
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3900:
            await message.answer(chunk, reply_markup=keyboard)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await message.answer(chunk, reply_markup=keyboard)


@router.message(Command("orders"))
async def cmd_orders(message: Message, db: Database, settings: Settings) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    text, keyboard = await _orders_page(db, "inbox", 0)
    await message.answer(text, reply_markup=keyboard)


@router.message(Command("deliveries"))
async def cmd_deliveries(
    message: Message, db: Database, settings: Settings
) -> None:
    if not _is_courier(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только доставщику.")
        return
    text, keyboard = await _courier_page(db, 0)
    await message.answer(text, reply_markup=keyboard)


@router.message(Command("export"))
async def cmd_export(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    raw = (command.args or "").strip()
    days = int(raw) if raw.isdigit() and 1 <= int(raw) <= 365 else 7
    now = datetime.now()
    start = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    # +1 секунда: заказ, созданный в эту же секунду, тоже попадает в отчёт.
    end = (now + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    orders = await db.orders_between(start, end)
    if not orders:
        await message.answer(f"За последние {days} дней заказов нет.")
        return
    venues = await db.venue_map(
        [int(order["chef_id"]) for order in orders if order.get("chef_id")]
    )
    try:
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Заказы"
        sheet.append(["Дата", "Заведение", "Позиции", "Сумма", "Статус"])
        for order in orders:
            venue = venues.get(int(order.get("chef_id") or 0)) or order.get("chef_name") or ""
            positions = "; ".join(
                f"{item.get('name')} — {format_qty(item.get('qty'))} "
                f"{plural(item.get('unit'), item.get('qty'))}"
                for item in order.get("items", [])
            )
            sheet.append(
                [
                    order.get("created_at"),
                    venue,
                    positions,
                    float(order.get("total") or 0),
                    order.get("status"),
                ]
            )
        # F2: если подключена таблица Google — дописываем те же строки туда.
        if sheets_ready(settings):
            rows = [
                [
                    str(order.get("created_at") or ""),
                    venues.get(int(order.get("chef_id") or 0))
                    or order.get("chef_name")
                    or "",
                    "; ".join(
                        f"{item.get('name')} — {format_qty(item.get('qty'))} "
                        f"{plural(item.get('unit'), item.get('qty'))}"
                        for item in order.get("items", [])
                    ),
                    float(order.get("total") or 0),
                    str(order.get("status") or ""),
                ]
                for order in orders
            ]
            await sheets_append(settings, rows)

        exports = settings.data_dir / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        path = exports / f"orders-{now.strftime('%Y%m%d-%H%M%S')}.xlsx"
        workbook.save(path)
    except Exception as exc:
        log.exception("Не удалось собрать Excel")
        await message.answer(f"Не получилось собрать файл: {type(exc).__name__}")
        return
    try:
        await message.bot.send_document(
            message.chat.id,
            FSInputFile(path),
            caption=f"Заказы за {days} дн. · {len(orders)} шт.",
        )
    finally:
        path.unlink(missing_ok=True)


@router.message(Command("price_undo"))
async def cmd_price_undo(
    message: Message, db: Database, settings: Settings, catalog: Catalog
) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    versions = await db.list_price_versions(5)
    if not versions:
        await message.answer("Сохранённых версий прайса пока нет.")
        return
    version = versions[0]
    path = Path(version["path"])
    if not path.exists():
        await message.answer("Файл последней версии не найден.")
        return
    try:
        restored = Catalog.load(path)
    except Exception:
        await message.answer("Файл версии повреждён.")
        return
    diff = diff_catalogs(restored, catalog)
    lines = [
        f"↩️ <b>Вернуть прайс от {esc(version['created_at'])}</b>",
        "",
        f"Позиций: {len(catalog.items)} → {len(restored.items)}",
        f"Изменились цены: {len(diff['price_changes'])}",
        f"Появятся: {len(diff['added'])}",
        f"Исчезнут: {len(diff['removed'])}",
    ]
    for previous, current in diff["price_changes"][:5]:
        lines.append(f"{esc(current.name)}: {money(previous.price)} → {money(current.price)}")
    await message.answer(
        "\n".join(lines), reply_markup=price_undo_keyboard(int(version["id"]))
    )


async def _ai_usage_line(db: Database, ai: AIClient, settings: Settings) -> str:
    """Строка «ИИ за сутки: N вызовов, ~$X» для /health (E4)."""
    try:
        from .aicost import estimate, usage_line

        today = datetime.now(settings.timezone).date().isoformat()
        rows = await db.llm_usage_day(today)
        calls = sum(int(row.get("calls") or 0) for row in rows)
        cost = sum(float(row.get("est_cost") or 0) for row in rows)
        calls = max(calls, sum(int(v) for v in ai.calls_today.values()))
        cost = max(cost, estimate(calls, getattr(settings, "llm_cost_per_call", 0.002)))
        budget = int(getattr(settings, "llm_daily_budget", 0) or 0)
        return "\n" + usage_line(calls, cost, budget) + (
            f"\nЗаблокировано по бюджету: {ai.budget_blocked}" if ai.budget_blocked else ""
        )
    except Exception:
        return ""


@router.message(Command("health"))
async def cmd_health(
    message: Message, db: Database, settings: Settings, ai: AIClient
) -> None:
    if not _is_manager(settings, message.from_user.id, message.chat.id):
        await message.answer("Команда доступна только менеджеру.")
        return
    uptime = int(time.time() - BOT_STARTED_AT)
    days, rest = divmod(uptime, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days} д")
    if hours or days:
        parts.append(f"{hours} ч")
    parts.append(f"{minutes} мин")
    labels = {
        "not_configured": "не задан",
        "ok": "работает",
        "lazy": "проверю при запросе",
        "quota": "лимит",
        "degraded": "сбои",
        "auth_failed": "ключ отклонён",
        "unreachable": "недоступен",
        "unknown": "не проверен",
    }
    pending = len(await db.list_unnotified_orders(500))
    pending_courier = len(await db.list_unnotified_delivery_orders(500))
    if _last_error["at"]:
        error_text = (
            time.strftime("%d.%m %H:%M", time.localtime(_last_error["at"]))
            + " — "
            + str(_last_error["text"])
        )
    else:
        error_text = "нет"
    size = await db.size_bytes()
    try:
        db_ms = await db.ping()
        db_line = f"БД: {'PostgreSQL' if db.is_postgres else 'SQLite'}, ответ {db_ms} мс\n"
    except Exception:
        db_line = f"БД: {'PostgreSQL' if db.is_postgres else 'SQLite'}, не отвечает\n"
    text = (
        "🩺 <b>Здоровье</b>\n\n"
        + db_line
        + f"Аптайм: {', '.join(parts)}\n"
        f"DeepSeek: {labels.get(ai.deepseek_status, ai.deepseek_status)}\n"
        f"Gemini: {labels.get(ai.gemini_status, ai.gemini_status)}\n"
        f"Ждут упаковщика: {pending}\n"
        f"Ждут доставщика: {pending_courier}\n"
        f"Последняя ошибка: {error_text}\n"
        f"Размер БД: {size / 1024 / 1024:.1f} МБ\n"
        f"Вызовов AI за сутки: DeepSeek {ai.calls_today.get('deepseek', 0)}, "
        f"Gemini {ai.calls_today.get('gemini', 0)}"
        + await _ai_usage_line(db, ai, settings)
    )
    await message.answer(text, reply_markup=manager_back_keyboard())


@router.message(Command("order"))
async def cmd_order(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Укажите номер: <code>/order 12</code>")
        return
    order = await db.get_order(int(command.args.strip()))
    if order is None:
        await message.answer("Заказ не найден.")
        return
    is_manager = _is_manager(settings, message.from_user.id, message.chat.id)
    is_courier = _is_courier(settings, message.from_user.id, message.chat.id)
    is_owner = order.get("chef_id") == message.from_user.id
    if not (is_manager or is_courier or is_owner):
        await message.answer("Нет доступа к этому заказу.")
        return
    if is_manager:
        keyboard = manager_status_keyboard(order["id"], order.get("status"))
    elif is_courier:
        keyboard = courier_status_keyboard(
            order["id"],
            str(order.get("status")),
            str(order.get("payment_status") or "unpaid"),
            point=order_point(order),
        )
    else:
        keyboard = client_order_keyboard(order["id"])
    await message.answer(format_order(order, "📦 Заказ"), reply_markup=keyboard)


@router.message(Command("last"))
async def cmd_last(message: Message, db: Database) -> None:
    order = await db.last_order_for_user(message.from_user.id)
    if order is None:
        await message.answer(
            "У вас пока нет заказов.", reply_markup=client_back_keyboard()
        )
        return
    await message.answer(
        format_client_order(order, "Последний заказ"),
        reply_markup=client_order_keyboard(order["id"]),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, db: Database, settings: Settings) -> None:
    await db.set_chat_state(message.chat.id, "qty_input", "")
    count = await db.cancel_active_drafts(message.chat.id)
    keyboard = _back_keyboard(settings, message.from_user.id, message.chat.id)
    if count:
        await message.answer(
            "Отменил. Можно прислать новый заказ.",
            reply_markup=keyboard,
        )
    else:
        await message.answer("Нечего отменять.", reply_markup=keyboard)
