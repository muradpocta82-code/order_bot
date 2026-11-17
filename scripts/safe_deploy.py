#!/usr/bin/env python3
"""Безопасный деплой Order bot на VPS.

Порядок: проверки (py_compile, selftest, integration, eval) -> бэкап кода и БД
-> перезапуск -> ожидание -> проверка службы и логов. Если служба не поднялась
или в логах есть ERROR/CRITICAL — автоматический откат кода и БД.

Запуск на сервере:
    venv/bin/python scripts/safe_deploy.py
    venv/bin/python scripts/safe_deploy.py --wait 30
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from orderbot.backup import backup_postgres  # noqa: E402
from orderbot.config import load_settings  # noqa: E402

PYTHON = BASE_DIR / "venv" / "bin" / "python"
SERVICE = "order_bot"
BACKUP_ROOT = Path("/opt/order_bot/backups_deploy")
CODE_PATHS = (
    "main.py",
    "orderbot",
    "scripts",
    "tests",
    "catalog.xlsx",
    "deploy",
    "README.md",
    "requirements.txt",
)
ERROR_RE = re.compile(r"\|\s*(ERROR|CRITICAL)\s*\|")


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=BASE_DIR, text=True, capture_output=True, timeout=timeout
    )


def _tail(text: str, limit: int = 3500) -> str:
    text = (text or "").strip()
    return text[-limit:]


def _python_files() -> list[Path]:
    files = [BASE_DIR / "main.py"]
    for folder in ("orderbot", "scripts"):
        files.extend(sorted((BASE_DIR / folder).glob("*.py")))
    return files


def _live_start_ok(verbose: bool = True) -> bool:
    """Живой вызов /start и приветствия на реальной БД (без Telegram)."""
    result = _run([str(PYTHON), "scripts/live_start_check.py"], timeout=180)
    if result.returncode != 0:
        if verbose:
            print(_tail(result.stdout + result.stderr))
        return False
    return True


def check_tests() -> bool:
    print("1/4 Проверяю код и тесты…")
    if not os.getenv("TEST_DATABASE_URL", "").strip():
        print("  TEST_DATABASE_URL не задан: тесты не могут пройти на PostgreSQL.")
        print("  Деплой отменён — SQLite-тесты не ловят ошибки диалекта.")
        return False
    print("  БД тестов: PostgreSQL (TEST_DATABASE_URL)")
    result = _run(
        [str(PYTHON), "-m", "py_compile", *[str(p) for p in _python_files()]]
    )
    if result.returncode != 0:
        print("  py_compile упал:\n" + _tail(result.stderr))
        return False
    print("  py_compile: OK")

    checks = (
        ("selftest", [str(PYTHON), "scripts/selftest.py"]),
        ("integration", [str(PYTHON), "scripts/integration_test.py"]),
        ("delivery_flow", [str(PYTHON), "scripts/delivery_flow_check.py"]),
        ("eval", [str(PYTHON), "scripts/eval_parse.py", "--local-only"]),
    )
    for name, command in checks:
        result = _run(command)
        if result.returncode != 0:
            print(f"  {name} упал:\n" + _tail(result.stdout + result.stderr))
            return False
        print(f"  {name}: OK")

    # /start на боевой БД проверяем ДО рестарта: поломка не уронит службу.
    if not _live_start_ok():
        print("  живая проверка /start упала")
        return False
    print("  живой /start: OK")
    return True


def make_backup(settings) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = BACKUP_ROOT / f"deploy_{stamp}"
    target.mkdir(parents=True, exist_ok=False)

    with tarfile.open(target / "code.tar.gz", "w:gz") as archive:
        for name in CODE_PATHS:
            path = BASE_DIR / name
            if path.exists():
                archive.add(path, arcname=name)

    if settings.database_url:
        backup_postgres(settings.database_url, target, keep=14)
        return target

    source = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True, timeout=30)
    destination = sqlite3.connect(target / "orderbot.db")
    try:
        source.backup(destination)
        check = destination.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0] != "ok":
            raise RuntimeError(f"integrity_check: {check}")
    finally:
        source.close()
        destination.close()
    return target


def _unit_ok() -> bool:
    path = Path("/etc/systemd/system/order_bot.service")
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    return "Restart=always" in text and "RestartSec=5" in text


def _service_active() -> bool:
    result = _run(["systemctl", "is-active", SERVICE], timeout=30)
    return result.stdout.strip() == "active"


def _new_errors(since: str) -> list[str]:
    result = _run(
        ["journalctl", "-u", SERVICE, "--since", since, "--no-pager"],
        timeout=60,
    )
    return [line for line in result.stdout.splitlines() if ERROR_RE.search(line)]


def rollback(backup_dir: Path, settings) -> None:
    print("!!! Откат кода и БД из резервной копии…")
    _run(["systemctl", "stop", SERVICE], timeout=60)
    with tarfile.open(backup_dir / "code.tar.gz", "r:gz") as archive:
        archive.extractall(BASE_DIR, filter="data")
    sqlite_copy = backup_dir / "orderbot.db"
    if settings.database_url:
        print("    PostgreSQL: откат данных не автоматический, см. .sql в бэкапе")
    elif sqlite_copy.exists():
        for suffix in ("-wal", "-shm"):
            Path(str(settings.db_path) + suffix).unlink(missing_ok=True)
        shutil.copy2(sqlite_copy, settings.db_path)
    _run(["systemctl", "start", SERVICE], timeout=60)
    time.sleep(5)
    print("    служба после отката:", "active" if _service_active() else "НЕ active")


def deploy(backup_dir: Path, settings, wait: int) -> bool:
    since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("3/4 Перезапускаю службу…")
    result = _run(["systemctl", "restart", SERVICE], timeout=60)
    if result.returncode != 0:
        print("  restart не удался: " + _tail(result.stderr))
        rollback(backup_dir, settings)
        return False

    print(f"    жду {wait} с и проверяю…")
    time.sleep(wait)
    active = _service_active()
    errors = _new_errors(since)
    if not active or errors:
        print(f"  служба: {'active' if active else 'НЕ active'}")
        for line in errors[:10]:
            print("  " + line)
        rollback(backup_dir, settings)
        return False
    print("    служба active, ошибок в логах нет")
    print("    проверяю живой /start на боевой БД…")
    if not _live_start_ok():
        print("  живой /start не отвечает — откатываю")
        rollback(backup_dir, settings)
        return False
    print("    живой /start: OK")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Безопасный деплой бота")
    parser.add_argument("--wait", type=int, default=30, help="пауза после рестарта")
    args = parser.parse_args()

    settings = load_settings()
    print("=" * 60)
    print("Безопасный деплой", BASE_DIR)
    print("=" * 60)
    if not _unit_ok():
        print("ВНИМАНИЕ: в юните нет Restart=always/RestartSec=5")

    if not check_tests():
        print("Проверки не прошли — деплой отменён, служба не тронута.")
        return 1

    print("2/4 Делаю бэкап кода и БД…")
    try:
        backup_dir = make_backup(settings)
    except Exception as exc:
        print(f"Бэкап не удался: {exc}. Деплой отменён.")
        return 1
    print("    " + str(backup_dir))

    if not deploy(backup_dir, settings, max(5, args.wait)):
        print("Деплой не удался — выполнен откат.")
        return 2

    print("4/4 Готово: деплой успешен.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
