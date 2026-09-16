"""Глубокая сверка с историей казначея и аудит-отчёт блокчейн-контура.

Одна страница (128 tx) провайдера — слишком мелкое окно для анти-дубля:
memo «уже отправленного» в длинной очереди уходит за край, сверка решает
«перевода нет» и очередь плодит повторный перевод. Здесь проверяется
пагинация вглубь (TonAPI offset / Toncenter offset), стоп-условия, стопор
повтора при недоступной истории (защита от double-pay при 2ч requeue) и
/blockchain как точка входа в разбор «куда делось».
"""

import json
import os
import time
from unittest.mock import AsyncMock

from app import ton_pay
from app.config import settings
from app.core.registry import BEAT_KEY, CURSOR_KEY, SOURCE_KEY, STUCK_TX_KEY
from app.db import SessionLocal
from app.http_utils import http_get_with_retry
from app.models import Payout, WatcherState


# ---------- Пагинация истории казначея ----------


class _FakeResp:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """httpx.AsyncClient-замена: page выбирается по offset (0, 128, 256...)."""

    def __init__(self, pages: list[list[dict]], capture: list[dict]) -> None:
        self._pages = pages
        self._capture = capture

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def get(self, url: str, params: dict | None = None, headers: dict | None = None) -> _FakeResp:
        params = dict(params or {})
        self._capture.append(params)
        offset = int(params.get("offset", 0))
        index = offset // 128
        if index >= len(self._pages):
            return _FakeResp({"transactions": []})
        return _FakeResp({"transactions": self._pages[index]})


def _tonapi_item(lt: int, utime: int, memo: str) -> dict:
    return {
        "hash": f"h{lt}",
        "lt": lt,
        "utime": utime,
        "out_msgs": [{"msg_data": {"decoded_comment": memo}}],
    }


def _toncenter_item(lt: int, utime: int, memo: str) -> dict:
    return {
        "hash": f"h{lt}",
        "lt": lt,
        "utime": utime,
        "out_msgs": [{"message_content": {"decoded": {"@type": "comment", "comment": memo}}}],
    }


def _build_pages(now: int, item_builder) -> list[list[dict]]:
    # Три полные страницы по 128 записей, utime строго убывает.
    pages: list[list[dict]] = []
    for page in range(3):
        base = page * 128
        pages.append([item_builder(1_000_000 + base + i, now - base - i, f"way:1:pr#{base + i}") for i in range(128)])
    return pages


async def test_toncenter_reconcile_pagination_goes_deep(monkeypatch) -> None:
    """Сверка ищет memo НЕ только среди последних 128 tx: ходит страницами вглубь.

    Регрессия: прежний лимит 128 терял memo выпавших из окна старых выплат,
    и 2-часовой requeue переотправлял уже ушедший перевод (двойной платёж).
    Здесь memo из ТРЕТЬЕЙ страницы (дальше 256 tx) должен найтись.
    """
    now = int(time.time())
    monkeypatch.setattr(settings, "payout_reconcile_history_seconds", 200)
    monkeypatch.setattr(settings, "payout_reconcile_max_pages", 12)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + os.urandom(32).hex())
    pages = _build_pages(now, _toncenter_item)
    capture: list[dict] = []
    monkeypatch.setattr(ton_pay.httpx, "AsyncClient", lambda *a, **k: _FakeClient(pages, capture))

    tx_map = await ton_pay._tx_map_via_toncenter()
    offsets = [p["offset"] for p in capture]
    # Первая страница (0) → вторая (128); третья уже вне окна 200с — стоп.
    assert offsets == [0, 128]
    assert tx_map["way:1:pr#160"] == "h1000160"  # со второй страницы, за окном 128
    assert "way:1:pr#0" in tx_map  # первая страница тоже собрана
    assert len(tx_map) == 256


async def test_tonapi_reconcile_pagination_uses_offset(monkeypatch) -> None:
    """TonAPI тоже ходит вглубь через offset, а не только одной страницей."""
    now = int(time.time())
    monkeypatch.setattr(settings, "payout_reconcile_history_seconds", 200)
    monkeypatch.setattr(settings, "payout_reconcile_max_pages", 12)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + os.urandom(32).hex())
    pages = _build_pages(now, _tonapi_item)
    capture: list[dict] = []
    monkeypatch.setattr(ton_pay.httpx, "AsyncClient", lambda *a, **k: _FakeClient(pages, capture))

    tx_map = await ton_pay._tx_map_via_tonapi()
    offsets = [p["offset"] for p in capture]
    assert offsets == [0, 128]
    assert tx_map["way:1:pr#200"] == "h1000200"


