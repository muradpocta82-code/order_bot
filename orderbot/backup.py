"""Бэкап PostgreSQL: SQLite-ветка удалена 19.11.2026 (I2)."""
from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)


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
