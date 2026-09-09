"""Защищённые фоновые задачи: создание, журналирование сбоев, реестр ссылок.

Две проблемы паттерна «просто asyncio.create_task(...)»:
1. Пока слабая ссылка на задачу — она может быть собрана GC до завершения.
2. Исключение внутри задачи глотается с "Task exception was never retrieved"
   — мониторинг не увидит падение фоновой шлифовки.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Coroutine, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)

_TASKS: set[asyncio.Task] = set()


def _done_callback(label: str, task: asyncio.Task) -> None:
    _TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Фоновая задача %s упала: %s", label, exc, exc_info=exc)


def spawn(coro: Coroutine, label: str) -> asyncio.Task:
    """Создаёт фоновую задачу с реестром ссылок и журнализацией сбоев."""
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(lambda finished: _done_callback(label, finished))
    return task


def safe_task(label: str | None = None) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Декоратор для async-функций, исполняемых в фоне.

    Логгирует сбой вместо «never retrieved»; одновременно запускает и ждёт
    результат при прямом await — совместимо и с фоном, и со стартом.
    """

    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        async def wrapper(*args, **kwargs) -> T:
            try:
                return await fn(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("Фоновая задача %s упала", label or fn.__name__, exc_info=True)
                raise

        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
        return wrapper

    return decorator