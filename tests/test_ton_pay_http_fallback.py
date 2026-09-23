"""Тесты HTTP-канала отправки: оффлайн-подпись и вещание через Toncenter.

Fallback лайтсерверов (ADNL/TCP режется окружением, REST жив): казна
переводы не теряет — внешнее сообщение подписывается локально (seqno и статус
через REST), BoC уходит через Toncenter v2 jsonRPC sendBoc. Слой чисто
оффлайн: никаких живых лайтсерверов в тестах, HTTP-вызовы подменяются.
"""

from __future__ import annotations

import asyncio

import pytest
from pytoniq_core.crypto.keys import mnemonic_new, mnemonic_to_private_key, private_key_to_public_key

from app import ton_pay
from app.config import settings
from app.ton_utils import to_nano


class _Resp:
    def __init__(self, *, status_code: int = 200, body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")

    def json(self):
        if self._body is not None:
            return self._body
        raise ValueError("no json")


def _setup_testnet_treasury(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Нужная мнемоника + производный v5r1 адрес: пара валидна и детерминирована
    внутри теста (адрес считается с той же мнемоники)."""
    mnemonic = mnemonic_new(24)
    words = list(mnemonic)
    _, private_key = mnemonic_to_private_key(words)
    pub = private_key_to_public_key(private_key)
    address = ton_pay._wallet_address("v5r1", pub, -3)
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_mnemonic", " ".join(words))
    monkeypatch.setattr(settings, "treasury_testnet_address", address)
    monkeypatch.setattr(settings, "treasury_wallet_version", "v5r1")
    return words


def test_parse_run_method_seqno() -> None:
    """exit_code 0 → число стека (hex/десятичное/число); прочее → None."""
    assert ton_pay._parse_run_method_seqno({"exit_code": 0, "stack": [{"type": "num", "value": "0x1"}]}) == 1
    assert ton_pay._parse_run_method_seqno({"exit_code": 0, "stack": [{"type": "num", "value": "17"}]}) == 17
    assert ton_pay._parse_run_method_seqno({"exit_code": 0, "stack": [{"type": "num", "value": "0x0"}]}) == 0
    assert ton_pay._parse_run_method_seqno({"exit_code": 0, "stack": [{"type": "num", "value": 5}]}) == 5
    assert ton_pay._parse_run_method_seqno({"exit_code": 11, "stack": [{"type": "num", "value": "0x1"}]}) is None
    assert ton_pay._parse_run_method_seqno({"exit_code": 0, "stack": []}) is None
    assert ton_pay._parse_run_method_seqno({"exit_code": 0, "stack": [{"type": "num", "value": "garbage"}]}) is None


def test_is_liteserver_down_classification() -> None:
    assert ton_pay._is_liteserver_down(TimeoutError("adnl timeout"))
    assert ton_pay._is_liteserver_down(RuntimeError("LiteServerError: have no alive peers"))
    assert not ton_pay._is_liteserver_down(ValueError("Адрес казначея не совпадает"))
    assert not ton_pay._is_liteserver_down(RuntimeError("Лайтсерверы не приняли перевод (результат 0)"))
    assert not ton_pay._is_liteserver_down(asyncio.CancelledError())


async def test_build_offline_treasury_wallet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Оффлайн-кошелёк: пара мнемоника/адрес валидна, wallet_id тестнета v5."""
    _setup_testnet_treasury(monkeypatch)
    wallet, version = ton_pay._build_offline_treasury_wallet()
    assert version == "v5r1"
    assert wallet.address.to_str(False) == settings.active_treasury_address
    assert wallet.private_key is not None
    assert wallet.wallet_id == 2147483645  # 0x80000000 ^ (-3)


async def test_http_get_seqno_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """Активный казначей: seqno читается из toncenter runGetMethod."""
    _setup_testnet_treasury(monkeypatch)
    wallet, _ = ton_pay._build_offline_treasury_wallet()

    async def fake_account_state():
        return to_nano(1), "active", "tonapi"

    captured = {}

    async def fake_post(client, url, *, json=None, headers=None, timeout=None, max_retries=1,
                        retry_delay=1.0, backoff_factor=2.0, max_delay=30.0):
        captured["url"] = url
        captured["json"] = json
        assert headers == {"X-API-Key": settings.toncenter_api_key}
        return _Resp(body={"exit_code": 0, "stack": [{"type": "num", "value": "0x3"}]})

    monkeypatch.setattr(ton_pay, "fetch_account_state", fake_account_state)
    monkeypatch.setattr(ton_pay, "http_post_with_retry", fake_post)
    assert await ton_pay._http_get_wallet_seqno(wallet) == 3
    assert "runGetMethod" in captured["url"]
    assert captured["json"]["method"] == "seqno"


async def test_http_get_seqno_uninit_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Неразвёрнутая казна (uninit) → seqno 0: внешнее сообщение задеплоит."""
    _setup_testnet_treasury(monkeypatch)
    wallet, _ = ton_pay._build_offline_treasury_wallet()
    called = False

    async def fake_account_state():
        return to_nano(1), "uninit", "tonapi"

    async def fake_post(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("uninit не должен ходить за seqno")

    monkeypatch.setattr(ton_pay, "fetch_account_state", fake_account_state)
    monkeypatch.setattr(ton_pay, "http_post_with_retry", fake_post)
    assert await ton_pay._http_get_wallet_seqno(wallet) == 0
    assert not called


async def test_http_broadcast_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сборка BoC и jsonRPC sendBoc доходят до провайдера целыми."""
    _setup_testnet_treasury(monkeypatch)
    wallet, _ = ton_pay._build_offline_treasury_wallet()
    captured = {}

    async def fake_post(client, url, *, json=None, headers=None, timeout=None, **kw):
        captured["url"] = url
        captured["json"] = json
        return _Resp(body={"ok": True, "result": {"@type": "ok"}})

    monkeypatch.setattr(ton_pay, "http_post_with_retry", fake_post)
    body = wallet.create_wallet_internal_message(
        destination=wallet.address, value=to_nano(0.475)
    )
    await ton_pay._http_broadcast_external(wallet, seqno=1, internal_msg=body)
    assert "jsonRPC" in captured["url"]
    assert captured["json"]["method"] == "sendBoc"
    assert isinstance(captured["json"]["params"]["boc"], str) and captured["json"]["params"]["boc"]


async def test_http_broadcast_rejects_ok_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Провайдер ответил ok:false — исключение с текстом ошибки."""
    _setup_testnet_treasury(monkeypatch)
    wallet, _ = ton_pay._build_offline_treasury_wallet()

    async def fake_post(client, url, *, json=None, headers=None, timeout=None, **kw):
        return _Resp(body={"ok": False, "error": "message rejected by node"})

    monkeypatch.setattr(ton_pay, "http_post_with_retry", fake_post)
    body = wallet.create_wallet_internal_message(
        destination=wallet.address, value=to_nano(0.475)
    )
    with pytest.raises(RuntimeError, match="rejected by node"):
        await ton_pay._http_broadcast_external(wallet, seqno=1, internal_msg=body)


async def test_send_http_uses_local_seqno_when_batch_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """Батч: HTTP-путь берёт _batch_seqno локально и не ходит за seqno в сеть."""
    _setup_testnet_treasury(monkeypatch)
    wallet, _ = ton_pay._build_offline_treasury_wallet()
    seen = {"seqno": None}
    fetched_seqno = 0

    async def fake_get_seqno(w):
        nonlocal fetched_seqno
        fetched_seqno += 1
        return 100

    async def fake_broadcast(w, seqno, internal_msg):
        seen["seqno"] = seqno
        return None

    monkeypatch.setattr(ton_pay, "_http_get_wallet_seqno", fake_get_seqno)
    monkeypatch.setattr(ton_pay, "_http_broadcast_external", fake_broadcast)
    monkeypatch.setattr(ton_pay, "_batch_seqno", 40)
    try:
        marker = await ton_pay._send_ton_transfer_http("0:" + "11" * 32, to_nano(1), comment="way:9:prize#1")
    finally:
        monkeypatch.setattr(ton_pay, "_batch_seqno", None)
    assert marker and marker.startswith("bcast:")
    assert seen["seqno"] == 40
    assert fetched_seqno == 0  # локальный батч-счётчик не дублирует сетевой запрос


async def test_send_http_fetches_seqno_outside_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Вне батча seqno берётся из toncenter; после успеха счётчик наращивается."""
    _setup_testnet_treasury(monkeypatch)
    wallet, _ = ton_pay._build_offline_treasury_wallet()
    seen = {"seqno": None}

    async def fake_get_seqno(w):
        return 7

    async def fake_broadcast(w, seqno, internal_msg):
        seen["seqno"] = seqno
        return None

    monkeypatch.setattr(ton_pay, "_http_get_wallet_seqno", fake_get_seqno)
    monkeypatch.setattr(ton_pay, "_http_broadcast_external", fake_broadcast)
    monkeypatch.setattr(ton_pay, "_batch_seqno", None)
    try:
        marker = await ton_pay._send_ton_transfer_http("0:" + "22" * 32, to_nano(1), comment="way:9:prize#2")
        assert ton_pay._batch_seqno == 8
    finally:
        monkeypatch.setattr(ton_pay, "_batch_seqno", None)
    assert marker and marker.startswith("bcast:")
    assert seen["seqno"] == 7


async def test_send_ton_transfer_falls_back_on_no_peers(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_wallet падает «have no alive peers» → перевод уходит через HTTP."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(mnemonic_new(24)))
    called = {}

    async def no_peers():
        raise RuntimeError("LiteServerError: have no alive peers")

    async def http_send(dest, amount, comment):
        called["dest"] = dest
        called["amount"] = amount
        return f"bcast:{1234567890}"

    monkeypatch.setattr(ton_pay, "_get_wallet", no_peers)
    monkeypatch.setattr(ton_pay, "_send_ton_transfer_http", http_send)
    dest = "0:" + "33" * 32
    marker = await ton_pay.send_ton_transfer(dest, to_nano(0.42), comment="way:9:prize#3")
    assert marker and marker.startswith("bcast:")
    assert called == {"dest": dest, "amount": to_nano(0.42)}


async def test_send_ton_transfer_non_liteserver_error_not_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ошибка пары мнемоника/адрес — настоящая: HTTP-канал не цепляем."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(mnemonic_new(24)))
    called = False

    async def bad_pair():
        raise ValueError("Адрес казначея не совпадает с производным от мнемоники")

    async def http_send(dest, amount, comment):
        nonlocal called
        called = True
        return "bcast:1"

    monkeypatch.setattr(ton_pay, "_get_wallet", bad_pair)
    monkeypatch.setattr(ton_pay, "_send_ton_transfer_http", http_send)
    with pytest.raises(ValueError, match="не совпадает"):
        await ton_pay.send_ton_transfer("0:" + "44" * 32, to_nano(1), comment="x")
    assert not called