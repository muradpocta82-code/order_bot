#!/usr/bin/env python3
"""Диагностика развёртывания: ключи, Telegram, прайс, ffmpeg, Vosk, сервис."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from orderbot.catalog import Catalog  # noqa: E402
from orderbot.config import load_settings  # noqa: E402

OK = "✅"
FAIL = "❌"
WARN = "⚠️"


def _unit_prop(unit: str, prop: str) -> str:
    """Свойство systemd-юнита (пустая строка, если не спросить)."""
    try:
        result = subprocess.run(
            ["systemctl", "show", unit, f"--property={prop}", "--value"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _applied_migrations(url: str) -> list[int]:
    if not url:
        return []
    import psycopg

    try:
        with psycopg.connect(url, connect_timeout=5) as conn:
            rows = conn.execute("SELECT version::int FROM schema_migrations").fetchall()
        return sorted(int(row[0]) for row in rows)
    except Exception:  # noqa: BLE001
        return []


def check_deployment(settings) -> bool:
    """Сверяет факты развёртывания с ожиданиями: молчаливый дрейф — главная беда."""
    problems = 0

    user = _unit_prop("order_bot", "User") or "root"
    if user == "root":
        print(f"{FAIL} бот работает от root — нужен User=orderbot")
        problems += 1
    else:
        print(f"{OK} пользователь бота: {user}")

    web_user = _unit_prop("order_bot_web", "User") or "root"
    print(f"{OK if web_user != 'root' else FAIL} пользователь каталога: {web_user}")
    problems += int(web_user == "root")

    env_file = BASE_DIR / ".env"
    if env_file.exists():
        mode = env_file.stat().st_mode & 0o777
        import pwd

        owner_name = pwd.getpwuid(env_file.stat().st_uid).pw_name
        if mode & 0o077:
            print(f"{FAIL} .env доступен другим (права {mode:o})")
            problems += 1
        else:
            print(f"{OK} .env: права {mode:o}, владелец {owner_name}")

    dump_files = [
        p
        for folder in (Path("/var/backups/order_bot"), BASE_DIR / "data" / "backups")
        if folder.is_dir()
        for p in folder.glob("orderbot-pg-*.dump")
    ]
    backups = sorted(dump_files, key=lambda p: p.stat().st_mtime)
    if not backups:
        print(f"{FAIL} бэкапов базы нет")
        problems += 1
    else:
        age = datetime.now() - datetime.fromtimestamp(backups[-1].stat().st_mtime)
        fresh = age < timedelta(hours=2)
        print(
            f"{OK if fresh else FAIL} последний бэкап: {age.total_seconds() / 3600:.1f} ч назад "
            f"({backups[-1]})"
        )
        problems += int(not fresh)

    disk = shutil.disk_usage(BASE_DIR)
    free_pct = disk.free / disk.total * 100
    print(f"{OK if free_pct > 20 else WARN} свободно на диске: {free_pct:.0f} %")
    problems += int(free_pct <= 20)

    swap_ok = len(Path("/proc/swaps").read_text(encoding="utf-8").splitlines()) > 1
    print(f"{OK if swap_ok else WARN} swap: {'есть' if swap_ok else 'нет'}")

    # A4: внешний мониторинг (Healthchecks). Пока не настроен — предупреждение, не ошибка.
    heartbeat = (os.getenv("HEARTBEAT_URL") or "").strip()
    if not heartbeat:
        try:
            for line in (BASE_DIR / ".env").read_text(encoding="utf-8").splitlines():
                if line.startswith("HEARTBEAT_URL="):
                    heartbeat = line.split("=", 1)[1].strip()
                    break
        except OSError:
            pass
    print(
        f"{OK if heartbeat else WARN} внешний мониторинг (HEARTBEAT_URL): "
        f"{'подключён' if heartbeat else 'не настроен — docs/MONITORING.md'}"
    )

    # A2: копия бэкапа вне сервера (rclone). Пока не настроена — предупреждение.
    offsite = (os.getenv("OFFSITE_REMOTE") or "").strip()
    if not offsite:
        try:
            for line in (BASE_DIR / ".env").read_text(encoding="utf-8").splitlines():
                if line.startswith("OFFSITE_REMOTE="):
                    offsite = line.split("=", 1)[1].strip()
                    break
        except OSError:
            pass
    print(
        f"{OK if offsite else WARN} копия бэкапа вне сервера: "
        f"{'настроена' if offsite else 'не настроена — docs/OFFSITE_BACKUP.md'}"
    )

    # F2: выгрузка в Google Sheets (не блокирует работу бота).
    sa_file = (os.getenv("GOOGLE_SA_FILE") or "").strip()
    sheets_id = (os.getenv("SHEETS_ID") or "").strip()
    if not sa_file or not sheets_id:
        try:
            for line in (BASE_DIR / ".env").read_text(encoding="utf-8").splitlines():
                if line.startswith("GOOGLE_SA_FILE=") and not sa_file:
                    sa_file = line.split("=", 1)[1].strip()
                if line.startswith("SHEETS_ID=") and not sheets_id:
                    sheets_id = line.split("=", 1)[1].strip()
        except OSError:
            pass
    sheets_ok = bool(sa_file and sheets_id and (BASE_DIR / sa_file).is_file())
    print(
        f"{OK if sheets_ok else WARN} выгрузка в Google Sheets: "
        f"{'настроена' if sheets_ok else 'не настроена — /sheets, docs/SHEETS.md'}"
    )

    files = sorted(
        int(p.name[:4])
        for p in (BASE_DIR / "migrations").glob("*.sql")
        if p.name[:4].isdigit()
    )
    applied = _applied_migrations(settings.database_url)
    if files and applied:
        last_applied, last_file = max(applied), max(files)
        ok = last_applied == last_file
        print(
            f"{OK if ok else FAIL} миграции: последняя применённая {last_applied}, "
            f"последняя в файлах {last_file} (применено {len(applied)})"
        )
        problems += int(not ok)
    else:
        print(f"{WARN} миграции: не удалось сверить список")

    try:
        listening = subprocess.run(
            ["ss", "-Htln"], capture_output=True, text=True, timeout=10
        ).stdout
        web_ok = ":8443" in listening
        print(f"{OK if web_ok else FAIL} каталог слушает порт 8443")
        problems += int(not web_ok)
        expected = {22: "SSH", 443: "VPN (wstunnel, чужой сервис)"}
        for port, title in expected.items():
            open_ok = f":{port} " in listening or f":{port}\n" in listening
            print(f"{OK if open_ok else WARN} порт {port} ({title})")
        ports: set[int] = set()
        for line in listening.splitlines():
            fields = line.split()
            if len(fields) < 4:
                continue
            local = fields[3]
            if local.startswith("127.") or local in ("::1", "[::1]", "::1:*"):
                continue  # локальные службы (DNS и т.п.) не считаем
            tail = local.rsplit(":", 1)[-1]
            if tail.isdigit():
                ports.add(int(tail))
        extra = sorted(ports)
        known = {22, 443, 8443, 2096, 52077, 5432}
        unknown = [port for port in extra if port not in known]
        if unknown:
            print(f"{WARN} незнакомые порты слушают: {unknown} — проверьте, что это ваше")
    except Exception as exc:  # noqa: BLE001
        print(f"{WARN} порты: {exc}")

    return problems == 0


def http_json(url: str, headers: dict[str, str] | None = None, timeout: int = 15):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body[:300]}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def main() -> int:
    settings = load_settings()
    print("=" * 70)
    print("ДИАГНОСТИКА ORDER BOT")
    print("=" * 70)
    print(f"Python: {sys.version.split()[0]}")
    print(f"Каталог: {settings.catalog_path}")
    print(f"База:    {settings.db_path}")
    print(f"Часовой пояс: {settings.timezone_name}")

    ffmpeg = shutil.which("ffmpeg")
    print(f"{OK if ffmpeg else FAIL} ffmpeg: {ffmpeg or 'не найден'}")
    if ffmpeg:
        try:
            version = subprocess.run(
                [ffmpeg, "-version"], capture_output=True, text=True, timeout=10
            ).stdout.splitlines()[0]
            print(f"   {version}")
        except Exception as exc:  # noqa: BLE001
            print(f"   {WARN} не удалось получить версию: {exc}")

    try:
        catalog = Catalog.load(settings.catalog_path)
        print(f"{OK} Прайс: {len(catalog.items)} позиций")
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} Прайс: {exc}")
        return 1

    if settings.vosk_model_path is not None:
        size = sum(p.stat().st_size for p in settings.vosk_model_path.rglob("*") if p.is_file())
        print(f"{OK} Vosk: {settings.vosk_model_path} ({size / 1024 / 1024:.0f} МБ)")
    else:
        print(f"{WARN} Vosk: модель не найдена — голос только через Gemini")

    disk = shutil.disk_usage(BASE_DIR)
    print(f"{OK} Диск: свободно {disk.free / 1024 ** 3:.1f} ГБ из {disk.total / 1024 ** 3:.1f} ГБ")

    print("-" * 70)
    print("TELEGRAM")
    if not settings.bot_token:
        print(f"{FAIL} BOT_TOKEN не задан")
        telegram_ok = False
    else:
        status, data = http_json(f"https://api.telegram.org/bot{settings.bot_token}/getMe")
        if status == 200 and data.get("ok"):
            result = data["result"]
            print(f"{OK} @{result.get('username')} (id={result.get('id')})")
            telegram_ok = True
        else:
            print(f"{FAIL} HTTP {status}: {data.get('description') or data}")
            telegram_ok = False

    print("-" * 70)
    print("DEEPSEEK")
    if not settings.deepseek_api_key:
        print(f"{WARN} ключ не задан — будет локальный разбор текста")
    else:
        status, data = http_json(
            f"{settings.deepseek_base_url.rstrip('/')}/models",
            {"Authorization": f"Bearer {settings.deepseek_api_key}"},
        )
        if status == 200:
            print(f"{OK} ключ принят")
        else:
            print(f"{FAIL} HTTP {status}: {str(data)[:300]}")

    print("-" * 70)
    print("GEMINI")
    if not settings.gemini_api_key:
        print(f"{WARN} ключ не задан — голос будет через Vosk")
    else:
        status, data = http_json(
            "https://generativelanguage.googleapis.com/v1beta/models",
            {"x-goog-api-key": settings.gemini_api_key},
        )
        if status == 200:
            print(f"{OK} ключ принят")
        else:
            error = data.get("error", data)
            print(f"{FAIL} HTTP {status}: {error}")

    print("-" * 70)
    print("СЕРВИС")
    try:
        active = subprocess.run(
            ["systemctl", "is-active", "order_bot"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        enabled = subprocess.run(
            ["systemctl", "is-enabled", "order_bot"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        print(f"{OK if active == 'active' else WARN} order_bot: {active} / {enabled}")
    except Exception as exc:  # noqa: BLE001
        print(f"{WARN} systemctl: {exc}")

    print("-" * 70)
    print("РАЗВЁРТЫВАНИЕ")
    deploy_ok = check_deployment(settings)

    print("=" * 70)
    if telegram_ok and deploy_ok:
        print(f"{OK} Бот может работать: токен Telegram действителен, развёртывание в порядке.")
        return 0
    if not telegram_ok:
        print(f"{FAIL} Telegram недоступен: замените BOT_TOKEN в .env и перезапустите сервис.")
    if not deploy_ok:
        print(f"{FAIL} Есть отличия от ожидаемого развёртывания — смотрите строки ❌ выше.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
