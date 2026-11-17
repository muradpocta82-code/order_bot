#!/usr/bin/env python3
"""Восстановление БД бота из файла бэкапа одной командой.

Примеры:
    venv/bin/python scripts/restore.py data/backups/orderbot-20261106-130529.db
    venv/bin/python scripts/restore.py backup.db --db data/restore_test.db
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from orderbot.config import load_settings  # noqa: E402

SERVICE = "order_bot"


def check_backup(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Бэкап не найден: {path}")
    if path.suffix == ".sql":
        text = path.read_text(encoding="utf-8", errors="replace")[:20000]
        if path.stat().st_size == 0 or "CREATE TABLE" not in text:
            raise RuntimeError("SQL-дамп пустой или повреждён")
        return
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"Бэкап повреждён: {result}")
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        conn.close()
    if "orders" not in tables:
        raise RuntimeError("В файле нет таблицы orders — это не база бота")


def _copy(source: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


def _restore_sql(backup: Path, url: str) -> None:
    from urllib.parse import urlparse

    import os

    parsed = urlparse(url)
    env = dict(os.environ)
    env["PGPASSWORD"] = parsed.password or ""
    command = [
        "psql",
        "-h",
        parsed.hostname or "127.0.0.1",
        "-p",
        str(parsed.port or 5432),
        "-U",
        parsed.username or "postgres",
        "-d",
        (parsed.path or "/postgres").lstrip("/") or "postgres",
        "-v",
        "ON_ERROR_STOP=1",
        "-f",
        str(backup),
    ]
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[-400:] or "psql restore failed")


def restore(backup: Path, target: Path, live: bool) -> Path | None:
    check_backup(backup)
    if backup.suffix == ".sql":
        settings = load_settings()
        if not settings.database_url:
            raise RuntimeError("Для .sql нужен DATABASE_URL в .env")
        if live:
            subprocess.run(["systemctl", "stop", SERVICE], check=False, timeout=60)
        try:
            _restore_sql(backup, settings.database_url)
        finally:
            if live:
                subprocess.run(["systemctl", "start", SERVICE], check=False, timeout=60)
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    safety: Path | None = None
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safety = target.with_name(f"{target.stem}.before-restore-{stamp}.db")
        _copy(target, safety)

    if live:
        subprocess.run(["systemctl", "stop", SERVICE], check=False, timeout=60)
    try:
        for suffix in ("-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
        _copy(backup, target)
        check_backup(target)
    finally:
        if live:
            subprocess.run(["systemctl", "start", SERVICE], check=False, timeout=60)
    return safety


def main() -> int:
    parser = argparse.ArgumentParser(description="Восстановление БД из бэкапа")
    parser.add_argument("backup", type=Path, help="файл бэкапа .db")
    parser.add_argument("--db", type=Path, default=None, help="куда восстановить")
    args = parser.parse_args()

    settings = load_settings()
    target = args.db or Path(settings.db_path)
    live = target.resolve() == Path(settings.db_path).resolve()
    try:
        safety = restore(args.backup, target, live)
    except Exception as exc:
        print(f"Восстановление не удалось: {exc}")
        return 1
    print(f"Готово: {target}")
    if safety:
        print(f"Копия прежней базы: {safety}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