async def test_toncenter_breaks_when_offset_not_honored(monkeypatch) -> None:
    """Провайдер проигнорировал offset (те же 128 поверх) — не проходимся по кругу."""
    now = int(time.time())
    monkeypatch.setattr(settings, "payout_reconcile_history_seconds", 3600 * 24)
    monkeypatch.setattr(settings, "payout_reconcile_max_pages", 12)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + os.urandom(32).hex())
    page = [_toncenter_item(1_000_000 + i, now - i, f"way:5:rake#{i}") for i in range(128)]
    capture: list[dict] = []
    # На любой offset отдаём одну и ту же страницу: первый хеш повторится.
    monkeypatch.setattr(ton_pay.httpx, "AsyncClient", lambda *a, **k: _FakeClient([page, page], capture))

    tx_map = await ton_pay._tx_map_via_toncenter()
    assert len(capture) == 2, "провайдер без offset должен обрываться после повтора страницы"
    assert len(tx_map) == 128


async def test_toncenter_stops_at_empty_history(monkeypatch) -> None:
    """Пустая страница завершает проход досрочно, без лишних запросов."""
    monkeypatch.setattr(settings, "payout_reconcile_history_seconds", 3600 * 24)
    monkeypatch.setattr(settings, "payout_reconcile_max_pages", 12)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + os.urandom(32).hex())
    capture: list[dict] = []
    monkeypatch.setattr(ton_pay.httpx, "AsyncClient", lambda *a, **k: _FakeClient([], capture))
    tx_map = await ton_pay._tx_map_via_toncenter()
    assert tx_map == {}
    assert len(capture) == 1


# ---------- Guard: повтор при недоступной истории ----------


async def _seed_payout(attempts: int) -> int:
    async with SessionLocal() as session:
        payout = Payout(
            round_id=7,
            player_id=42,
            kind="prize",
            amount_nanotons=500_000_000,
            dest_address="0:" + os.urandom(32).hex(),
            status="pending",
        )
        session.add(payout)
        await session.flush()
        payout.attempts = attempts
        await session.commit()
        return payout.id


