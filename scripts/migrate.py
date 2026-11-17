"""Раннер миграций: .sql-файлы, одна транзакция на файл, advisory-lock."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg import ClientCursor

BASE_DIR = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = BASE_DIR / "migrations"
load_dotenv(BASE_DIR / ".env")
LOCK_ID = 727001
BASELINE = 1


def dsn_from_env() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        sys.exit("DATABASE_URL не задан")
    return url


def ensure_registry(conn: psycopg.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version integer PRIMARY KEY, "
        "name text NOT NULL, "
        "applied_at timestamptz NOT NULL DEFAULT now())"
    )


def applied_versions(conn: psycopg.Connection) -> set[int]:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {int(row[0]) for row in rows}


def files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def version_of(path: Path) -> int:
    match = re.match(r"^(\d+)", path.name)
    if not match:
        sys.exit(f"Имя файла должно начинаться с номера: {path.name}")
    return int(match.group(1))


def stamp_baseline(conn: psycopg.Connection, applied: set[int]) -> set[int]:
    """Прод уже имеет схему версии 13 — помечаем baseline применённым."""
    if BASELINE in applied:
        return applied
    exists = conn.execute(
        "SELECT to_regclass('public.orders') IS NOT NULL"
    ).fetchone()[0]
    if not exists:
        return applied
    conn.execute(
        "INSERT INTO schema_migrations(version, name) VALUES (%s, %s) "
        "ON CONFLICT (version) DO NOTHING",
        (BASELINE, "0001_baseline.sql"),
    )
    print("baseline 0001 помечен как применённый (схема уже есть)")
    return applied | {BASELINE}


def apply_file(conn: psycopg.Connection, path: Path, version: int) -> None:
    sql = path.read_text(encoding="utf-8")
    if not sql.strip():
        return
    no_tx = sql.lstrip().startswith("-- no-transaction")
    print(f"-> {path.name}")
    if no_tx:
        old = conn.autocommit
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
        finally:
            conn.autocommit = old
        conn.execute(
            "INSERT INTO schema_migrations(version, name) VALUES (%s, %s)",
            (version, path.name),
        )
        return
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.execute(
            "INSERT INTO schema_migrations(version, name) VALUES (%s, %s)",
            (version, path.name),
        )


def main() -> int:
    dsn = dsn_from_env()
    with psycopg.connect(dsn, autocommit=True, cursor_factory=ClientCursor) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            ensure_registry(conn)
            applied = applied_versions(conn)
            applied = stamp_baseline(conn, applied)
            pending = [p for p in files() if version_of(p) not in applied]
            if not pending:
                print("Нечего применять, схема актуальна")
                return 0
            for path in pending:
                apply_file(conn, path, version_of(path))
            print(f"Применено файлов: {len(pending)}")
            return 0
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))


if __name__ == "__main__":
    raise SystemExit(main())
