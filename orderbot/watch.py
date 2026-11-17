"""Счётчик ошибок в окне времени: чтобы тревога была по факту, а не по grep.

Обычный `journalctl | grep -c ERROR` считает и старые записи, поэтому не
годится как порог. Здесь ошибки считаются в памяти процесса за последние
10 минут и группируются по типу исключения.
"""
from __future__ import annotations

import logging
import time
from collections import Counter, deque

WINDOW_SECONDS = 600


class ErrorWatch(logging.Handler):
    """Хранит время и класс исключения для каждой записи уровня ERROR и выше."""

    def __init__(self, window: int = WINDOW_SECONDS) -> None:
        super().__init__(level=logging.ERROR)
        self.window = window
        self._events: deque[tuple[float, str]] = deque(maxlen=2000)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.exc_info and record.exc_info[0] is not None:
                kind = record.exc_info[0].__name__
            elif record.levelno >= logging.ERROR:
                kind = "Error"
            else:
                return
            self._events.append((time.time(), kind))
            self._trim()
        except Exception:  # noqa: BLE001 — обработчик не должен ронять логирование
            pass

    def _trim(self) -> None:
        border = time.time() - self.window
        while self._events and self._events[0][0] < border:
            self._events.popleft()

    def snapshot(self) -> tuple[int, Counter[str]]:
        """(всего ошибок в окне, счётчик по типам)."""
        self._trim()
        counter: Counter[str] = Counter(kind for _, kind in self._events)
        return len(self._events), counter

    def clear(self) -> None:
        self._events.clear()