def _patch_send_environment(monkeypatch, transfer: AsyncMock, markers) -> None:
    """Общая обстановка dispatch-тестов: TON включён, маркеры подменены."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", markers)


async def test_retry_held_when_history_unavailable(monkeypatch) -> None:
    """Повтор (>1 попытки) при «история молчит» НЕ переотправляется: риск double-pay.

    2-часовой requeue: перевод мог уйти в прошлый цикл, а сверка не видит memo,
    потому что оба провайдера недоступны. Пустой ответ маркеров тут — «не знаю»,
    а не «перевода нет». Замораживаем строку вместо реального двойного платежа."""
    payout_id = await _seed_payout(attempts=1)

    transfer = AsyncMock(return_value="bcast:123")

    async def empty_markers() -> set[str]:
        ton_pay._RECONCILE_HISTORY_OK = False  # оба провайдера реально упали
        return set()

    _patch_send_environment(monkeypatch, transfer, empty_markers)

    try:
        sent = await ton_pay.dispatch_pending_payouts(bot=None)
        assert sent == 0
        assert transfer.await_count == 0  # не вещали — денег не задвоили
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "pending"  # вернётся, когда история оживёт
        assert "анти-дубль" in (row.last_error or "")
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_retry_proceeds_when_history_known(monkeypatch) -> None:
    """История доступна, memo в ней нет — это честное «перевод не ушёл»: отправляем."""
    payout_id = await _seed_payout(attempts=1)

    transfer = AsyncMock(return_value="bcast:777")

    async def empty_markers() -> set[str]:
        return set()

    _patch_send_environment(monkeypatch, transfer, empty_markers)
    monkeypatch.setattr(ton_pay, "_RECONCILE_HISTORY_OK", True)

    try:
        sent = await ton_pay.dispatch_pending_payouts(bot=None)
        assert sent == 1
        assert transfer.await_count == 1
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent"
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_first_attempt_not_blocked_by_unknown_history(monkeypatch) -> None:
    """Первая попытка (ещё ничего не вещали) не боится «не знаю»: отправляется."""
    payout_id = await _seed_payout(attempts=0)

    transfer = AsyncMock(return_value="bcast:999")

    async def empty_markers() -> set[str]:
        return set()

    _patch_send_environment(monkeypatch, transfer, empty_markers)
    monkeypatch.setattr(ton_pay, "_RECONCILE_HISTORY_OK", False)

    try:
        sent = await ton_pay.dispatch_pending_payouts(bot=None)
        assert sent == 1
        assert transfer.await_count == 1
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent"
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


# ---------- Экспоненциальный backoff ----------


class _BoomClient:
    """Клиент, который сначала ловит транзиентные сбои, потом отвечает 200."""

    def __init__(self, failures: int) -> None:
        self._left = failures
        self.calls = 0

    async def get(self, url: str, params=None, headers=None):
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            raise httpx_transport_error()
        return _FakeResp({"ok": True})


def httpx_transport_error():
    import httpx

    return httpx.ConnectError("нет соединения")


async def test_http_retry_uses_exponential_backoff(monkeypatch) -> None:
    """Паузы между ретраями растут: 1с → 2с → 4с, а не одна и та же задержка."""
    sleeps: list[float] = []

    async def fake_sleep(secs: float) -> None:
        sleeps.append(secs)

    import app.http_utils as http_utils

    monkeypatch.setattr(http_utils.asyncio, "sleep", fake_sleep)
    client = _BoomClient(failures=3)

    response = await http_get_with_retry(
        client, "https://ton.example/v2/ping", max_retries=3,
        retry_delay=1.0, backoff_factor=2.0, max_delay=30.0,
    )
    assert response.json() == {"ok": True}
    assert client.calls == 4
    assert sleeps == [1.0, 2.0, 4.0]


async def test_http_retry_backoff_respects_cap(monkeypatch) -> None:
    """Задержка не растёт бесконечно: потолок max_delay обрезает экспоненту."""
    sleeps: list[float] = []

    async def fake_sleep(secs: float) -> None:
        sleeps.append(secs)

    import app.http_utils as http_utils

    monkeypatch.setattr(http_utils.asyncio, "sleep", fake_sleep)
    client = _BoomClient(failures=4)

    await http_get_with_retry(
        client, "https://ton.example/v2/ping", max_retries=4,
        retry_delay=10.0, backoff_factor=10.0, max_delay=25.0,
    )
    assert sleeps == [10.0, 25.0, 25.0, 25.0]


# ---------- /blockchain: аудит-отчёт ----------


async def test_blockchain_diagnostics_reports_pipeline(monkeypatch) -> None:
    """Отчёт виден целиком: курсор, stuck, очередь, сверка, баланс."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    now_iso = "2024-05-01T10:00:00+00:00"
    cursor = int(time.time()) - 300

    async def fake_state():
        return 5_000_000_000, "active", "tonapi"

    monkeypatch.setattr(ton_pay, "fetch_account_state", fake_state)
    monkeypatch.setattr(ton_pay, "_RECONCILE_HISTORY_OK", True)

    async with SessionLocal() as session:
        for key, value in (
            (BEAT_KEY, now_iso),
            (SOURCE_KEY, "tonapi"),
            (CURSOR_KEY, str(cursor)),
        ):
            session.add(WatcherState(key=key, value=value))
        session.add(WatcherState(key=STUCK_TX_KEY, value=json.dumps({"abc123": {"fails": 2}})))
        await session.commit()

    try:
        text = await ton_pay.blockchain_diagnostics()
        assert "Блокчейн-контур" in text
        assert "курсор" in text
        assert "Stuck-входящих: 1" in text
        assert "Очередь выплат" in text
        assert "Кошельков verified" in text
        assert "5.0000 Gram" in text
        assert "Сверка истории" in text
        assert "доступна" in text
    finally:
        async with SessionLocal() as session:
            for key in (BEAT_KEY, SOURCE_KEY, CURSOR_KEY, STUCK_TX_KEY):
                row = await session.get(WatcherState, key)
                if row is not None:
                    await session.delete(row)
            await session.commit()


async def test_blockchain_diagnostics_flags_history_down(monkeypatch) -> None:
    """История молчит → отчёт прямо говорит, что повторы выплат заморожены."""
    monkeypatch.setattr(settings, "ton_enabled", True)

    async def silent():
        return None, None, "none"

    monkeypatch.setattr(ton_pay, "fetch_account_state", silent)
    monkeypatch.setattr(ton_pay, "_RECONCILE_HISTORY_OK", False)

    text = await ton_pay.blockchain_diagnostics()
    assert "НЕДОСТУПНА" in text
    assert "повторы выплат заморожены" in text
    assert "Баланс казначея: недоступен" in text