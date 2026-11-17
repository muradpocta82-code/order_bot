"""Небольшое разделяемое состояние между модулями.

Токены подтверждения цены живут в памяти процесса: после перезапуска
блока подтверждения просто нет, и это безопасное поведение.
"""
from __future__ import annotations

from typing import Any

pending_prices: dict[str, dict[str, Any]] = {}
