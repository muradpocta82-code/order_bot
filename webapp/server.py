"""Мини-приложение каталога: отдаёт страницу и список товаров с ценами.

Запускается отдельной службой order_bot_web на порту 8443 (443 занят VPN).
Каталог отдаётся только по подписи Telegram (initData), поэтому цены
не видны случайным посетителям сайта.

Лёгкий режим (G4): прайс читается из data/catalog.json, который пишет бот
при каждой публикации цен, поэтому openpyxl и aiogram здесь не нужны —
служба держит в памяти только aiohttp и JSON. Excel читается лишь как
аварийный запасной вариант, если JSON ещё не создан.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import ssl
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

BASE = Path(__file__).resolve().parent.parent
WEB = Path(__file__).resolve().parent
PORT = int(os.getenv("WEBAPP_PORT", "8443"))
CERT = WEB / "certs" / "fullchain.pem"
KEY = WEB / "certs" / "privkey.pem"
CATALOG_JSON = BASE / "data" / "catalog.json"
CATALOG_XLSX = BASE / "catalog.xlsx"
OPEN_DEDUP_MINUTES = 30

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("webapp")


def bot_token() -> str:
    raw = os.getenv("BOT_TOKEN", "").strip()
    if raw:
        return raw
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def check_init_data(init_data: str, token: str, ttl_seconds: int = 86400) -> bool:
    """Проверка подписи Telegram WebApp (HMAC-SHA256), без aiogram."""
    if not init_data or not token:
        return False
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=False))
    except ValueError:
        return False
    received = pairs.pop("hash", "")
    if not received:
        return False
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received):
        return False
    auth_date = pairs.get("auth_date", "")
    if auth_date.isdigit():
        age = datetime.now().timestamp() - int(auth_date)
        if age > ttl_seconds:
            return False
    return True


def database_url() -> str:
    """Адрес боевой базы: из окружения службы или из .env."""
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return url
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    return ""


def _db_ok() -> bool:
    """Простой SELECT 1: база отвечает и пароль верный."""
    url = database_url()
    if not url:
        return False
    import psycopg

    try:
        with psycopg.connect(url, connect_timeout=5) as conn:
            return conn.execute("SELECT 1").fetchone()[0] == 1
    except Exception as exc:  # noqa: BLE001
        log.warning("Проверка базы не прошла: %s", exc)
        return False


def _catalog_from_xlsx() -> list[dict]:
    """Аварийный путь: читаем Excel, если JSON ещё не создан ботом."""
    import openpyxl

    if not CATALOG_XLSX.exists():
        return []
    workbook = openpyxl.load_workbook(CATALOG_XLSX, data_only=True)
    rows = list(workbook.active.iter_rows(values_only=True))
    if not rows:
        return []
    header = [str(value or "").strip().lower() for value in rows[0]]

    def column(*names: str) -> int | None:
        for name in names:
            for i, title in enumerate(header):
                if title.startswith(name):
                    return i
        return None

    c_id = column("id")
    c_name = column("название", "наименование")
    c_unit = column("единица")
    c_pack = column("фасовка")
    c_price = column("цена")
    c_cat = column("категория")
    c_stock = column("остаток")
    items: list[dict] = []
    for row in rows[1:]:
        if not row or c_name is None or not row[c_name]:
            continue
        try:
            price = float(row[c_price] or 0) if c_price is not None else 0.0
        except (TypeError, ValueError):
            price = 0.0
        try:
            stock = (
                float(row[c_stock])
                if c_stock is not None and row[c_stock] is not None
                else None
            )
        except (TypeError, ValueError):
            stock = None
        items.append(
            {
                "id": int(row[c_id]) if c_id is not None and row[c_id] else len(items) + 1,
                "name": str(row[c_name]),
                "unit": str(row[c_unit] or "кг") if c_unit is not None else "кг",
                "pack": str(row[c_pack] or "") if c_pack is not None else "",
                "price": round(price, 2),
                "category": str(row[c_cat] or "") if c_cat is not None else "",
                "stock": stock,
            }
        )
    return items


def load_catalog() -> tuple[list[dict], str]:
    """Прайс и время обновления. Основной путь — JSON от бота."""
    try:
        if CATALOG_JSON.exists():
            payload = json.loads(CATALOG_JSON.read_text(encoding="utf-8"))
            items = payload.get("items") or []
            if items:
                return items, str(payload.get("updated") or "")
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Не удалось прочитать %s: %s", CATALOG_JSON, exc)
    return _catalog_from_xlsx(), datetime.now().isoformat(timespec="seconds")


def _user_from_init_data(init_data: str) -> tuple[int, int]:
    """id пользователя и чата из подписанных данных Telegram."""
    pairs = dict(parse_qsl(init_data, strict_parsing=False))
    user = json.loads(pairs.get("user") or "{}")
    user_id = int(user.get("id") or 0)
    return user_id, user_id


def _record_event(user_id: int, kind: str) -> None:
    """Счётчик открытий и заказов из каталога (G5).

    Открытия пишем не чаще раза в 30 минут на человека, чтобы таблица
    не росла от каждого переключения вкладки.
    """
    import psycopg

    url = database_url()
    if not url or not user_id:
        return
    try:
        with psycopg.connect(url) as conn:
            if kind == "open":
                fresh = conn.execute(
                    "SELECT 1 FROM webapp_events WHERE user_id = %s AND kind = 'open' "
                    "AND ts > now() - (%s || ' minutes')::interval LIMIT 1",
                    (int(user_id), str(OPEN_DEDUP_MINUTES)),
                ).fetchone()
                if fresh:
                    return
            conn.execute(
                "INSERT INTO webapp_events(user_id, kind) VALUES (%s, %s)",
                (int(user_id), kind),
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("Событие каталога не записано: %s", exc)


def _insert_order(user_id: int, text: str) -> None:
    """Пишет заказ в базу: бот разберёт его следующим шагом."""
    import psycopg

    url = database_url()
    if not url:
        raise RuntimeError("DATABASE_URL не задан")
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO webapp_orders(user_id, chat_id, text) VALUES (%s, %s, %s)",
            (user_id, user_id, text),
        )


async def create_order(request: web.Request) -> web.Response:
    """Заказ, собранный в каталоге. Подпись Telegram обязательна."""
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.Response(status=400, text="bad json")
    init_data = str(payload.get("initData", ""))
    if not check_init_data(init_data, bot_token()):
        return web.Response(status=403, text="подпись Telegram не проверена")
    text = str(payload.get("items", "")).strip()
    comment = str(payload.get("comment", "")).strip()
    if not text:
        return web.Response(status=400, text="пустой заказ")
    full_text = text if not comment else f"{text}. {comment}"
    user_id, chat_id = _user_from_init_data(init_data)
    if not user_id:
        return web.Response(status=400, text="нет пользователя в данных Telegram")
    await asyncio.to_thread(_insert_order, user_id, full_text)
    await asyncio.to_thread(_record_event, user_id, "order")
    log.info("Заказ из каталога принят от %s: %s", user_id, full_text[:120])
    return web.json_response({"ok": True})


async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(WEB / "index.html")


async def catalog(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.Response(status=400, text="bad json")
    init_data = str(payload.get("initData", ""))
    if not check_init_data(init_data, bot_token()):
        return web.Response(status=403, text="подпись Telegram не проверена")
    items, updated = load_catalog()
    user_id, _ = _user_from_init_data(init_data)
    if user_id:
        await asyncio.to_thread(_record_event, user_id, "open")
    return web.json_response({"items": items, "updated": updated})


async def health(request: web.Request) -> web.Response:
    """Для внешнего мониторинга: 200 — служба и база живы, 503 — нет."""
    ok = await asyncio.to_thread(_db_ok)
    return web.json_response(
        {"ok": ok, "service": "order_bot_web", "ts": datetime.now().isoformat()},
        status=200 if ok else 503,
    )


def main() -> None:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_post("/api/catalog", catalog)
    app.router.add_post("/api/order", create_order)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERT, KEY)
    log.info("Мини-приложение каталога на порту %s", PORT)
    web.run_app(app, host="0.0.0.0", port=PORT, ssl_context=context, access_log=None)


if __name__ == "__main__":
    main()
