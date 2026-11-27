"""F3: страница владельца — сегодня, долги, касса, доставки.

Открывается по ссылке `https://littlemouse.su:8443/owner?t=ТОКЕН`. Токен задан
в `.env` (`OWNER_WEB_TOKEN`), без него страница отвечает 403 — данные видны
только по ссылке из бота (команда `/owner` менеджеру).

Отдельная служба не нужна: страница живёт в том же `order_bot_web`, что и
каталог (порт 8443). База читается напрямую через psycopg, запросы простые
и только на чтение.
"""
from __future__ import annotations

import html
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent.parent

log = logging.getLogger("webapp.owner")

STATUS_LABELS = {
    "new": "новый",
    "accepted": "принят",
    "ready": "собран",
    "delivering": "в пути",
    "done": "доставлен",
    "failed": "не доставлен",
    "cancelled": "отменён",
}


def owner_token() -> str:
    """Токен доступа к странице владельца: из окружения или из .env."""
    raw = os.getenv("OWNER_WEB_TOKEN", "").strip()
    if raw:
        return raw
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("OWNER_WEB_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return url
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    return ""


def _money(value: Any) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    if abs(number - round(number)) < 0.005:
        return "$" + f"{round(number):,}".replace(",", " ")
    return "$" + f"{number:,.2f}".replace(",", " ")


def collect(day: date | None = None) -> dict[str, Any]:
    """Собирает цифры для страницы владельца. Ошибки не роняют страницу."""
    day = day or date.today()
    data: dict[str, Any] = {
        "day": day,
        "errors": [],
        "today": {},
        "debts": [],
        "debt_total": 0.0,
        "cash": {},
        "deliveries": [],
    }
    url = _database_url()
    if not url:
        data["errors"].append("не задан DATABASE_URL")
        return data
    import psycopg
    from psycopg.rows import dict_row

    try:
        conn = psycopg.connect(url, connect_timeout=5, row_factory=dict_row)
    except Exception as exc:  # noqa: BLE001
        log.warning("Страница владельца: база недоступна (%s)", exc)
        data["errors"].append(f"база недоступна: {type(exc).__name__}")
        return data
    try:
        data["today"] = _today_block(conn, day)
        data["debts"], data["debt_total"] = _debts_block(conn)
        data["cash"] = _cash_block(conn, day)
        data["deliveries"] = _deliveries_block(conn, day)
    finally:
        conn.close()
    return data


def _today_block(conn, day: date) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n, COALESCE(SUM(total), 0) AS s FROM orders "
        "WHERE created_at::date = %s AND status != 'cancelled' GROUP BY status",
        (day,),
    ).fetchall()
    total_orders = sum(int(row["n"]) for row in rows)
    revenue = sum(float(row["s"] or 0) for row in rows)
    by_status = {
        STATUS_LABELS.get(str(row["status"]), str(row["status"])): int(row["n"])
        for row in rows
    }
    avg = revenue / total_orders if total_orders else 0.0
    return {
        "orders": total_orders,
        "revenue": revenue,
        "avg": avg,
        "by_status": by_status,
    }


def _debts_block(conn) -> tuple[list[dict[str, Any]], float]:
    rows = conn.execute(
        "SELECT o.chef_id, COALESCE(u.venue, u.name, '') AS name, "
        "       SUM(o.total) AS billed, "
        "       COALESCE((SELECT SUM(p.amount) FROM payments p "
        "                 WHERE p.client_id = o.chef_id), 0) AS paid, "
        "       COALESCE((SELECT SUM(cn.amount) FROM credit_notes cn "
        "                 WHERE cn.client_id = o.chef_id), 0) AS credited "
        "FROM orders o LEFT JOIN users u ON u.user_id = o.chef_id "
        "WHERE o.chef_id IS NOT NULL AND o.status != 'cancelled' "
        "GROUP BY o.chef_id, u.venue, u.name"
    ).fetchall()
    debts: list[dict[str, Any]] = []
    total = 0.0
    for row in rows:
        open_sum = (
            float(row["billed"] or 0)
            - float(row["paid"] or 0)
            - float(row["credited"] or 0)
        )
        if open_sum <= 0.005:
            continue
        total += open_sum
        debts.append(
            {
                "client_id": int(row["chef_id"]),
                "name": str(row["name"] or f"id {row['chef_id']}"),
                "open": open_sum,
            }
        )
    debts.sort(key=lambda item: -item["open"])
    return debts[:10], total


