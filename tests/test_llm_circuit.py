"""Выключатель LLM-провайдеров: цепочка сбоев уводит хост на паузу.

Проверяем и сами переходы _breaker_status/_breaker_note, и поведение
`_chat_completion`: открытый выключатель не делает сетевых запросов вовсе,
успешный ответ закрывает выключатель.
"""

import httpx
import pytest

from app import story
from app.config import settings

# Концетка подменяет story._chat_completion мгновенным AsyncMock; здесь
# тестируем РЕАЛЬНУЮ функцию, поэтому держим ссылку, взятую при импорте.
_REAL_CHAT = story._chat_completion

_POLLINATIONS = "https://text.pollinations.ai/openai"


async def _no_op_sleep(_s):
    return None


@pytest.fixture(autouse=True)
def _single_provider(monkeypatch) -> None:
    """Один провайдер (Pollinations без токена) с фиксированным URL и моделью."""
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "pollinations_token", "")
    monkeypatch.setattr(settings, "story_models", "m1")
    monkeypatch.setattr(story.asyncio, "sleep", _no_op_sleep)
    story._PROVIDER_BREAKERS.clear()
    yield
    story._PROVIDER_BREAKERS.clear()


class _FakeClient:
    """httpx.AsyncClient, который отвечает заранее заданной функцией."""

    def __init__(self, response_fn):
        self._response_fn = response_fn
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        self.calls += 1
        result = self._response_fn()
        if isinstance(result, BaseException):
            raise result
        return result


def _ok_response() -> httpx.Response:
    request = httpx.Request("POST", _POLLINATIONS)
    return httpx.Response(200, json={"choices": [{"message": {"content": "ок"}}]}, request=request)


async def test_breaker_opens_after_consecutive_failures() -> None:
    for _ in range(story._PROVIDER_OPEN_AFTER):
        story._breaker_note(_POLLINATIONS, False)
    assert story._breaker_status(_POLLINATIONS)


async def test_breaker_stays_closed_after_single_failure() -> None:
    story._breaker_note(_POLLINATIONS, False)
    assert not story._breaker_status(_POLLINATIONS)


async def test_breaker_resets_on_success() -> None:
    for _ in range(story._PROVIDER_OPEN_AFTER):
        story._breaker_note(_POLLINATIONS, False)
    assert story._breaker_status(_POLLINATIONS)
    story._breaker_note(_POLLINATIONS, True)
    assert not story._breaker_status(_POLLINATIONS)
    assert story._PROVIDER_BREAKERS[_POLLINATIONS]["fails"] == 0


async def test_chat_skips_open_provider_without_network(monkeypatch) -> None:
    for _ in range(story._PROVIDER_OPEN_AFTER):
        story._breaker_note(_POLLINATIONS, False)
    client = _FakeClient(lambda: pytest.fail("сетевой вызов при открытом выключателе"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)

    result = await _REAL_CHAT([{"role": "user", "content": "hi"}])
    assert result is None
    assert client.calls == 0


async def test_chat_closes_breaker_on_success(monkeypatch) -> None:
    client = _FakeClient(_ok_response)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)

    result = await _REAL_CHAT([{"role": "user", "content": "hi"}])
    assert result is not None and result[1] == "m1"
    assert story._PROVIDER_BREAKERS[_POLLINATIONS]["fails"] == 0
    assert not story._breaker_status(_POLLINATIONS)


async def test_chat_returns_none_when_all_providers_fail(monkeypatch) -> None:
    client = _FakeClient(lambda: httpx.ConnectError("net down"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)

    result = await _REAL_CHAT([{"role": "user", "content": "hi"}])
    assert result is None
    assert client.calls >= 1
    # Открылся ли выключатель после серии провалов — зависит от попыток,
    # а вот счётчик сбоев обязан расти.
    assert story._PROVIDER_BREAKERS[_POLLINATIONS]["fails"] >= 1