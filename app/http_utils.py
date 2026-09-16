"""Общие HTTP-утилиты: GET с ретраями на транзиентные сбои.

Вынесено из ton_watch.py и ton_pay.py (идентичный дубль) — одна точка
реализации backoff'а для внешних REST-узлов (TonAPI, лайтсерверы).
"""

from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

_HTTP_CLIENT: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """Общий HTTP-клиент с переиспользуемым пулом соединений.

    Раньше каждый вызов watcher/сверки/диагностики поднимал собственный
    AsyncClient — DNS+TLS устанавливались заново на каждый запрос даже между
    соседними циклами. Один клиент на процесс даёт keep-alive между циклами
    и единую точку настройки таймаута/лимитов.
    """
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=60.0,
            ),
        )
    return _HTTP_CLIENT


async def close_http_client() -> None:
    """Вежливо гасит общий клиент на выключении процесса (сброс пула)."""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is not None:
        await _HTTP_CLIENT.aclose()
        _HTTP_CLIENT = None


async def http_get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float | None = None,
    max_retries: int = 1,
    retry_delay: float = 1.0,
    backoff_factor: float = 2.0,
    max_delay: float = 30.0,
) -> httpx.Response:
    """HTTP GET с retry для 5xx ошибок и транзиентных таймаутов.

    Пауза между попытками растёт экспоненциально: retry_delay, retry_delay ×
    backoff_factor, ×backoff_factor²… — не выше max_delay. Прежняя постоянная
    задержка долбила молчащий индексатор с одинаковой частотой и не давала
    ему оправиться. timeout — пер-запросный таймаут (None = по умолчанию
    клиента): для «не смеющего тормозить тик» вызовов (энтропия мастерчейна).
    """
    last_exc = None
    for attempt in range(1 + max_retries):
        try:
            request_kwargs: dict = {}
            if timeout is not None:
                request_kwargs["timeout"] = timeout
            response = await client.get(url, params=params, headers=headers, **request_kwargs)
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
