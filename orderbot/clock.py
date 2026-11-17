"""Единая точка работы со временем.

В базе время хранится как timestamptz (момент времени), а в коде проекта —
как местная строка "ГГГГ-ММ-ДД ЧЧ:ММ:СС". Переводят эти два мира только
здесь, чтобы не расползались разные часовые пояса и форматы.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo

TZ_NAME = (os.getenv("TZ") or "").strip() or "Asia/Tashkent"
TZ: tzinfo = ZoneInfo(TZ_NAME)

DB_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Строка ровно вида "2026-11-14 18:51:43" — это время из БД.
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def now() -> datetime:
    """Текущий момент в часовом поясе проекта."""
    return datetime.now(TZ)


def now_str() -> str:
    """Текущее время строкой в формате БД (местное)."""
    return now().strftime(DB_TIME_FORMAT)


def looks_like_time(value: object) -> bool:
    return isinstance(value, str) and bool(_TS_RE.match(value))


def to_db(value: object) -> object:
    """Строка времени -> момент времени с поясом; остальное без изменений."""
    if looks_like_time(value):
        assert isinstance(value, str)
        return datetime.strptime(value, DB_TIME_FORMAT).replace(tzinfo=TZ)
    return value


def from_db(value: object) -> object:
    """Момент времени из базы -> местная строка; остальное без изменений."""
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=TZ)
        return moment.astimezone(TZ).strftime(DB_TIME_FORMAT)
    return value


def to_db_params(params) -> tuple:
    return tuple(to_db(value) for value in (params or ()))
