"""Состояние процесса для /health: время старта и последняя ошибка (I1)."""
from __future__ import annotations

import time

BOT_STARTED_AT = time.time()
_last_error: dict[str, float | str] = {"at": 0.0, "text": ""}


def record_error(exc: BaseException) -> None:
    """Запоминает последнюю необработанную ошибку для /health."""
    _last_error["at"] = time.time()
    _last_error["text"] = type(exc).__name__
