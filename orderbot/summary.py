"""Сводка дня для владельца: что горит прямо сейчас.

Одним сообщением: заказы на сегодня и завтра, что не собрано и не доставлено,
наличные, долги, заказы без принятия, свежесть бэкапа. Без AI и без новых таблиц.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .formatting import money

BACKUP_DIRS = (Path("/var/backups/order_bot"), Path("data/backups"))


def _backup_age_hours() -> float | None:
    newest: float | None = None
    for folder in BACKUP_DIRS:
        if not folder.is_dir():
            continue
        for path in folder.glob("orderbot-pg-*.dump"):
            mtime = path.stat().st_mtime
            if newest is None or mtime > newest:
                newest = mtime
    if newest is None:
        return None
    return (datetime.now().timestamp() - newest) / 3600


def _day_block(title: str, orders: list[dict[str, Any]]) -> list[str]:
    if not orders:
        return [f"<b>{title}</b>: заказов нет"]
    total = sum(float(order.get("total") or 0) for order in orders)
    counts: dict[str, int] = {}
    for order in orders:
        counts[str(order.get("status"))] = counts.get(str(order.get("status")), 0) + 1
    parts = [f"<b>{title}</b>: {len(orders)} на {money(total)}"]
    detail = []
    if counts.get("new"):
        detail.append(f"новых {counts['new']}")
    if counts.get("accepted"):
        detail.append(f"в сборке {counts['accepted']}")
    if counts.get("ready"):
        detail.append(f"собрано {counts['ready']}")
    if counts.get("delivering"):
        detail.append(f"в пути {counts['delivering']}")
    if counts.get("done"):
        detail.append(f"доставлено {counts['done']}")
    if detail:
        parts.append(" (" + ", ".join(detail) + ")")
    return ["".join(parts)]


async def today_summary(db: Database, settings: Settings) -> str:
    now = datetime.now(settings.timezone)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    orders_today = await db.orders_for_date(today.isoformat())
    orders_tomorrow = await db.orders_for_date(tomorrow.isoformat())

    lines = [f"☀️ <b>Сводка на {today.strftime('%d.%m.%Y')}</b>", ""]
    lines.extend(_day_block("Сегодня", orders_today))
    lines.extend(_day_block("Завтра", orders_tomorrow))

    waiting = [order for order in orders_today if order.get("status") == "new"]
    if waiting:
        ids = ", ".join(f"№{order['id']}" for order in waiting[:5])
        lines.append("")
        lines.append(f"⏳ Не приняты: {ids}")

    stale = await db.stale_new_orders(15)
    if stale:
        lines.append(f"⏰ Ждут принятия больше 15 минут: {len(stale)}")

    cash = await db.cash_report(today.isoformat())
    if cash["orders"]:
        lines.append("")
        lines.append(
            f"💵 Наличные: ожидалось {money(cash['expected'])}, "
            f"получено {money(cash['received'])}"
        )
        if cash["diff"]:
            lines.append(f"⚠️ Расхождения по {len(cash['diff'])} заказам")

    debts = await db.debt_list()
    if debts:
        total_debt = sum(float(row.get("s") or 0) for row in debts)
        lines.append("")
        lines.append(
            f"🧾 Долги: {len(debts)} клиентов на {money(total_debt)}"
        )
        oldest = debts[0]
        since = str(oldest.get("since") or "")[:10]
        if since:
            lines.append(f"   самый старый — {since} ({oldest.get('chef_name') or oldest.get('chef_id')})")

    age = _backup_age_hours()
    lines.append("")
    if age is None:
        lines.append("❌ Бэкапа базы не видно — проверьте order_bot_backup.timer")
    elif age > 3:
        lines.append(f"⚠️ Бэкап устарел: {age:.0f} ч назад")
    else:
        lines.append(f"💾 Бэкап: {age:.1f} ч назад")

    return "\n".join(lines)
