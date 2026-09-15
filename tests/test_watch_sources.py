"""Тесты двух источников истории переводов: честный 404 TonAPI и фолбэк Toncenter v3.

Реальный инцидент: TonAPI-тестнет отдавал 404 истории транзакций активного
казначея, watcher считал это «пустой цепочкой», ставил сердцебиение — и ставки
тихо не находились при зелёном /health. Здесь проверяется, что такое состояние
теперь распознаётся, а переводы читаются через резервный источник.
"""

from __future__ import annotations

import base64
import os

import pytest

from app import ton_watch
from app.config import settings
from app.ton_utils import to_nano

TREASURY = "0:" + "ab" * 32
SENDER = "0:" + "cd" * 32

_HISTORY = f"/v2/accounts/{TREASURY}/transactions"
_ACCOUNT = f"/v2/accounts/{TREASURY}"
_V3 = "/api/v3/transactions"


class _Response:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


def install_http(monkeypatch: pytest.MonkeyPatch, routes: dict[str, list]) -> list[tuple]:
    """Скриптованный httpx.AsyncClient внутри ton_watch.

    routes: фрагмент URL -> очередь ответов (_Response или Exception);
    порядок ключей важен: совпадение ищется по первому подходящему фрагменту.
    Возвращает журнал вызовов [(url, params), ...].
    """
    calls: list[tuple] = []

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info) -> bool:
            return False

        async def get(self, url, params=None, headers=None):
            calls.append((url, dict(params or {})))
            for fragment, queue in routes.items():
                if fragment in url:
                    assert queue, f"заглушка исчерпана: {fragment}"
                    item = queue.pop(0)
                    if isinstance(item, Exception):
                        raise item
                    return item
            raise AssertionError(f"нет заглушки для {url}")

    monkeypatch.setattr(ton_watch.httpx, "AsyncClient", _Client)
    return calls


