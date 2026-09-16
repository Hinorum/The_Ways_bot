"""Общие HTTP-утилиты: GET с ретраями на транзиентные сбои.

Вынесено из ton_watch.py и ton_pay.py (идентичный дубль) — одна точка
реализации backoff'а для внешних REST-узлов (TonAPI, лайтсерверы).
"""

from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)


async def http_get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    max_retries: int = 1,
    retry_delay: float = 1.0,
    backoff_factor: float = 2.0,
    max_delay: float = 30.0,
) -> httpx.Response:
    """HTTP GET с retry для 5xx ошибок и транзиентных таймаутов.

    Пауза между попытками растёт экспоненциально: retry_delay, retry_delay ×
    backoff_factor, ×backoff_factor²… — не выше max_delay. Прежняя постоянная
    задержка долбила молчащий индексатор с одинаковой частотой и не давала
    ему оправиться.
    """
    last_exc = None
    for attempt in range(1 + max_retries):
        try:
            response = await client.get(url, params=params, headers=headers)
            if response.status_code < 500 or attempt == max_retries:
                return response
            delay = min(max_delay, retry_delay * (backoff_factor**attempt))
            logger.warning(
                "HTTP %d от %s (попытка %d/%d), повтор через %.1fs",
                response.status_code, url, attempt + 1, 1 + max_retries, delay,
            )
            await asyncio.sleep(delay)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt == max_retries:
                raise
            delay = min(max_delay, retry_delay * (backoff_factor**attempt))
            logger.warning(
                "HTTP ошибка %s от %s (попытка %d/%d), повтор через %.1fs",
                exc, url, attempt + 1, 1 + max_retries, delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]
