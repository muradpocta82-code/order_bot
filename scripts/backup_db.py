#!/usr/bin/env python3
"""Часовой бэкап боевой базы: отдельная служба, а не процесс бота.

Запускается таймером order_bot_backup.timer от пользователя postgres
(локальное доверие peer, пароли не нужны). Пишет дампы в /var/backups/order_bot,
проверяет читаемость через `pg_restore --list`, держит 48 часовых,
30 дневных и 12 месячных копий.

Бот эти файлы только читает (группа orderbot, права 0640) и раз в сутки
пересылает свежий менеджеру в Telegram.

Запуск: venv/bin/python scripts/backup_db.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DB = os.getenv("BACKUP_DB", "order_bot")
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "/var/backups/order_bot"))
HOURLY = int(os.getenv("BACKUP_KEEP_HOURLY", "48"))
DAILY = int(os.getenv("BACKUP_KEEP_DAILY", "30"))
MONTHLY = int(os.getenv("BACKUP_KEEP_MONTHLY", "12"))


def _run(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def make_dump() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"orderbot-pg-{stamp}.dump"
    temp = target.with_suffix(".dump.part")
    result = _run(
        [
            "pg_dump",
            "--no-owner",
            "--no-privileges",
            "-Fc",
            "-Z",
            "6",
            "-d",
            DB,
            "-f",
            str(temp),
        ]
    )
    if result.returncode != 0:
        temp.unlink(missing_ok=True)
        raise RuntimeError(result.stderr.strip()[-300:] or "pg_dump failed")
    check = _run(["pg_restore", "--list", str(temp)])
    if check.returncode != 0 or "TABLE DATA" not in check.stdout:
        temp.unlink(missing_ok=True)
        raise RuntimeError(
            "дамп не читается: " + (check.stderr.strip()[-300:] or "pg_restore --list")
        )
    temp.chmod(0o640)
    temp.replace(target)
    return target


def prune() -> list[Path]:
    """48 последних часовых + по одной за день (30) + по одной за месяц (12)."""
    files = sorted(BACKUP_DIR.glob("orderbot-pg-*.dump"), key=lambda p: p.stat().st_mtime)
    keep: set[Path] = set(files[-HOURLY:]) if HOURLY > 0 else set()
    days: dict[str, Path] = {}
    months: dict[str, Path] = {}
    for path in files:
        moment = datetime.fromtimestamp(path.stat().st_mtime)
        days[moment.strftime("%Y-%m-%d")] = path
        months[moment.strftime("%Y-%m")] = path
    keep.update(list(days.values())[-DAILY:])
    keep.update(list(months.values())[-MONTHLY:])
    removed: list[Path] = []
    for path in files:
        if path not in keep:
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


def main() -> int:
    started = datetime.now()
    try:
        target = make_dump()
        removed = prune()
        size = target.stat().st_size / 1024
        print(
            f"OK {target.name} {size:.0f} КБ за {(datetime.now() - started).seconds} с, "
            f"удалено старых: {len(removed)}"
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL бэкап не создан: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
