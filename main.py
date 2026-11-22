"""Точка входа: запуск Order bot."""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import subprocess
import urllib.request
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from orderbot.ai import AIClient
from orderbot.catalog import Catalog
from orderbot.config import Settings, load_settings
from orderbot.db import Database
from orderbot.extras import (
    tomorrow_summary,
    week_advice,
    week_comparison,
    week_metrics,
    weekly_report,
)
from orderbot.formatting import money, set_local_rate
from orderbot.handlers import (
    _send_order_card,
    load_staff_from_db,
    poll_ton_payments,
    prune_locks,
    record_error,
    router,
)
from orderbot.survey import survey_text
from orderbot.keyboards import (
    survey_keyboard,
    courier_status_keyboard,
    manager_status_keyboard,
    reminder_keyboard,
)
from orderbot.pricecast import send_morning_prices
from orderbot import pricereview as price_review
from orderbot.aicost import WARN_SHARE as ai_warn_share
from orderbot.aicost import budget_share as ai_budget_share
from orderbot.aicost import budget_warning as ai_budget_warning
from orderbot.aicost import estimate as ai_cost_estimate
from orderbot.aicost import weekly_line as ai_weekly_line
from orderbot.churn import alert_text as churn_alert_text
from orderbot.payer import debt_text as payer_debt_text
from orderbot.staffrating import overall_line as staff_rating_line
from orderbot.staffrating import ratings_block as staff_rating_block
from orderbot.habit import nudge_text as habit_nudge_text, should_nudge
from orderbot.missing import digest_text as missing_digest_text
from orderbot.stock import (
    current as stock_current,
    hidden_ids,
    low_stock as stock_low,
    resolve as resolve_stock,
    summary_line as stock_summary_line,
    threshold as stock_threshold,
)
from orderbot.pending import purge_pending_prices
from orderbot.quiet import bind as bind_quiet, flush_queue
from orderbot.stt import Transcriber
from orderbot.summary import today_summary
from orderbot.watch import ErrorWatch

log = logging.getLogger("orderbot")

INVALID_TOKEN_HELP = """
================================================================================
  TELEGRAM ОТКЛОНИЛ ТОКЕН БОТА (401 Unauthorized)
================================================================================
  BOT_TOKEN в /opt/order_bot/.env недействителен или отозван.

  Что делать:
  1) Откройте @BotFather в Telegram и проверьте/перевыпустите токен:
     /mybots -> ваш бот -> API Token -> Revoke current token
  2) Вставьте новый токен в /opt/order_bot/.env (строка BOT_TOKEN=...)
  3) Перезапустите сервис:
     systemctl restart order_bot
  4) Проверьте логи:
     journalctl -u order_bot -n 50 --no-pager
================================================================================
"""


class AllowedChatsMiddleware(BaseMiddleware):
    """Если ALLOWED_CHAT_IDS задан — остальные чаты игнорируются."""

    def __init__(
        self, allowed: set[int], manager: int | None, courier: int | None = None
    ):
        self.allowed = allowed
        self.manager = manager
        self.courier = courier

    async def __call__(self, handler, event, data):
        chat = getattr(event, "chat", None)
        if chat is None:
            chat = getattr(getattr(event, "message", None), "chat", None)
        chat_id = getattr(chat, "id", None)
        staff = {self.manager, self.courier} - {None}
        if chat_id is None or chat_id in self.allowed or chat_id in staff:
            return await handler(event, data)
        return None


def setup_logging(level: str) -> ErrorWatch:
    watch = ErrorWatch()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("aiogram").setLevel(logging.INFO)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)
    logging.getLogger().addHandler(watch)
    return watch


def _manager_chat(settings: Settings) -> int | None:
    """Менеджер задаётся только через .env; изменить его из Telegram нельзя."""
    return settings.manager_chat_id


async def _notify_startup(bot: Bot, db: Database, settings: Settings) -> None:
    manager = _manager_chat(settings)
    if manager is None:
        return
    last_raw = await db.get_setting("last_startup_notify")
    try:
        last = float(last_raw) if last_raw else 0.0
    except ValueError:
        last = 0.0
    if time.time() - last < 3600:
        return
    text = (
        "🤖 <b>Order bot запущен</b>\n"
        "Принимаю заказы. Упаковщику — /orders, доставщику — /deliveries."
    )
    for chat_id in {manager, settings.courier_chat_id} - {None}:
        try:
            await bot.send_message(chat_id, text)
        except Exception as exc:
            log.warning("Не удалось отправить приветствие %s: %s", chat_id, exc)
    await db.set_setting("last_startup_notify", str(time.time()))


BACKUP_DIRS = (Path("/var/backups/order_bot"),)


def latest_dump(settings: Settings) -> Path | None:
    """Свежий дамп делает отдельный таймер order_bot_backup, бот его только читает."""
    newest: Path | None = None
    for folder in (*BACKUP_DIRS, settings.data_dir / "backups"):
        if not folder.is_dir():
            continue
        for path in folder.glob("orderbot-pg-*.dump"):
            if newest is None or path.stat().st_mtime > newest.stat().st_mtime:
                newest = path
    return newest


async def _webapp_order_loop(
    bot: Bot, db: Database, settings: Settings, catalog: Catalog, ai: AIClient
) -> None:
    """Заказы, собранные в мини-приложении каталога.

    Мини-приложение кладёт строку заказа в таблицу, бот разбирает её тем же
    путём, что и обычное сообщение повара.
    """
    from aiogram.types import Chat, Message, User

    from orderbot.handlers_client import _process_order

    while True:
        await asyncio.sleep(5)
        try:
            rows = await db.take_webapp_orders()
        except Exception:
            log.exception("Не удалось прочитать заказы из каталога")
            continue
        for row in rows:
            try:
                user_id = int(row["user_id"])
                text = str(row["text"])
                message = Message(
                    message_id=int(time.time()) % 2_000_000_000,
                    date=datetime.now(settings.timezone),
                    chat=Chat(id=int(row["chat_id"]), type="private"),
                    from_user=User(id=user_id, is_bot=False, first_name="Клиент"),
                    text=text,
                ).as_(bot)
                await _process_order(
                    message=message,
                    text=text,
                    source="catalog",
                    transcript=None,
                    db=db,
                    settings=settings,
                    catalog=catalog,
                    ai=ai,
                )
                await db.close_webapp_order(int(row["id"]))
                log.info("Заказ из каталога разобран: id=%s", row["id"])
            except Exception as exc:
                log.exception("Ошибка разбора заказа из каталога id=%s", row["id"])
                try:
                    await db.close_webapp_order(int(row["id"]), str(exc))
                except Exception:
                    pass


