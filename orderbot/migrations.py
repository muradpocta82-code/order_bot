"""Версии схемы БД и раннер миграций.

Каждая миграция применяется ровно один раз и только по порядку.
Новые изменения схемы добавляются сюда новым номером, а не правкой старых.
"""
from __future__ import annotations

from datetime import datetime
from typing import Awaitable, Callable

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS aliases (
    chef_id INTEGER NOT NULL DEFAULT 0,
    phrase TEXT NOT NULL,
    catalog_id INTEGER NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chef_id, phrase)
);

CREATE TABLE IF NOT EXISTS missing_items (
    phrase TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_missing_last_seen ON missing_items(last_seen);

CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drafts_chat_status ON drafts(chat_id, status);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    chef_id INTEGER,
    chef_name TEXT,
    chef_username TEXT,
    manager_chat_id INTEGER,
    delivery_date TEXT,
    delivery_time TEXT,
    comment TEXT,
    total REAL DEFAULT 0,
    status TEXT DEFAULT 'new',
    source TEXT,
    raw_text TEXT,
    notified_at TEXT,
    delivery_address TEXT,
    delivery_phone TEXT,
    payment_method TEXT DEFAULT 'cash',
    payment_status TEXT NOT NULL DEFAULT 'unpaid',
    paid_amount DOUBLE PRECISION,
    paid_at TEXT,
    packed_at TEXT,
    delivering_at TEXT,
    delivered_at TEXT,
    courier_chat_id INTEGER,
    courier_card_message_id INTEGER,
    courier_notified_at TEXT
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    catalog_id INTEGER,
    name TEXT,
    qty REAL,
    unit TEXT,
    price REAL,
    total REAL,
    raw TEXT,
    status TEXT,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
"""


SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS aliases (
    chef_id INTEGER NOT NULL DEFAULT 0,
    phrase TEXT NOT NULL,
    catalog_id INTEGER NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chef_id, phrase)
);

CREATE TABLE IF NOT EXISTS missing_items (
    phrase TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_missing_last_seen ON missing_items(last_seen);

CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drafts_chat_status ON drafts(chat_id, status);

CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    created_at TEXT NOT NULL,
    chef_id BIGINT,
    chef_name TEXT,
    chef_username TEXT,
    manager_chat_id BIGINT,
    delivery_date TEXT,
    delivery_time TEXT,
    comment TEXT,
    total DOUBLE PRECISION DEFAULT 0,
    status TEXT DEFAULT 'new',
    source TEXT,
    raw_text TEXT,
    notified_at TEXT,
    questions INTEGER NOT NULL DEFAULT 0,
    delivery_address TEXT,
    delivery_phone TEXT,
    payment_method TEXT DEFAULT 'cash',
    payment_status TEXT NOT NULL DEFAULT 'unpaid',
    paid_amount DOUBLE PRECISION,
    paid_at TEXT,
    packed_at TEXT,
    delivering_at TEXT,
    delivered_at TEXT,
    courier_chat_id BIGINT,
    courier_card_message_id BIGINT,
    courier_notified_at TEXT
);

CREATE TABLE IF NOT EXISTS order_items (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL,
    catalog_id INTEGER,
    name TEXT,
    qty DOUBLE PRECISION,
    unit TEXT,
    price DOUBLE PRECISION,
    total DOUBLE PRECISION,
    raw TEXT,
    status TEXT,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);

CREATE TABLE IF NOT EXISTS question_log (
    id SERIAL PRIMARY KEY,
    phrase TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_question_log_created ON question_log(created_at);
"""


async def _has_column(
    conn: aiosqlite.Connection, table: str, column: str
) -> bool:
    if getattr(conn, "dialect", "sqlite") == "postgres":
        cursor = await conn.execute(
            "SELECT 1 AS found FROM information_schema.columns "
            "WHERE table_name = ? AND column_name = ?",
            (table, column),
        )
        return await cursor.fetchone() is not None
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    return column in {row["name"] for row in await cursor.fetchall()}


async def _migrate_legacy_aliases(conn: aiosqlite.Connection) -> None:
    """Старые алиасы были глобальными: переносим их как общие (chef_id = 0)."""
    if await _has_column(conn, "aliases", "chef_id"):
        return
    await conn.executescript(
        """
        BEGIN;
        ALTER TABLE aliases RENAME TO aliases_old;
        CREATE TABLE aliases (
            chef_id INTEGER NOT NULL DEFAULT 0,
            phrase TEXT NOT NULL,
            catalog_id INTEGER NOT NULL,
            hits INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (chef_id, phrase)
        );
        INSERT INTO aliases(chef_id, phrase, catalog_id, hits, updated_at)
            SELECT 0, phrase, catalog_id, hits, updated_at FROM aliases_old;
        DROP TABLE aliases_old;
        COMMIT;
        """
    )


async def migration_1_baseline(conn: aiosqlite.Connection) -> None:
    """Базовая схема: все таблицы, старые алиасы, notified_at, черновики."""
    if getattr(conn, "dialect", "sqlite") == "postgres":
        await conn.executescript(SCHEMA_PG)
        await conn.execute(
            "UPDATE drafts SET status = 'draft' WHERE status = 'confirming'"
        )
        return
    await conn.executescript(SCHEMA)
    await _migrate_legacy_aliases(conn)
    if not await _has_column(conn, "orders", "notified_at"):
        # Старые заказы уже были доставлены менеджеру — не пересылаем их.
        await conn.execute("ALTER TABLE orders ADD COLUMN notified_at TEXT")
        await conn.execute("UPDATE orders SET notified_at = created_at")
    # Если процесс упал между claim и create_order, заказ не создан
    # (create_order коммитит статус confirmed атомарно). Такой черновик
    # безопасно вернуть в draft, иначе кнопка подтверждения навсегда залипнет.
    await conn.execute(
        "UPDATE drafts SET status = 'draft' WHERE status = 'confirming'"
    )


async def migration_2_questions(conn: aiosqlite.Connection) -> None:
    """Число вопросов в заказе и журнал фраз, на которых бот переспрашивал."""
    if not await _has_column(conn, "orders", "questions"):
        await conn.execute(
            "ALTER TABLE orders ADD COLUMN questions INTEGER NOT NULL DEFAULT 0"
        )
    if getattr(conn, "dialect", "sqlite") == "postgres":
        # В PG-схеме таблица и колонка уже созданы миграцией №1.
        return
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS question_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phrase TEXT NOT NULL,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_question_log_created
            ON question_log(created_at);
        """
    )


async def migration_3_alias_bigint(conn: aiosqlite.Connection) -> None:
    """chef_id в aliases — BIGINT: Telegram id не влезает в int4 (22003)."""
    if getattr(conn, "dialect", "sqlite") != "postgres":
        return
    await conn.execute("ALTER TABLE aliases ALTER COLUMN chef_id TYPE BIGINT")
    await conn.commit()


async def migration_4_order_cards(conn: aiosqlite.Connection) -> None:
    """id карточек заказа у повара и у менеджера — чтобы убирать плашки."""
    if not await _has_column(conn, "orders", "chef_card_message_id"):
        await conn.execute(
            "ALTER TABLE orders ADD COLUMN chef_card_message_id "
            + ("BIGINT" if getattr(conn, "dialect", "sqlite") == "postgres" else "INTEGER")
        )
    if not await _has_column(conn, "orders", "manager_card_message_id"):
        await conn.execute(
            "ALTER TABLE orders ADD COLUMN manager_card_message_id "
            + ("BIGINT" if getattr(conn, "dialect", "sqlite") == "postgres" else "INTEGER")
        )
    await conn.commit()


async def migration_5_users_stock_prices(conn: aiosqlite.Connection) -> None:
    """Профили поваров, остатки, версии прайса, заметка об остатке в позиции."""
    pg = getattr(conn, "dialect", "sqlite") == "postgres"
    serial = "SERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    bigint = "BIGINT" if pg else "INTEGER"
    await conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS users (
            user_id {bigint} PRIMARY KEY,
            name TEXT,
            username TEXT,
            venue TEXT,
            access TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stock_items (
            catalog_id INTEGER PRIMARY KEY,
            available INTEGER NOT NULL DEFAULT 1,
            stock_left DOUBLE PRECISION,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS price_versions (
            id {serial},
            created_at TEXT NOT NULL,
            path TEXT NOT NULL,
            summary TEXT,
            items INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    if not await _has_column(conn, "order_items", "stock_note"):
        await conn.execute("ALTER TABLE order_items ADD COLUMN stock_note TEXT")
    await conn.commit()


async def migration_6_delivery_payment(conn: aiosqlite.Connection) -> None:
    """Адрес/телефон клиента, доставщик и фиксация оплаты наличными."""
    pg = getattr(conn, "dialect", "sqlite") == "postgres"
    bigint = "BIGINT" if pg else "INTEGER"
    user_columns = (
        ("phone", "TEXT"),
        ("address", "TEXT"),
    )
    order_columns = (
        ("delivery_address", "TEXT"),
        ("delivery_phone", "TEXT"),
        ("payment_method", "TEXT DEFAULT 'cash'"),
        ("payment_status", "TEXT NOT NULL DEFAULT 'unpaid'"),
        ("paid_amount", "DOUBLE PRECISION"),
        ("paid_at", "TEXT"),
        ("packed_at", "TEXT"),
        ("delivering_at", "TEXT"),
        ("delivered_at", "TEXT"),
        ("courier_chat_id", bigint),
        ("courier_card_message_id", bigint),
        ("courier_notified_at", "TEXT"),
    )
    for column, sql_type in user_columns:
        if not await _has_column(conn, "users", column):
            await conn.execute(f"ALTER TABLE users ADD COLUMN {column} {sql_type}")
    for column, sql_type in order_columns:
        if not await _has_column(conn, "orders", column):
            await conn.execute(f"ALTER TABLE orders ADD COLUMN {column} {sql_type}")
    # Исторические доставленные заказы считаем оплаченными, чтобы отчёты
    # не показывали старые закрытые заказы как долг.
    await conn.execute(
        "UPDATE orders SET payment_status = 'paid', payment_method = "
        + ("'legacy'" if pg else "'legacy'")
        + " WHERE status = 'done' AND payment_status = 'unpaid'"
    )
    await conn.commit()


async def migration_8_cash_only(conn: aiosqlite.Connection) -> None:
    """Только наличные: убираем следы онлайн-оплаты."""
    for column in ("payment_url", "payment_session_id"):
        if await _has_column(conn, "orders", column):
            await conn.execute(f"ALTER TABLE orders DROP COLUMN {column}")
    await conn.execute(
        "UPDATE orders SET payment_method = 'cash' "
        "WHERE payment_method IS NULL OR payment_method != 'cash'"
    )
    await conn.commit()


async def migration_9_ton_payments(conn: aiosqlite.Connection) -> None:
    """Счета на оплату USDT в сети TON."""
    pg = getattr(conn, "dialect", "sqlite") == "postgres"
    id_col = "BIGSERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS ton_payments ("
        f"id {id_col}, "
        "order_id BIGINT NOT NULL, "
        "comment TEXT NOT NULL, "
        "amount_usdt TEXT NOT NULL, "
        "amount_nano BIGINT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'pending', "
        "tx_hash TEXT, "
        "created_at TEXT NOT NULL, "
        "expires_at TEXT NOT NULL, "
        "paid_at TEXT)"
    )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_ton_tx ON ton_payments(tx_hash)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ton_pending ON ton_payments(status, order_id)"
    )
    await conn.commit()


async def migration_10_pin_rating(conn: aiosqlite.Connection) -> None:
    """PIN выдачи и оценка заказа."""
    for column, sql_type in (("delivery_pin", "TEXT"), ("rating", "INTEGER")):
        if not await _has_column(conn, "orders", column):
            await conn.execute(f"ALTER TABLE orders ADD COLUMN {column} {sql_type}")
    await conn.commit()


async def migration_11_staff(conn: aiosqlite.Connection) -> None:
    """Сотрудники: несколько упаковщиков и курьеров."""
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS staff ("
        "user_id BIGINT PRIMARY KEY, "
        "role TEXT NOT NULL, "
        "name TEXT, "
        "active INTEGER NOT NULL DEFAULT 1, "
        "created_at TEXT NOT NULL)"
    )
    await conn.commit()


async def migration_12_order_photo(conn: aiosqlite.Connection) -> None:
    """Фото подтверждения сборки/вручения."""
    if not await _has_column(conn, "orders", "photo_file_id"):
        await conn.execute("ALTER TABLE orders ADD COLUMN photo_file_id TEXT")
    await conn.commit()


async def migration_13_claims(conn: aiosqlite.Connection) -> None:
    """Претензии по заказам."""
    pg = getattr(conn, "dialect", "sqlite") == "postgres"
    id_col = "BIGSERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS claims ("
        f"id {id_col}, "
        "order_id INTEGER NOT NULL, "
        "user_id BIGINT, "
        "text TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'open', "
        "created_at TEXT NOT NULL)"
    )
    await conn.commit()


MIGRATIONS: list[tuple[int, Callable[[aiosqlite.Connection], Awaitable[None]]]] = [
    (1, migration_1_baseline),
    (2, migration_2_questions),
    (3, migration_3_alias_bigint),
    (4, migration_4_order_cards),
    (5, migration_5_users_stock_prices),
    (6, migration_6_delivery_payment),
    (8, migration_8_cash_only),
    (9, migration_9_ton_payments),
    (10, migration_10_pin_rating),
    (11, migration_11_staff),
    (12, migration_12_order_photo),
    (13, migration_13_claims),
]

LATEST_VERSION = MIGRATIONS[-1][0]


async def apply_migrations(conn: aiosqlite.Connection) -> int:
    """Применяет недостающие миграции по порядку. Возвращает текущую версию."""
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    await conn.commit()

    cursor = await conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version")
    row = await cursor.fetchone()
    current = int(row["v"]) if row else 0

    for version, migration in MIGRATIONS:
        if version <= current:
            continue
        await migration(conn)
        await conn.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES(?, ?)",
            (version, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        await conn.commit()
        current = version
    return current
