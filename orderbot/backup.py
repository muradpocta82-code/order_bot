"""Горячий бэкап SQLite без остановки бота."""
from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def backup_database(db_path: str | Path, backup_dir: str | Path, keep: int = 14) -> Path:
    """Делает согласованный снимок БД через sqlite3 backup API.

    Безопасно при включённом WAL: читает БД целиком, не блокируя запись.
    """
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    if not db_path.exists():
        raise FileNotFoundError(f"База не найдена: {db_path}")
    if keep < 1:
        raise ValueError("keep должен быть >= 1")

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"orderbot-{stamp}.db"
    suffix = 1
    while target.exists():
        target = backup_dir / f"orderbot-{stamp}-{suffix}.db"
        suffix += 1

    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
        check = destination.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0] != "ok":
            raise RuntimeError(f"бэкап не прошёл integrity_check: {check}")
    except Exception:
        source.close()
        destination.close()
        target.unlink(missing_ok=True)
        raise
    else:
        source.close()
        destination.close()

    backups = sorted(backup_dir.glob("orderbot-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[keep:]:
        try:
            old.unlink()
        except OSError:
            log.warning("Не удалось удалить старый бэкап %s", old)
    return target


HOURLY_KEEP = 48   # снимков каждый час
DAILY_KEEP = 30    # по одному снимку в день


def _prune(backup_dir: Path, pattern: str = "orderbot-pg-*.dump") -> None:
    """Оставляет часовые снимки за двое суток и по одному в день на месяц."""
    files = sorted(
        backup_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True
    )
    now = time.time()
    keep: set[Path] = set(files[:HOURLY_KEEP])
    seen_days: set[str] = set()
    for path in files:
        day = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
        if day in seen_days:
            continue
        seen_days.add(day)
        if len(seen_days) <= DAILY_KEEP:
            keep.add(path)
    for path in files:
        if path in keep:
            continue
        try:
            path.unlink()
        except OSError:
            log.warning("Не удалось удалить старый бэкап %s", path)


def backup_postgres(url: str, backup_dir: str | Path, keep: int = HOURLY_KEEP) -> Path:
    """Снимок PostgreSQL в сжатом формате (-Fc) с проверкой читаемости.

    Сразу после дампинга проверяем файл (`pg_restore --list`): битый снимок
    удаляется, чтобы не хранить пустую надежду.
    """
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError("Некорректный DATABASE_URL")
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"orderbot-pg-{stamp}.dump"
    suffix = 1
    while target.exists():
        target = backup_dir / f"orderbot-pg-{stamp}-{suffix}.dump"
        suffix += 1
    env = dict(os.environ)
    env["PGPASSWORD"] = parsed.password or ""
    command = [
        "pg_dump",
        "--no-owner",
        "--no-privileges",
        "-Fc",
        "-Z",
        "6",
        "-h",
        parsed.hostname,
        "-p",
        str(parsed.port or 5432),
        "-U",
        parsed.username or "postgres",
        "-d",
        (parsed.path or "/postgres").lstrip("/") or "postgres",
        "-f",
        str(target),
    ]
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        raise RuntimeError(result.stderr.strip()[-300:] or "pg_dump failed")
    check = subprocess.run(
        ["pg_restore", "--list", str(target)], capture_output=True, text=True
    )
    if check.returncode != 0 or "TABLE DATA" not in check.stdout:
        target.unlink(missing_ok=True)
        raise RuntimeError(
            "бэкап не читается: " + (check.stderr.strip()[-300:] or "pg_restore --list")
        )
    _prune(backup_dir)
    files = sorted(backup_dir.glob("orderbot-pg-*.dump"))
    keep = max(int(keep), 1)
    return target