async def _ton_loop(bot: Bot, db: Database, settings: Settings) -> None:
    """Проверяет входящие USDT-переводы раз в N секунд.

    Если цикл падает подряд несколько раз, менеджер получает одну тревогу:
    молчащая проверка платежей — это неоплаченные заказы.
    """
    if not settings.ton_enabled:
        return
    failures = 0
    while True:
        await asyncio.sleep(settings.ton_poll_seconds)
        try:
            await poll_ton_payments(bot, db, settings)
            failures = 0
        except Exception as exc:
            failures += 1
            log.exception("Ошибка проверки USDT-платежей")
            if failures == 5:
                await _alert_loop_failure(bot, db, settings, "USDT", exc)


async def _alert_loop_failure(
    bot: Bot, db: Database, settings: Settings, name: str, exc: BaseException
) -> None:
    """Одна тревога менеджеру, если фоновый цикл падает снова и снова."""
    manager = settings.manager_chat_id
    if manager is None:
        return
    try:
        await bot.send_message(
            int(manager),
            f"⚠️ Фоновый цикл «{name}» падает подряд: {type(exc).__name__}.\n"
            "Проверьте журнал сервера — часть функций может не работать.",
        )
    except Exception:
        log.warning("Не удалось отправить тревогу о цикле %s", name)


def _ping(url: str) -> None:
    urllib.request.urlopen(url, timeout=10).read()


