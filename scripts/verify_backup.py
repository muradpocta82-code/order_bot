#!/usr/bin/env python3
"""Проверка бэкапа: восстанавливаем последний pg_dump во временную БД.

Сверяем число заказов и удаляем временную БД. Ничего в боевой базе не меняем.
Запуск: venv/bin/python scripts/verify_backup.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from orderbot.config import load_settings  # noqa: E402

VERIFY_DB = "order_bot_verify"


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _psql(url: str, query: str, as_postgres: bool = False) -> str:
    if as_postgres:
        cmd = ["sudo", "-u", "postgres", "psql", "-d", VERIFY_DB, "-tAc", query]
    else:
        cmd = ["psql", url, "-tAc", query]
    result = _run(cmd)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "psql failed")
    return result.stdout.strip()


def main() -> int:
    settings = load_settings()
    if not settings.database_url:
        print("SQLite — проверка бэкапа PostgreSQL пропущена")
        return 0
    folders = [Path("/var/backups/order_bot"), settings.data_dir / "backups"]
    found = [
        f
        for folder in folders
        if folder.is_dir()
        for f in folder.glob("orderbot-pg-*")
        if f.suffix in (".dump", ".sql")
    ]
    found.sort(key=lambda f: f.stat().st_mtime)
    if not found:
        print("FAIL: нет файлов orderbot-pg-*")
        return 1
    dump = found[-1]
    if dump.stat().st_size < 1000:
        print(f"FAIL: дамп слишком мал: {dump}")
        return 1

    _run(["sudo", "-u", "postgres", "dropdb", "--if-exists", VERIFY_DB])
    created = _run(["sudo", "-u", "postgres", "createdb", "-O", "order_bot", VERIFY_DB])
    if created.returncode != 0:
        print("FAIL: не удалось создать временную БД: " + created.stderr.strip())
        return 1
    # postgres не имеет доступа в /root, поэтому дамп копируем в читаемый /tmp.
    fd, temp_name = tempfile.mkstemp(
        prefix="orderbot-verify-", suffix=dump.suffix, dir="/tmp"
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        shutil.copyfile(dump, temp_path)
        os.chmod(temp_path, 0o644)
        if dump.suffix == ".dump":
            restore = _run(
                [
                    "sudo",
                    "-u",
                    "postgres",
                    "pg_restore",
                    "--no-owner",
                    "--no-privileges",
                    "-d",
                    VERIFY_DB,
                    str(temp_path),
                ]
            )
        else:
            restore = _run(
                [
                    "sudo",
                    "-u",
                    "postgres",
                    "psql",
                    "-q",
                    "-d",
                    VERIFY_DB,
                    "-f",
                    str(temp_path),
                ]
            )
        if restore.returncode != 0:
            print("FAIL: восстановление: " + (restore.stderr.strip()[-800:]))
            return 1
        restored = int(_psql("", "SELECT COUNT(*) FROM orders", as_postgres=True))
        live = int(_psql(settings.database_url, "SELECT COUNT(*) FROM orders"))
        if restored != live:
            print(f"FAIL: заказов в дампе {restored}, в боевой {live}")
            return 1
        print(f"OK: {dump.name} восстановлен, заказов {restored}")
        return 0
    finally:
        _run(["sudo", "-u", "postgres", "dropdb", "--if-exists", VERIFY_DB])
        temp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