@pytest.fixture(autouse=True)
def _enabled_treasury(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_address", TREASURY)
    monkeypatch.setattr(settings, "treasury_testnet_address", TREASURY)


def _api_tx(utime: int, value_nano: int) -> dict:
    """Транзакция в формате TonAPI v2."""
    raw = os.urandom(32)
    return {
        "hash": base64.urlsafe_b64encode(raw).decode().rstrip("="),
        "utime": utime,
        "in_msg": {"source": {"address": SENDER}, "value": str(value_nano), "raw_message": ""},
    }


def _v3_tx(utime: int, value_nano: int, comment: str | None = None) -> dict:
    """Транзакция в формате Toncenter v3."""
    raw = os.urandom(32)
    decoded = (
        {"@type": "comment", "comment": comment}
        if comment is not None
        else {"@type": "empty_cell"}
    )
    return {
        "hash": base64.b64encode(raw).decode(),
        "lt": str(utime),
        "now": utime,
        "in_msg": {
            "source": SENDER,
            "value": str(value_nano),
            "message_content": {"decoded": decoded},
        },
    }


# ---------- Честная трактовка 404 ----------


async def test_uninitialized_treasury_404_is_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Свежий казначей без единой транзакции: 404 — здоровье, фолбэк не нужен."""
    calls = install_http(
        monkeypatch,
        {
            _HISTORY: [_Response(404)],
            _ACCOUNT: [_Response(200, {"status": "uninitialized"})],
            _V3: [AssertionError("фолбэк не должен вызываться")],
        },
    )
    transfers, ok = await ton_watch.fetch_recent_transfers(1000)
    assert ok is True and transfers == []
    assert any("/api/v3/" not in url for url, _params in calls)


async def test_active_account_without_recent_activity_404_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Активность кошелька старше курсора — история за окном и правда пуста."""
    install_http(
        monkeypatch,
        {
            _HISTORY: [_Response(404)],
            _ACCOUNT: [_Response(200, {"status": "active", "last_activity": 900})],
        },
    )
    transfers, ok = await ton_watch.fetch_recent_transfers(1000)
    assert ok is True and transfers == []


async def test_stale_index_404_is_degraded_and_toncenter_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Инцидент: 404 истории при живом кошельке с свежей активностью.

    Цикл не считается успешным по TonAPI, переводы приходят из Toncenter,
    источник успешного прохода помечается как «toncenter».
    """
    install_http(
        monkeypatch,
        {
            _HISTORY: [_Response(404)],
            _ACCOUNT: [_Response(200, {"status": "active", "last_activity": 1500})],
            _V3: [_Response(200, {"transactions": [_v3_tx(1500, to_nano(1.67))]})],
        },
    )
    transfers, ok, source = await ton_watch._collect_transfers(1000)
    assert ok is True and source == "toncenter"
    assert len(transfers) == 1
    assert transfers[0].value_nanotons == to_nano(1.67)
    assert transfers[0].utime == 1500
    assert transfers[0].comment == ""


async def test_tonapi_network_error_also_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """TonAPI лежит целиком (не только 404) — фолбэк спасает цикл."""
    install_http(
        monkeypatch,
        {
            _HISTORY: [RuntimeError("connect timeout")],
            _V3: [
                _Response(
                    200,
                    {"transactions": [_v3_tx(1600, to_nano(0.5), comment="привет")]},
                )
            ],
        },
    )
    transfers, ok, source = await ton_watch._collect_transfers(1000)
    assert ok is True and source == "toncenter"
    assert transfers[0].comment == "привет"


async def test_both_sources_down_means_failed_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Оба источника недоступны — цикл неуспешен, сердцебиение не встанет."""
    install_http(
        monkeypatch,
        {
            _HISTORY: [_Response(404)],
            _ACCOUNT: [RuntimeError("tonapi молчит")],
            _V3: [RuntimeError("toncenter молчит")],
        },
    )
    transfers, ok, source = await ton_watch._collect_transfers(1000)
    assert transfers == [] and ok is False and source == "none"


# ---------- Парсер Toncenter v3 ----------


def test_v3_comment_decoded_and_old_transactions_skipped() -> None:
    fresh = _v3_tx(1500, to_nano(0.42), comment="rv:7")
    transfer = ton_watch._parse_toncenter_item(fresh, since_utime=1000)
    assert transfer is not None
    assert transfer.comment == "rv:7"
    assert transfer.provider_ref == "1500"

    assert ton_watch._parse_toncenter_item(_v3_tx(500, to_nano(1)), since_utime=1000) is None
    empty = _v3_tx(1500, 0)
    assert ton_watch._parse_toncenter_item(empty, since_utime=1000) is None


def test_tx_hash_normalized_across_providers() -> None:
    """Один хеш в разных кодировках провайдеров — одна строка идемпотентности."""
    raw = bytes(range(32))
    urlsafe = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    standard = base64.b64encode(raw).decode()
    expected = raw.hex()
    assert ton_watch._norm_tx_hash(urlsafe) == expected
    assert ton_watch._norm_tx_hash(standard) == expected
    assert ton_watch._norm_tx_hash(expected.upper()) == expected
    # Мусор проходит насквозь, не падая.
    assert ton_watch._norm_tx_hash("c-0") == "c-0"


# ---------- Пагинация фолбэка ----------


async def test_toncenter_pagination_walks_by_lt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Полная страница уводит проход вглубь с курсором before_lt."""
    limit = ton_watch._PAGE_LIMIT
    page_one = [_v3_tx(2000 - index, to_nano(0.01)) for index in range(limit)]
    page_two = [_v3_tx(1500, to_nano(0.02)), _v3_tx(1400, to_nano(0.03))]
    calls = install_http(
        monkeypatch,
        {_V3: [_Response(200, {"transactions": page_one}), _Response(200, {"transactions": page_two})]},
    )

    transfers, complete = await ton_watch._deep_collect(
        ton_watch._toncenter_page,
        lambda page: page[-1].provider_ref or page[-1].tx_hash,
        1000,
    )

    assert complete is True
    assert len(transfers) == limit + 2
    v3_calls = [params for url, params in calls if _V3 in url]
    assert len(v3_calls) == 2
    assert v3_calls[0]["account"] == TREASURY
    assert "before_lt" not in v3_calls[0]
    # Курсор второй страницы — младший lt первой (страница отсортирована desc).
    assert v3_calls[1]["before_lt"] == page_one[-1]["lt"]