async def _churn_loop(bot: Bot, db: Database, settings: Settings) -> None:
    """Раз в сутки: кто выпал из своего ритма заказов (C10)."""
    manager = settings.manager_chat_id
    if manager is None:
        return
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            now = datetime.now(settings.timezone)
            today = now.date().isoformat()
            if await db.get_setting("churn_alert_day") == today:
                continue
            rows = await _silent_clients(db, today)
            if not rows:
                continue
            await db.set_setting("churn_alert_day", today)
            buttons = [
                [
                    InlineKeyboardButton(
                        text=f"📨 Напомнить {str(row['name'])[:20]}",
                        callback_data=f"chase:{row['chef_id']}",
                    )
                ]
                for row in rows[:6]
            ]
            await bot.send_message(
                int(manager),
                churn_alert_text(rows),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
            log.info("Алерт по оттоку: %s клиентов", len(rows))
        except Exception:
            log.exception("Ошибка проверки оттока")


async def _silent_clients(db: Database, today: str) -> list[dict]:
    """Клиенты, которые молчат дольше собственного ритма заказов."""
    from orderbot.churn import is_silent

    rows = await db.client_order_days(180)
    grouped: dict[int, dict] = {}
    for row in rows:
        chef_id = row.get("chef_id")
        if chef_id is None:
            continue
        entry = grouped.setdefault(
            int(chef_id), {"chef_id": int(chef_id), "name": row.get("chef_name"), "days": []}
        )
        entry["days"].append(str(row.get("day"))[:10])
    day = datetime.fromisoformat(str(today)[:10]).date()
    result: list[dict] = []
    for chef_id, entry in grouped.items():
        silent, silent_days, interval = is_silent(entry["days"], day)
        if not silent:
            continue
        last_chase = await db.get_setting(f"chase_sent:{chef_id}", "")
        if last_chase:
            try:
                if (
                    day - datetime.fromisoformat(str(last_chase)[:10]).date()
                ).days < 7:
                    continue
            except ValueError:
                pass
        result.append(
            {
                "chef_id": chef_id,
                "name": entry["name"] or chef_id,
                "silent_days": silent_days,
                "interval": interval,
            }
        )
    result.sort(key=lambda row: -int(row["silent_days"]))
    return result


async def _heartbeat_loop(settings: Settings) -> None:
    """Внешний признак жизни: если процесс упал, пинги прекратятся."""
    if not settings.heartbeat_url:
        return
    while True:
        try:
            await asyncio.to_thread(_ping, settings.heartbeat_url)
        except Exception as exc:
            log.debug("Heartbeat не отправлен: %s", exc)
        await asyncio.sleep(300)


async def _escalation_loop(bot: Bot, db: Database, settings: Settings) -> None:
    """Пингует упаковщика, если заказ не приняли за 15 минут."""
    manager = settings.manager_chat_id
    if manager is None:
        return
    while True:
        await asyncio.sleep(5 * 60)
        try:
            for order in await db.stale_new_orders(15):
                if not await db.claim_escalation(int(order["id"])):
                    continue
                who = await db.assigned_staff(int(order["id"]))
                tail = ""
                if who is not None:
                    tail = f" Назначен: {who.get('name') or who.get('user_id')}."
                # D4: сначала пробуем передать заказ другому упаковщику на смене.
                replacement = await db.reassign_to_other_packer(int(order["id"]))
                if replacement is not None:
                    fresh = await db.get_order(int(order["id"]))
                    if fresh is not None:
                        try:
                            await _send_order_card(
                                bot,
                                replacement,
                                fresh,
                                "🆕 Заказ (передан от напарника)",
                                manager_status_keyboard(int(order["id"]), "new"),
                            )
                        except Exception as exc:
                            log.warning("Не удалось передать карточку: %s", exc)
                    await bot.send_message(
                        int(manager),
                        f"⏰ Заказ №{order['id']} не приняли 15 минут — "
                        f"передал другому упаковщику (id {replacement}).{tail}",
                    )
                    log.info(
                        "Эскалация: заказ №%s передан упаковщику %s",
                        order["id"],
                        replacement,
                    )
                    continue
                await bot.send_message(
                    int(manager),
                    f"⏰ Заказ №{order['id']} ждёт принятия больше 15 минут. "
                    f"Сумма: {order.get('total')} USD.{tail}",
                    reply_markup=manager_status_keyboard(int(order["id"]), "new"),
                )
                log.info("Эскалация: заказ №%s не принят", order["id"])
        except Exception:
            log.exception("Ошибка эскалации заказов")


async def _send_daily_backup(bot: Bot, db: Database, settings: Settings) -> None:
    """Раз в сутки отправляет свежий бэкап БД менеджеру в Telegram."""
    manager = settings.manager_chat_id
    if manager is None:
        return
    raw = await db.get_setting("last_backup_sent")
    try:
        last = float(raw) if raw else 0.0
    except ValueError:
        last = 0.0
    if not last:
        # Первый запуск: отсчитываем сутки от него, чтобы не спамить сразу.
        await db.set_setting("last_backup_sent", str(time.time()))
        return
    if time.time() - last < 24 * 3600:
        return
    path = latest_dump(settings)
    if path is None:
        log.warning("Суточная отправка: свежего бэкапа базы нет")
        return
    await bot.send_document(
        manager,
        FSInputFile(path),
        caption=f"Бэкап базы · {path.name}",
    )
    await db.set_setting("last_backup_sent", str(time.time()))
    log.info("Бэкап БД отправлен менеджеру: %s", path)


async def _backup_send_loop(bot: Bot, db: Database, settings: Settings) -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            await _send_daily_backup(bot, db, settings)
        except Exception as exc:
            log.warning("Не удалось отправить суточный бэкап: %s", exc)


async def _run_maintenance(db: Database) -> None:
    purged = await db.purge_closed_drafts(7)
    if purged:
        log.info("Удалено закрытых черновиков старше 7 дней: %s", purged)
    removed = prune_locks()
    if removed:
        log.debug("Очищено блокировок: %s", removed)

    raw = await db.get_setting("last_vacuum")
    try:
        last = float(raw) if raw else 0.0
    except ValueError:
        last = 0.0
    if time.time() - last >= 30 * 24 * 3600:
        await db.vacuum()
        await db.set_setting("last_vacuum", str(time.time()))
        log.info("VACUUM базы выполнен")


def _dump_catalog_json(
    catalog: Catalog, settings: Settings, hidden: set[int] | None = None
) -> None:
    """Отдаёт прайс мини-аппу в JSON: он больше не читает Excel (G4)."""
    target = settings.data_dir / "catalog.json"
    try:
        count = catalog.dump_json(target, exclude=hidden)
        log.info("Каталог для мини-аппа: %s позиций → %s", count, target)
    except Exception as exc:
        log.warning("Не удалось записать %s: %s", target, exc)


def _catalog_json_stale(catalog: Catalog, settings: Settings) -> bool:
    """Excel новее JSON — прайс правили файлом, надо пересобрать."""
    target = settings.data_dir / "catalog.json"
    try:
        if not target.exists():
            return True
        return settings.catalog_path.stat().st_mtime > target.stat().st_mtime
    except OSError:
        return False


async def _maintenance_loop(
    db: Database, catalog: Catalog | None = None, settings: Settings | None = None
) -> None:
    first = True
    while True:
        await asyncio.sleep(60 if first else 6 * 3600)
        first = False
        try:
            await _run_maintenance(db)
        except Exception as exc:
            log.warning("Ошибка обслуживания БД: %s", exc)
        try:
            await purge_pending_prices(db)
        except Exception as exc:
            log.warning("Ошибка уборки подтверждений прайса: %s", exc)
        if catalog is not None and _catalog_json_stale(catalog, settings):
            hidden: set[int] | None = None
            try:
                hidden = hidden_ids(resolve_stock(catalog, await db.stock_overrides()))
            except Exception as exc:
                log.warning("Не удалось учесть остатки в каталоге: %s", exc)
            _dump_catalog_json(catalog, settings, hidden)


async def _resend_unnotified(bot: Bot, db: Database, settings: Settings) -> None:
    """Досылает менеджеру заказы, которые не ушли из-за обрыва сети."""
    manager = settings.manager_chat_id
    if manager is None:
        return
    for order_id in await db.list_unnotified_orders():
        order = await db.get_order(order_id)
        if order is None:
            continue
        try:
            await _send_order_card(
                bot,
                manager,
                order,
                "🆕 Заказ",
                manager_status_keyboard(order["id"], str(order.get("status"))),
            )
            await db.mark_order_notified(order["id"])
            log.info("Дослан менеджеру заказ №%s", order["id"])
        except Exception as exc:
            log.warning("Заказ №%s пока не удалось дослать: %s", order_id, exc)
            break


async def _resend_courier(bot: Bot, db: Database, settings: Settings) -> None:
    """Досылает доставщику собранные заказы, которые не ушли из-за сети."""
    courier = settings.courier_chat_id
    if courier is None:
        return
    for order_id in await db.list_unnotified_delivery_orders():
        order = await db.get_order(order_id)
        if order is None:
            continue
        if order.get("chef_id"):
            profile = await db.get_user(int(order["chef_id"]))
            if profile and profile.get("venue"):
                order["venue"] = profile["venue"]
        try:
            card_id = await _send_order_card(
                bot,
                courier,
                order,
                "🚚 Доставка",
                courier_status_keyboard(
                    order["id"], "ready", str(order.get("payment_status") or "unpaid")
                ),
            )
            await db.mark_courier_notified(order["id"])
            if card_id:
                await db.set_order_cards(order["id"], courier_card=card_id)
            log.info("Дослан доставщику заказ №%s", order["id"])
        except Exception as exc:
            log.warning("Заказ №%s пока не удалось дослать доставщику: %s", order_id, exc)
            break


async def _quiet_loop(bot: Bot, db: Database, settings: Settings) -> None:
    """Раз в 5 минут: если тихие часы кончились, отправить накопленное одной сводкой."""
    while True:
        await asyncio.sleep(5 * 60)
        try:
            await flush_queue(bot, db, settings)
        except Exception as exc:
            log.warning("Ошибка сводки тихих часов: %s", exc)


async def _notify_loop(bot: Bot, db: Database, settings: Settings) -> None:
    while True:
        await asyncio.sleep(5 * 60)
        try:
            await _resend_unnotified(bot, db, settings)
            await _resend_courier(bot, db, settings)
        except Exception as exc:
            log.warning("Ошибка фоновой досылки заказов: %s", exc)


async def _send_alarm(
    bot: Bot, db: Database, manager: int, kind: str, text: str
) -> None:
    """Тревога менеджеру не чаще одного раза в час на каждый тип."""
    key = f"alert:{kind}"
    raw = await db.get_setting(key)
    try:
        last = float(raw) if raw else 0.0
    except ValueError:
        last = 0.0
    if time.time() - last < 3600:
        return
    try:
        await bot.send_message(manager, text)
        await db.set_setting(key, str(time.time()))
    except Exception as exc:
        log.warning("Не удалось отправить тревогу %s: %s", kind, exc)


async def _check_alarms(bot: Bot, db: Database, settings: Settings, ai: AIClient) -> None:
    manager = settings.manager_chat_id
    if manager is None:
        return
    for provider, label in (("deepseek", "DeepSeek"), ("gemini", "Gemini")):
        if ai.provider_down(provider, 600):
            await _send_alarm(
                bot,
                db,
                manager,
                f"ai_{provider}",
                f"⚠️ {label} не отвечает больше 10 минут.",
            )
    old = await db.old_unnotified_orders(15)
    if old:
        await _send_alarm(
            bot,
            db,
            manager,
            "orders",
            f"⚠️ Заказ №{old[0]} не ушёл менеджеру больше 15 минут.",
        )


async def _check_error_rate(
    bot: Bot, db: Database, settings: Settings, watch: ErrorWatch
) -> None:
    """Тревога по факту: много ошибок за 10 минут или один и тот же тип трижды."""
    manager = settings.manager_chat_id
    if manager is None:
        return
    total, per_class = watch.snapshot()
    if total == 0:
        return
    worst = ", ".join(
        f"{kind}×{count}" for kind, count in per_class.most_common(3)
    )
    if total >= 5:
        await _send_alarm(
            bot,
            db,
            manager,
            "errors_total",
            f"⚠️ Ошибок за 10 минут: {total} ({worst}).\n"
            "Журнал: journalctl -u order_bot -n 50 --no-pager",
        )
    for kind, count in per_class.items():
        if count >= 3:
            await _send_alarm(
                bot,
                db,
                manager,
                f"errors_{kind}",
                f"⚠️ Ошибка «{kind}» повторилась {count} раз за 10 минут.\n"
                "Журнал: journalctl -u order_bot -n 50 --no-pager",
            )


async def _health_loop(
    bot: Bot, db: Database, settings: Settings, ai: AIClient, watch: ErrorWatch
) -> None:
    while True:
        await asyncio.sleep(5 * 60)
        try:
            await _check_alarms(bot, db, settings, ai)
        except Exception as exc:
            log.warning("Ошибка проверки тревог: %s", exc)
        try:
            await _check_error_rate(bot, db, settings, watch)
        except Exception as exc:
            log.warning("Ошибка проверки частоты ошибок: %s", exc)


async def _send_quiet(bot: Bot, chat_id: int | None, text: str, **kwargs) -> bool:
    if chat_id is None:
        return False
    try:
        await bot.send_message(chat_id, text, **kwargs)
        return True
    except Exception as exc:
        log.warning("Не удалось отправить сообщение %s: %s", chat_id, exc)
        return False


async def _evening_summary(bot: Bot, db: Database, settings: Settings) -> bool:
    """Сводка на завтра, один раз в день после 18:00."""
    manager = settings.manager_chat_id
    if manager is None:
        return False
    now = datetime.now(settings.timezone)
    today = now.date().isoformat()
    if now.hour < 18:
        return False
    if await db.get_setting("last_evening_summary") == today:
        return False
    tomorrow = (now.date() + timedelta(days=1)).isoformat()
    orders = await db.orders_for_date(tomorrow)
    await db.set_setting("last_evening_summary", today)
    if not orders:
        return False
    venues = await db.venue_map(
        [int(order["chef_id"]) for order in orders if order.get("chef_id")]
    )
    for order in orders:
        order["venue"] = venues.get(int(order.get("chef_id") or 0))
    text = tomorrow_summary(
        orders, datetime.fromisoformat(tomorrow).strftime("%d.%m.%Y")
    )
    await _send_quiet(bot, manager, text)
    log.info("Вечерняя сводка отправлена: заказов %s", len(orders))
    return True


async def _evening_loop(bot: Bot, db: Database, settings: Settings) -> None:
    while True:
        await asyncio.sleep(10 * 60)
        try:
            await _evening_summary(bot, db, settings)
        except Exception as exc:
            log.warning("Ошибка вечерней сводки: %s", exc)


async def _debt_reminders(bot: Bot, db: Database, settings: Settings) -> int:
    """Напоминания должникам, у которых вышел срок оплаты. Не чаще раза в день."""
    now = datetime.now(settings.timezone)
    if now.hour < 10:
        return 0
    today = now.date().isoformat()
    if await db.get_setting("last_debt_reminder") == today:
        return 0
    try:
        default_days = int(await db.get_setting("debt_default_days", "14") or 14)
    except ValueError:
        default_days = 14
    sent = 0
    for row in await db.debt_list():
        chef_id = int(row["chef_id"])
        user = await db.get_user(chef_id)
        days = int(user.get("credit_days") or 0) if user else 0
        term = days or default_days
        debt, count, age = await db.client_debt(chef_id)
        if age is None or age <= term:
            continue
        try:
            await bot.send_message(
                chef_id,
                f"Напоминание об оплате: {money(debt)} за {count} заказ(ов), "
                f"самый ранний — {age} дней назад.\n"
                "Если оплата уже отправлена — напишите менеджеру, отметим.",
            )
            sent += 1
        except Exception as exc:
            log.warning("Напоминание о долге не отправлено %s: %s", chef_id, exc)
        # C6: те же суммы — плательщику точки, если он подключён.
        try:
            for payer_id in await db.payers_for(chef_id):
                name = (user or {}).get("name") or (user or {}).get("venue") or str(chef_id)
                await bot.send_message(
                    payer_id, payer_debt_text(str(name), debt, count, age)
                )
                sent += 1
        except Exception as exc:
            log.info("Напоминание плательщику по %s не ушло: %s", chef_id, exc)
    await db.set_setting("last_debt_reminder", today)
    if sent:
        log.info("Напоминания о долге отправлены: %s", sent)
    return sent


async def _debt_reminder_loop(bot: Bot, db: Database, settings: Settings) -> None:
    while True:
        try:
            await _debt_reminders(bot, db, settings)
        except Exception as exc:
            log.warning("Ошибка напоминаний о долге: %s", exc)
        await asyncio.sleep(2 * 3600)


async def _price_morning_loop(
    bot: Bot, db: Database, settings: Settings, catalog: Catalog
) -> None:
    """Утренние цены клиентам с кнопкой «Как обычно» (включает владелец)."""
    while True:
        try:
            await send_morning_prices(bot, db, settings, catalog)
        except Exception as exc:
            log.warning("Ошибка утренней рассылки цен: %s", exc)
        await asyncio.sleep(30 * 60)


async def _survey_loop(bot: Bot, db: Database, settings: Settings) -> None:
    """C5: через сутки после доставки спрашивает «как прошёл заказ»."""
    while True:
        try:
            for order in await db.delivered_between(20, 48):
                order_id = int(order["id"])
                if not await db.claim_once(f"survey:{order_id}"):
                    continue
                chef_id = order.get("chef_id")
                if not chef_id:
                    continue
                try:
                    await bot.send_message(
                        int(chef_id),
                        survey_text(order_id),
                        reply_markup=survey_keyboard(order_id),
                    )
                except Exception as exc:
                    log.info("Опрос по заказу %s не ушёл: %s", order_id, exc)
        except Exception as exc:
            log.warning("Ошибка опроса после доставки: %s", exc)
        await asyncio.sleep(30 * 60)


async def _vosk_idle_loop(transcriber) -> None:
    """Раз в 10 минут выгружает офлайн-модель Vosk, если ею не пользовались (A5)."""
    while True:
        await asyncio.sleep(10 * 60)
        try:
            await transcriber.release_idle(10)
        except Exception as exc:
            log.warning("Не удалось выгрузить модель Vosk: %s", exc)


async def _llm_usage_loop(
    bot: Bot, db: Database, settings: Settings, ai: AIClient
) -> None:
    """Раз в 15 минут пишет расход ИИ в базу и предупреждает о бюджете (E4)."""
    manager = settings.manager_chat_id
    written: dict[str, int] = {}
    while True:
        try:
            today = datetime.now(settings.timezone).date().isoformat()
            total = 0
            for provider, calls in ai.calls_today.items():
                delta = int(calls) - written.get(provider, 0)
                if delta > 0:
                    await db.add_llm_usage(
                        today, provider, delta, 0, ai_cost_estimate(delta, settings)
                    )
                    written[provider] = int(calls)
                total += int(calls)
            budget = int(getattr(settings, "llm_daily_budget", 0) or 0)
            if manager is not None and budget:
                key = f"llm_budget_warned:{today}"
                share = ai_budget_share(total, budget)
                if share >= ai_warn_share() and await db.get_setting(key) != "1":
                    await db.set_setting(key, "1")
                    await bot.send_message(
                        int(manager),
                        ai_budget_warning(total, budget, ai_cost_estimate(total, settings)),
                    )
        except Exception as exc:
            log.warning("Не удалось записать расход ИИ: %s", exc)
        await asyncio.sleep(15 * 60)


async def _habit_loop(
    bot: Bot, db: Database, settings: Settings, catalog: Catalog
) -> None:
    """C2: в привычный день клиента, если заказа нет, — предложить «как обычно»."""
    while True:
        try:
            now = datetime.now(settings.timezone)
            today = now.date().isoformat()
            if now.hour >= 10 and await db.get_setting("habit_nudge_day") != today:
                if await db.get_setting("habit_nudge_enabled", "1") == "1":
                    await db.set_setting("habit_nudge_day", today)
                    await _send_habit_nudges(bot, db, settings, catalog, now.date())
        except Exception as exc:
            log.warning("Ошибка напоминаний по привычке: %s", exc)
        await asyncio.sleep(3600)


async def _send_habit_nudges(
    bot: Bot, db: Database, settings: Settings, catalog: Catalog, today
) -> int:
    """Рассылает «обычно вы заказываете по средам» тем, кто ещё не заказал."""
    rows = await db.client_order_days(120)
    by_client: dict[int, dict] = {}
    for row in rows:
        chef_id = row.get("chef_id")
        if chef_id is None:
            continue
        entry = by_client.setdefault(
            int(chef_id), {"name": row.get("chef_name"), "days": []}
        )
        entry["days"].append(str(row.get("day"))[:10])
    sent = 0
    for chef_id, entry in by_client.items():
        if sent >= 30:
            break
        try:
            if await db.get_chat_state(chef_id, "remind_off"):
                continue
            if not should_nudge(entry["days"], today, await db.ordered_on(chef_id, today.isoformat())):
                continue
            usual = await db.usual_items(chef_id, 5)
            names = [str(item.get("name")) for item in usual if item.get("name")]
            await bot.send_message(
                chef_id,
                habit_nudge_text(today.weekday(), names),
                reply_markup=reminder_keyboard(int(chef_id)),
            )
            sent += 1
        except Exception as exc:
            log.info("Не отправил напоминание клиенту %s: %s", chef_id, exc)
    if sent:
        log.info("Напоминания по привычке: %s клиентам", sent)
    return sent


async def _missing_digest_loop(
    bot: Bot, db: Database, settings: Settings
) -> None:
    """Раз в неделю (пн, после 10:00): что просят, а в прайсе нет (B11)."""
    manager = settings.manager_chat_id
    while True:
        try:
            if manager is not None:
                now = datetime.now(settings.timezone)
                today = now.date().isoformat()
                if (
                    now.weekday() == 0
                    and now.hour >= 10
                    and await db.get_setting("missing_digest_day") != today
                ):
                    rows = await db.missing_digest(30, 10)
                    await db.set_setting("missing_digest_day", today)
                    await bot.send_message(int(manager), missing_digest_text(rows, 30))
                    log.info("Сводка запросов вне прайса отправлена: %s поз.", len(rows))
        except Exception as exc:
            log.warning("Ошибка сводки запросов вне прайса: %s", exc)
        await asyncio.sleep(6 * 3600)


async def _price_review_loop(
    bot: Bot, db: Database, settings: Settings
) -> None:
    """Напоминание менеджеру пересмотреть цены (B6), не чаще раза в неделю."""
    while True:
        try:
            await price_review.send_reminder(bot, db, settings)
        except Exception as exc:
            log.warning("Ошибка напоминания о прайсе: %s", exc)
        await asyncio.sleep(6 * 3600)


async def _morning_summary(
    bot: Bot, db: Database, settings: Settings, catalog: Catalog | None = None
) -> bool:
    """Сводка «что горит» владельцу каждое утро, один раз в день."""
    manager = settings.manager_chat_id
    if manager is None:
        return False
    now = datetime.now(settings.timezone)
    if now.hour < 9:
        return False
    today = now.date().isoformat()
    if await db.get_setting("last_morning_summary") == today:
        return False
    await db.set_setting("last_morning_summary", today)
    text = await today_summary(db, settings)
    if catalog is not None:
        try:
            limit = await stock_threshold(db)
            rows = stock_low(await stock_current(catalog, db), limit)
            line = stock_summary_line(rows)
            if line:
                text = f"{text}\n\n{line}"
        except Exception as exc:
            log.warning("Не удалось посчитать остатки в сводке: %s", exc)
    await bot.send_message(int(manager), text)
    log.info("Утренняя сводка отправлена")
    return True


async def _morning_loop(
    bot: Bot, db: Database, settings: Settings, catalog: Catalog | None = None
) -> None:
    while True:
        try:
            await _morning_summary(bot, db, settings, catalog)
        except Exception as exc:
            log.warning("Ошибка утренней сводки: %s", exc)
        await asyncio.sleep(15 * 60)


def _week_key(value: str) -> tuple[int, int] | None:
    try:
        day = datetime.fromisoformat(str(value)).date()
    except (TypeError, ValueError):
        return None
    iso = day.isocalendar()
    return (iso[0], iso[1])


async def _weekly_report(bot: Bot, db: Database, settings: Settings) -> bool:
    manager = settings.manager_chat_id
    if manager is None:
        return False
    now = datetime.now(settings.timezone)
    if now.weekday() != 0 or now.hour < 9:
        return False
    today = now.date().isoformat()
    if await db.get_setting("last_weekly_report") == today:
        return False
    await db.set_setting("last_weekly_report", today)
    end = now + timedelta(seconds=1)
    start = now - timedelta(days=7)
    orders = await db.orders_between(
        start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")
    )
    venues = await db.venue_map(
        [int(order["chef_id"]) for order in orders if order.get("chef_id")]
    )
    month_orders = await db.orders_between(
        (now - timedelta(days=28)).strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
    )
    weeks: dict[int, set[tuple[int, int]]] = {}
    last_seen: dict[int, datetime] = {}
    for order in month_orders:
        chef = order.get("chef_id")
        if not chef:
            continue
        key = _week_key(order.get("created_at") or "")
        if key is not None:
            weeks.setdefault(int(chef), set()).add(key)
        try:
            created = datetime.fromisoformat(str(order.get("created_at")))
            last_seen[int(chef)] = max(last_seen.get(int(chef), created), created)
        except ValueError:
            pass
    regulars = [
        venues.get(chef) or f"id{chef}"
        for chef, keys in weeks.items()
        if len(keys) >= 3
    ]
    dropped = [
        venues.get(chef) or f"id{chef}"
        for chef, seen in last_seen.items()
        if (now.replace(tzinfo=None) - seen).days >= 14
    ]
    prev_orders = await db.orders_between(
        (start - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),
        start.strftime("%Y-%m-%d %H:%M:%S"),
    )
    current_metrics = week_metrics(orders)
    previous_metrics = week_metrics(prev_orders)
    stats = await db.ops_stats(7)
    extra: list[str] = []
    extra.extend(week_comparison(current_metrics, previous_metrics))
    extra.extend(
        week_advice(
            current_metrics,
            previous_metrics,
            failed=sum(int(row.get("n") or 0) for row in stats.get("failed") or []),
            claims=sum(int(row.get("n") or 0) for row in stats.get("claims") or []),
            dropped=len(dropped),
        )
    )
    try:
        ai_line = ai_weekly_line(
            await db.llm_usage_range(7), await db.count_orders_done(7)
        )
        if ai_line:
            extra.append(ai_line)
    except Exception as exc:
        log.warning("Не удалось собрать расход ИИ для отчёта: %s", exc)
    try:
        extra.extend(staff_rating_block(await db.staff_ratings(7)))
        extra.append(staff_rating_line(await db.order_ratings(7)))
    except Exception as exc:
        log.warning("Не удалось собрать оценки бригады: %s", exc)
    text = weekly_report(
        orders,
        venues,
        regulars,
        dropped,
        start.strftime("%d.%m"),
        end.strftime("%d.%m"),
        extra,
    )
    await _send_quiet(bot, manager, text)
    log.info("Недельный отчёт отправлен")
    return True


async def _weekly_loop(bot: Bot, db: Database, settings: Settings) -> None:
    while True:
        await asyncio.sleep(30 * 60)
        try:
            await _weekly_report(bot, db, settings)
        except Exception as exc:
            log.warning("Ошибка недельного отчёта: %s", exc)


async def _send_reminders(bot: Bot, db: Database, settings: Settings) -> int:
    """Напоминание накануне обычного дня заказа."""
    now = datetime.now(settings.timezone)
    if now.hour < 19:
        return 0
    tomorrow = now.date() + timedelta(days=1)
    tomorrow_str = tomorrow.isoformat()
    sent = 0
    for chef in await db.active_chefs():
        if await db.get_chat_state(chef, "remind_off"):
            continue
        if await db.get_chat_state(chef, "last_remind") == now.date().isoformat():
            continue
        if await db.get_chat_state(chef, "remind_off"):
            continue
        already = await db.orders_for_date(tomorrow_str)
        if any(int(order.get("chef_id") or 0) == int(chef) for order in already):
            continue
        dates = await db.client_order_dates(chef, 20)
        if len(dates) >= 3:
            days_list = []
            for value in dates:
                try:
                    days_list.append(
                        datetime.strptime(value[:10], "%Y-%m-%d").date()
                    )
                except ValueError:
                    continue
            gaps = [
                (days_list[i] - days_list[i + 1]).days
                for i in range(len(days_list) - 1)
            ]
            gaps = sorted(gap for gap in gaps if gap > 0)
            if not gaps:
                continue
            median = gaps[len(gaps) // 2]
            since_last = (now.date() - days_list[0]).days
            if since_last < max(1, median - 1):
                continue
        else:
            weekdays = await db.user_weekdays(chef, 60)
            if weekdays.count(tomorrow.weekday()) < 3:
                continue
        await db.set_chat_state(chef, "last_remind", now.date().isoformat())
        if await _send_quiet(
            bot,
            int(chef),
            "Завтра ваш обычный день заказа. Заказать как обычно?",
            reply_markup=reminder_keyboard(int(chef)),
        ):
            sent += 1
    return sent


async def _reminder_loop(bot: Bot, db: Database, settings: Settings) -> None:
    while True:
        await asyncio.sleep(60 * 60)
        try:
            await _send_reminders(bot, db, settings)
        except Exception as exc:
            log.warning("Ошибка напоминаний: %s", exc)


async def _verify_backup_loop(bot: Bot, db: Database, settings: Settings) -> None:
    """Раз в неделю проверяем, что последний дамп действительно восстанавливается.

    Создание временной базы требует прав postgres, поэтому когда бот работает
    не от root, эту работу делает отдельный таймер order_bot_verify.
    """
    if os.geteuid() != 0:
        log.info("Проверка восстановления бэкапа — отдельным таймером order_bot_verify")
        return
    while True:
        await asyncio.sleep(7 * 24 * 3600)
        if not settings.database_url:
            continue
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, str(Path(__file__).resolve().parent / "scripts" / "verify_backup.py")],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent),
        )
        if result.returncode != 0:
            tail = (result.stdout + result.stderr).strip()[-500:]
            await _send_alarm(
                bot,
                db,
                settings.manager_chat_id or 0,
                "backup_verify",
                "⚠️ Проверка бэкапа не прошла:\n" + tail,
            )
        else:
            log.info("Проверка бэкапа: OK")