def _cash_block(conn, day: date) -> dict[str, Any]:
    row = conn.execute(
        "SELECT COALESCE(SUM(total), 0) AS expected, "
        "       COALESCE(SUM(COALESCE(paid_amount, 0)), 0) AS received, "
        "       COUNT(*) AS n FROM orders "
        "WHERE payment_method = 'cash' AND paid_at::date = %s "
        "AND status IN ('delivering', 'done')",
        (day,),
    ).fetchone() or {}
    handed = conn.execute(
        "SELECT value FROM settings WHERE key = %s",
        (f"cash_handover:{day.isoformat()}",),
    ).fetchone()
    try:
        handed_sum = float((handed or {}).get("value") or 0)
    except (TypeError, ValueError):
        handed_sum = 0.0
    return {
        "orders": int(row.get("n") or 0),
        "expected": float(row.get("expected") or 0),
        "received": float(row.get("received") or 0),
        "diff": float(row.get("received") or 0) - float(row.get("expected") or 0),
        "handed": handed_sum,
    }


def _deliveries_block(conn, day: date) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, COALESCE(chef_name, '') AS name, total, status, "
        "       COALESCE(delivery_time, '') AS slot "
        "FROM orders WHERE delivery_date = %s AND status != 'cancelled' "
        "ORDER BY COALESCE(delivery_time, '99'), id",
        (day.isoformat(),),
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "name": str(row["name"] or ""),
            "total": float(row["total"] or 0),
            "status": STATUS_LABELS.get(str(row["status"]), str(row["status"])),
            "slot": str(row["slot"] or ""),
        }
        for row in rows
    ]


def _row(text: str, value: str) -> str:
    return (
        "<div class='row'><span>"
        + html.escape(text)
        + "</span><b>"
        + html.escape(value)
        + "</b></div>"
    )


def render(data: dict[str, Any]) -> str:
    day = data["day"]
    today = data.get("today") or {}
    cash = data.get("cash") or {}
    parts: list[str] = []
    parts.append(
        f"<h1>Сводка за {day.strftime('%d.%m.%Y')}</h1>"
        f"<p class='muted'>Обновлено {datetime.now().strftime('%H:%M')}</p>"
    )
    for error in data.get("errors") or []:
        parts.append(f"<p class='warn'>⚠️ {html.escape(str(error))}</p>")

    parts.append("<section><h2>Сегодня</h2>")
    parts.append(_row("Заказов", str(today.get("orders", 0))))
    parts.append(_row("Выручка", _money(today.get("revenue"))))
    parts.append(_row("Средний чек", _money(today.get("avg"))))
    by_status = today.get("by_status") or {}
    if by_status:
        parts.append(
            "<p class='muted'>"
            + html.escape(", ".join(f"{k}: {v}" for k, v in by_status.items()))
            + "</p>"
        )
    parts.append("</section>")

    parts.append("<section><h2>Касса за день</h2>")
    parts.append(_row("Ожидалось", _money(cash.get("expected"))))
    parts.append(_row("Получено", _money(cash.get("received"))))
    diff = float(cash.get("diff") or 0)
    parts.append(
        _row(
            "Расхождение",
            ("+" if diff > 0 else "") + _money(diff).lstrip("$"),
        )
    )
    if cash.get("handed"):
        parts.append(_row("Сдано курьером", _money(cash.get("handed"))))
    parts.append("</section>")

    parts.append("<section><h2>Долги клиентов</h2>")
    parts.append(_row("Всего открыто", _money(data.get("debt_total"))))
    debts = data.get("debts") or []
    if debts:
        for debt in debts:
            parts.append(_row(debt["name"], _money(debt["open"])))
    else:
        parts.append("<p class='muted'>Долгов нет.</p>")
    parts.append("</section>")

    parts.append("<section><h2>Доставки на день</h2>")
    deliveries = data.get("deliveries") or []
    if deliveries:
        for item in deliveries:
            parts.append(
                _row(
                    f"{item['slot']} · {item['name']} · №{item['id']}",
                    f"{_money(item['total'])} · {item['status']}",
                )
            )
    else:
        parts.append("<p class='muted'>Доставок на этот день нет.</p>")
    parts.append("</section>")

    body = "\n".join(parts)
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Сводка владельца</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:16px;
       background:#f6f7f9;color:#111;max-width:640px}}
 h1{{font-size:20px;margin:0 0 4px}}
 h2{{font-size:15px;margin:0 0 8px;color:#555;text-transform:uppercase;letter-spacing:.04em}}
 section{{background:#fff;border-radius:12px;padding:14px;margin:12px 0;
          box-shadow:0 1px 2px rgba(0,0,0,.06)}}
 .row{{display:flex;justify-content:space-between;gap:12px;padding:5px 0;font-size:15px}}
 .row+.row{{border-top:1px solid #f0f1f4}}
 .muted{{color:#6b7280;font-size:13px;margin:6px 0 0}}
 .warn{{color:#b45309;font-size:14px}}
</style></head><body>
{body}
</body></html>"""


def tomorrow_iso() -> str:
    return (date.today() + timedelta(days=1)).isoformat()
