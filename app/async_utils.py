"""Защищённые фоновые задачи: создание, журналирование сбоев, реестр ссылок.

Две проблемы паттерна «просто asyncio.create_task(...)»:
1. Пока слабая ссылка на задачу — она может быть собрана GC до завершения.
2. Исключение внутри задачи глотается с "Task exception was never retrieved"
   — мониторинг не увидит падение фоновой шлифовки.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Coroutine, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)

_TASKS: set[asyncio.Task] = set()


def unwrap_llm_json(result) -> dict | list | None:
    """Разворачивает ответ LLM в распарсенный JSON (объект или массив) либо None.

    `story._chat_completion` возвращает `(payload, model)`, где payload — конверт
    OpenAI вида `{"choices": [{"message": {"content": "<JSON-текст>"}}]}`. Здесь
    вытаскивается `content` и парсится тем же `_extract_json`, что и в основном
    конвейере главы. Уже-распарсенный объект (моки, фолбэки, вызовы без конверта)
    возвращается как есть.
    """
    if not result:
        return None
    raw = result[0] if isinstance(result, tuple) else result
    if not isinstance(raw, dict):
        return None
    choices = raw.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict) and isinstance(first.get("message"), dict):
            content = first["message"].get("content")
            if isinstance(content, str) and content.strip():
                return _llm_json_value(content)
    # Не конверт OpenAI — считаем уже распарсенным объектом.
    return raw


def unwrap_llm_text(result) -> str | None:
    """Возвращает сырой текст из конверта OpenAI (для want_json=False)."""
    if not result:
        return None
    raw = result[0] if isinstance(result, tuple) else result
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, dict):
        return None
    choices = raw.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        first = choices[0]
        message = first.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
        if isinstance(first.get("text"), str):
            return first["text"]
    return None


def _llm_json_value(text: str) -> dict | list | None:
    """Парсит JSON-объект или JSON-массив из ответа модели."""
    from app.story import _extract_json

    stripped = text.strip()
    if stripped.startswith("["):
        try:
            value = json.JSONDecoder().raw_decode(stripped)[0]
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, list) else None
    try:
        parsed = _extract_json(stripped)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


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