async def _on_error(
    event: ErrorEvent,
    db: Database | None = None,
    settings: Settings | None = None,
    bot: Bot | None = None,
) -> None:
    """Не даём боту молча падать: трейс в журнал, текст заказа — для повтора."""
    exc = event.exception
    record_error(exc)
    log.error(
        "Необработанная ошибка при обработке апдейта: %s",
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    from orderbot.handlers import RETRY_TEXT, _remember_retry, _report_failure
    from orderbot.keyboards import retry_keyboard

    try:
        if event.update.callback_query is not None:
            await event.update.callback_query.answer(
                "Ошибка. Попробуйте ещё раз или /start.", show_alert=True
            )
        elif event.update.message is not None:
            message = event.update.message
            text = (message.text or "").strip()
            is_manager_chat = bool(
                settings
                and message.chat.id
                in {settings.manager_chat_id, settings.courier_chat_id}
            )
            if db is not None and text and not is_manager_chat:
                try:
                    await _remember_retry(db, message.chat.id, text, "text", None)
                    await message.answer(RETRY_TEXT, reply_markup=retry_keyboard())
                except Exception:
                    await message.answer(
                        "⚠️ Что-то пошло не так. Попробуйте ещё раз или /start."
                    )
            else:
                await message.answer(
                    "⚠️ Что-то пошло не так. Попробуйте ещё раз или /start."
                )
    except Exception:
        log.debug("Не удалось сообщить об ошибке пользователю", exc_info=True)
    if bot is not None and db is not None and settings is not None:
        try:
            await _report_failure(bot, db, settings, "update", exc)
        except Exception:
            log.debug("Не удалось отправить тревогу менеджеру", exc_info=True)


async def run() -> int:
    started = time.time()
    settings = load_settings()
    watch = setup_logging(settings.log_level)

    log.info("=" * 70)
    log.info("Order bot — запуск")
    log.info("Каталог: %s", settings.catalog_path)
    log.info(
        "База:    %s",
        "PostgreSQL" if settings.database_url else settings.db_path,
    )
    log.info("Часовой пояс: %s", settings.timezone_name)

    if not settings.bot_token:
        log.critical("BOT_TOKEN не задан в .env — запуск невозможен.")
        return 2

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        catalog = Catalog.load(settings.catalog_path)
        _dump_catalog_json(catalog, settings)
    except Exception as exc:
        log.critical("Не удалось загрузить прайс %s: %s", settings.catalog_path, exc)
        return 3
    log.info("Прайс загружен: %d позиций", len(catalog.items))

    db = Database(settings.db_path, settings.database_url)
    db.alias_auto_global = settings.alias_auto_global
    await db.connect()
    await db.init()
    log.info("Драйвер БД: %s", type(db.conn).__name__)
    await load_staff_from_db(db)
    try:
        set_local_rate(float(await db.get_setting("usd_rate", "0") or 0))
    except Exception:
        log.debug("Не удалось загрузить курс")
    latest = latest_dump(settings)
    if latest is None:
        log.warning("Свежих бэкапов базы не видно — проверьте order_bot_backup.timer")
    else:
        log.info("Последний бэкап БД: %s", latest)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        log.critical(INVALID_TOKEN_HELP)
        await bot.session.close()
        await db.close()
        return 2
    except Exception as exc:
        log.critical("Не удалось подключиться к Telegram API: %s", exc)
        await bot.session.close()
        await db.close()
        return 3

    log.info("Подключение к Telegram: @%s (id=%s)", me.username, me.id)
    if settings.manager_chat_id is None:
        log.warning(
            "MANAGER_CHAT_ID не задан: заказы будут сохраняться, но отправлять их некому."
        )
    else:
        log.info("Упаковщик заказов: chat_id=%s", settings.manager_chat_id)
    if settings.courier_chat_id is None:
        log.warning("COURIER_CHAT_ID не задан: передавать заказы в доставку некому.")
    else:
        log.info("Доставщик: chat_id=%s", settings.courier_chat_id)

    ai = AIClient(settings, catalog)
    await ai.check_health()
    transcriber = Transcriber(settings, ai)
    if settings.vosk_model_path is not None:
        log.info("Офлайн-распознавание Vosk: %s", settings.vosk_model_path)
    else:
        log.warning("Офлайн-модель Vosk не найдена — голос зависит только от Gemini")

    dp = Dispatcher(storage=MemoryStorage())
    dp["db"] = db
    dp["settings"] = settings
    dp["catalog"] = catalog
    dp["ai"] = ai
    dp["transcriber"] = transcriber
    bind_quiet(db, settings)
    dp.include_router(router)
    dp.errors.register(
        functools.partial(_on_error, db=db, settings=settings, bot=bot)
    )
    if settings.allowed_chat_ids:
        allowed = AllowedChatsMiddleware(
            set(settings.allowed_chat_ids),
            settings.manager_chat_id,
            settings.courier_chat_id,
        )
        dp.message.outer_middleware(allowed)
        dp.callback_query.outer_middleware(allowed)
        log.info("ALLOWED_CHAT_IDS: %d чатов", len(settings.allowed_chat_ids))

    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Начать"),
                BotCommand(command="price", description="Список товаров"),
                BotCommand(command="help", description="Помощь"),
            ]
        )
        if settings.manager_chat_id is not None:
            try:
                await bot.set_my_commands(
                    [
                        BotCommand(command="start", description="Панель"),
                        BotCommand(command="orders", description="Входящие"),
                        BotCommand(command="order", description="Заказ"),
                        BotCommand(command="price", description="Прайс"),
                        BotCommand(command="missing", description="Нет в прайсе"),
                        BotCommand(command="lowstock", description="Заканчивается"),
                        BotCommand(command="income", description="Приход товара"),
                        BotCommand(command="quality", description="Качество разбора"),
                        BotCommand(command="health", description="Здоровье"),
                        BotCommand(command="today", description="Сводка дня"),
                        BotCommand(command="pricenew", description="Цены на завтра"),
                        BotCommand(command="markup", description="Наценки"),
                    ],
                    scope=BotCommandScopeChat(chat_id=settings.manager_chat_id),
                )
            except Exception as exc:
                log.warning("Не удалось установить команды менеджера: %s", exc)
        if settings.courier_chat_id is not None:
            try:
                await bot.set_my_commands(
                    [
                        BotCommand(command="start", description="Панель доставок"),
                        BotCommand(command="deliveries", description="Мои доставки"),
                        BotCommand(command="order", description="Заказ"),
                        BotCommand(command="price", description="Прайс"),
                        BotCommand(command="help", description="Помощь"),
                    ],
                    scope=BotCommandScopeChat(chat_id=settings.courier_chat_id),
                )
            except Exception as exc:
                log.warning("Не удалось установить команды доставщика: %s", exc)
    except Exception as exc:
        log.warning("Не удалось установить команды бота: %s", exc)

    await _notify_startup(bot, db, settings)
    await _resend_unnotified(bot, db, settings)
    await _resend_courier(bot, db, settings)
    log.info("Бот готов, старт polling (%.1f сек)", time.time() - started)
    await flush_queue(bot, db, settings)
    notify_task = asyncio.create_task(
        _notify_loop(bot, db, settings), name="order-notify"
    )
    quiet_task = asyncio.create_task(
        _quiet_loop(bot, db, settings), name="quiet-hours"
    )
    health_task = asyncio.create_task(
        _health_loop(bot, db, settings, ai, watch), name="health-alerts"
    )
    backup_send_task = asyncio.create_task(
        _backup_send_loop(bot, db, settings), name="backup-send"
    )
    maintenance_task = asyncio.create_task(
        _maintenance_loop(db, catalog, settings), name="db-maintenance"
    )
    evening_task = asyncio.create_task(
        _evening_loop(bot, db, settings), name="evening-summary"
    )
    morning_task = asyncio.create_task(
        _morning_loop(bot, db, settings, catalog), name="morning-summary"
    )
    debt_task = asyncio.create_task(
        _debt_reminder_loop(bot, db, settings), name="debt-reminders"
    )
    price_task = asyncio.create_task(
        _price_morning_loop(bot, db, settings, catalog), name="price-morning"
    )
    price_review_task = asyncio.create_task(
        _price_review_loop(bot, db, settings), name="price-review"
    )
    missing_digest_task = asyncio.create_task(
        _missing_digest_loop(bot, db, settings), name="missing-digest"
    )
    habit_task = asyncio.create_task(
        _habit_loop(bot, db, settings, catalog), name="habit-nudges"
    )
    llm_usage_task = asyncio.create_task(
        _llm_usage_loop(bot, db, settings, ai), name="llm-usage"
    )
    vosk_idle_task = asyncio.create_task(
        _vosk_idle_loop(transcriber), name="vosk-idle"
    )
    survey_task = asyncio.create_task(
        _survey_loop(bot, db, settings), name="delivery-survey"
    )
    weekly_task = asyncio.create_task(
        _weekly_loop(bot, db, settings), name="weekly-report"
    )
    reminder_task = asyncio.create_task(
        _reminder_loop(bot, db, settings), name="order-reminders"
    )
    verify_task = asyncio.create_task(
        _verify_backup_loop(bot, db, settings), name="backup-verify"
    )
    ton_task = asyncio.create_task(
        _ton_loop(bot, db, settings), name="ton-payments"
    )
    escalation_task = asyncio.create_task(
        _escalation_loop(bot, db, settings), name="order-escalation"
    )
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(settings), name="heartbeat"
    )
    churn_task = asyncio.create_task(
        _churn_loop(bot, db, settings), name="churn-alert"
    )
    webapp_task = asyncio.create_task(
        _webapp_order_loop(bot, db, settings, catalog, ai), name="webapp-orders"
    )

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            handle_signals=True,
        )
    finally:
        log.info("Остановка бота")
        for task in (
            webapp_task,
            quiet_task,
            notify_task,
            health_task,
            backup_send_task,
            maintenance_task,
            evening_task,
            morning_task,
            price_task,
            debt_task,
            weekly_task,
            reminder_task,
            verify_task,
            ton_task,
            escalation_task,
            heartbeat_task,
            churn_task,
        ):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await bot.session.close()
        await db.close()
        await ai.close()
    return 0


def main() -> None:
    try:
        code = asyncio.run(run())
    except KeyboardInterrupt:
        code = 0
    raise SystemExit(code)


if __name__ == "__main__":
    main()
