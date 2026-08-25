"""Анти-дубль выплат: сверка memo с историей казначея перед повторной отправкой.

Краш между вещанием перевода и коммитом «sent» оставляет строку в
sending → ретрай. Без сверки это второй реальный перевод. Сверка по memo
(уникальному way:<день>:<тип>#<id>) находит уже ушедший платёж и помечает
выплату sent без новой отправки.
"""

import asyncio as _asyncio
import base64
import os
import time as _t
from unittest.mock import AsyncMock

from app import ton_pay
from app.config import settings
from app.db import SessionLocal
from app.models import Payout


def _comment(payout_id: int) -> str:
    return f"way:7:prize#{payout_id}"


async def _seed_payout(attempts: int) -> int:
    async with SessionLocal() as session:
        payout = Payout(
            round_id=7,
            player_id=42,
            kind="prize",
            amount_nanotons=500_000_000,
            dest_address="0:" + os.urandom(32).hex(),
        )
        session.add(payout)
        await session.flush()
        payout.attempts = attempts
        payout.status = "pending"
        await session.commit()
        return payout.id


def test_out_comments_extractors_cover_both_providers() -> None:
    tonapi_item = {
        "out_msgs": [
            {"raw_message": "way:1:rake#3"},
            {"msg_data": {"decoded_comment": "way:7:prize#9"}},
            {"msg_data": {"text": base64.b64encode("привет".encode()).decode()}},
            {"msg_data": {}},
            "мусор",
        ]
    }
    comments = ton_pay._out_comments_tonapi(tonapi_item)
    assert comments == ["way:1:rake#3", "way:7:prize#9", "привет"]

    toncenter_item = {
        "out_msgs": [
            {"message_content": {"decoded": {"@type": "comment", "comment": "way:8:refund#11"}}},
            {"message_content": {}},
        ]
    }
    assert ton_pay._out_comments_toncenter(toncenter_item) == ["way:8:refund#11"]


async def test_dispatch_marks_sent_when_memo_already_broadcast(monkeypatch) -> None:
    """Перевод уже ушёл в цепочку в прошлый раз — ретрай НЕ задваивает платёж."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    payout_id = await _seed_payout(attempts=1)

    async def fake_markers() -> set[str]:
        return {_comment(payout_id)}

    transfer = AsyncMock(return_value=None)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", fake_markers)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)

    try:
        sent = await ton_pay.dispatch_pending_payouts(bot=None)
        assert sent == 1
        assert transfer.await_count == 0  # вещания не было
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent"
        assert row.sent_at is not None
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_dispatch_still_sends_when_marker_absent(monkeypatch) -> None:
    """Сверка не нашла memo (сеть молчала в прошлый раз) — обычная отправка."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    payout_id = await _seed_payout(attempts=1)

    async def empty_markers() -> set[str]:
        return set()

    transfer = AsyncMock(return_value="bcast:123")
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", empty_markers)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)

    try:
        await ton_pay.dispatch_pending_payouts(bot=None)
        assert transfer.await_count == 1
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent" and row.tx_hash == "bcast:123"
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_first_attempt_skips_history_check(monkeypatch) -> None:
    """Свежая выплата (attempts==0) не могла вещаться раньше — история не читается."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    payout_id = await _seed_payout(attempts=0)

    async def explode() -> set[str]:
        raise AssertionError("для первого attempts история казначея не нужна")

    transfer = AsyncMock(return_value="bcast:77")
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", explode)
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)

    try:
        await ton_pay.dispatch_pending_payouts(bot=None)
        assert transfer.await_count == 1
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_failed_transfer_records_reason(monkeypatch) -> None:
    """Причина неудачи пишется в last_error и переживает исчерпание ретраев."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    # Guard пропускает дальше только при непустой мнемонике активной сети.
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(["слово"] * 24))
    payout_id = await _seed_payout(attempts=settings.payout_max_attempts - 1)

    # Сломанная пара мнемоника/адрес — типичная тестнет-причина.
    async def broken_wallet():
        raise ValueError("Адрес казначея не совпадает с производным от мнемоники")

    async def empty_markers() -> set[str]:
        return set()

    monkeypatch.setattr(ton_pay, "_get_wallet", broken_wallet)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", empty_markers)

    try:
        await ton_pay.dispatch_pending_payouts(bot=None)
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "failed"
        assert row.last_error is not None
        assert "не совпадает" in row.last_error

        # Ручной retry снова поднимет строку с той же видимой причиной.
        row.status = "pending"
        row.attempts = 0
        row.alerted = False
        await session.commit()
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_empty_treasury_dest_revives_from_owner_env(monkeypatch) -> None:
    """Рейк без адреса (OWNER_WALLET_ADDRESS задан позже) уходит сам, без retry."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    owner = "0:" + os.urandom(32).hex()
    monkeypatch.setattr(settings, "owner_wallet_address", owner)
    async with SessionLocal() as session:
        payout = Payout(
            round_id=7,
            player_id=None,
            kind="rake",
            amount_nanotons=50_000_000,
            dest_address="",  # создано до того, как адрес появился в окружении
        )
        session.add(payout)
        await session.flush()
        payout_id = payout.id
        await session.commit()

    transfer = AsyncMock(return_value="bcast:88")
    monkeypatch.setattr(ton_pay, "send_ton_transfer", transfer)

    try:
        await ton_pay.dispatch_pending_payouts(bot=None)
        assert transfer.await_count == 1
        assert transfer.await_args.args[0] == owner
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "sent" and row.last_error is None
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_empty_dest_without_owner_fails_with_clear_reason(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "owner_wallet_address", "")
    async with SessionLocal() as session:
        payout = Payout(
            round_id=7,
            player_id=None,
            kind="leaderboard",
            amount_nanotons=30_000_000,
            dest_address="",
        )
        session.add(payout)
        await session.flush()
        payout_id = payout.id
        await session.commit()

    try:
        await ton_pay.dispatch_pending_payouts(bot=None)
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "failed"
        assert "OWNER_WALLET_ADDRESS" in row.last_error
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_payouts_listing_shows_reason(monkeypatch) -> None:
    """/payouts показывает причину у каждой строки — разбор без логов."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.handlers import cmd_payouts

    monkeypatch.setattr(settings, "admin_ids", "4242")
    async with SessionLocal() as session:
        payout = Payout(
            round_id=9,
            kind="prize",
            amount_nanotons=2_000_000,
            dest_address="0:" + os.urandom(32).hex(),
            status="pending",
            attempts=2,
            last_error="Лайтсерверы не приняли перевод (результат 0)",
        )
        session.add(payout)
        await session.flush()
        payout_id = payout.id
        await session.commit()

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=4242),
        text="/payouts",
        answer=AsyncMock(),
    )
    try:
        await cmd_payouts(message)
        text = message.answer.call_args.args[0]
        assert f"#{payout_id}" in text
        assert "Лайтсерверы не приняли" in text
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_peer_failure_hint_points_to_liteserver_config(monkeypatch) -> None:
    """«have no alive peers» — причина-действие: конфиг лайтсерверов или локальный разгон."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(["слово"] * 24))
    payout_id = await _seed_payout(attempts=settings.payout_max_attempts - 1)

    async def no_peers():
        raise RuntimeError("LiteServerError: have no alive peers")

    async def empty_markers() -> set[str]:
        return set()

    monkeypatch.setattr(ton_pay, "_get_wallet", no_peers)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", empty_markers)

    try:
        await ton_pay.dispatch_pending_payouts(bot=None)
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "failed"
        assert "LITESERVER_CONFIG_URL" in row.last_error
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


async def test_send_timeout_is_retry_not_freeze(monkeypatch) -> None:
    """Зависший лайтсервер не морозит цикл: таймаут = ретрай с причиной."""

    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(["слово"] * 24))
    monkeypatch.setattr(settings, "payout_send_timeout_seconds", 1)
    payout_id = await _seed_payout(attempts=0)

    async def hang_forever(dest, amount, comment):
        await _asyncio.sleep(30)
        return None

    async def empty_markers() -> set[str]:
        return set()

    monkeypatch.setattr(ton_pay, "_get_wallet", AsyncMock(return_value=object()))
    monkeypatch.setattr(ton_pay, "send_ton_transfer", hang_forever)
    monkeypatch.setattr(ton_pay, "fetch_broadcast_markers", empty_markers)

    try:
        started = _time_now()
        await _asyncio.wait_for(
            ton_pay.dispatch_pending_payouts(bot=None), timeout=10
        )
        elapsed = _time_now() - started
        assert elapsed < 5, "цикл не должен ждать полный hang"
        async with SessionLocal() as session:
            row = await session.get(Payout, payout_id)
        assert row.status == "pending"  # attempts=1 < max → вернётся в очередь
        assert "таймаут вещания" in (row.last_error or "")
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()


def _time_now() -> float:

    return _t.monotonic()


def test_watcher_state_value_is_unlimited_text() -> None:
    """Регрессия: план Хозяина Ошибки рвал тик о VARCHAR(255)."""
    from sqlalchemy import Text as SAText

    from app.models import WatcherState

    column_type = WatcherState.__table__.c.value.type
    assert isinstance(column_type, SAText)
    assert column_type.length is None


def test_pollinations_429_breaker_blocks_until_cooldown(monkeypatch) -> None:
    """429 ставит паузу: следующие кадры не дёргают провайдер впустую."""
    from app import story

    monkeypatch.setattr(story, "_THROTTLED_UNTIL_MONOTONIC", 0.0)
    assert story._throttle_active() is False

    story._note_429(60)
    assert story._throttle_active() is True
    assert 0 < story._throttle_left_seconds() <= 60

    async def explode_client(*args, **kwargs):
        raise AssertionError("в период охлаждения сеть не должна дёргаться")

    monkeypatch.setattr(story.httpx, "AsyncClient", explode_client)
    from pathlib import Path as _Path

    result = _asyncio.run(
        story.fetch_free_image("prompt", _Path("unused.jpg"), seed=1, width=64, height=64)
    )
    assert result is False

    monkeypatch.setattr(story, "_THROTTLED_UNTIL_MONOTONIC", 0.0)
    assert story._throttle_active() is False


async def test_image_stub_roundtrip(session) -> None:
    """Заглушки фиксируются и вынимаются однократно (для отложенного апгрейда)."""
    from app.rounds import _pop_image_stubs, _record_image_stubs

    await _record_image_stubs(session, 8_800, cover_stub=True, card_positions=[1, 2])
    first = await _pop_image_stubs(session, 8_800)
    assert first == {"cover": True, "cards": [1, 2]}
    # Повторное чтение — пусто: задача апгрейда не задвоится.
    assert await _pop_image_stubs(session, 8_800) is None


async def test_wallet_uses_remote_liteserver_config(monkeypatch) -> None:
    """LITESERVER_CONFIG_URL доходит до конструктора провайдера."""
    import pytest
    from pytoniq_core.crypto.keys import mnemonic_new, mnemonic_to_private_key, private_key_to_public_key

    monkeypatch.setattr(ton_pay, "_provider", None)
    monkeypatch.setattr(ton_pay, "_wallet", None)
    monkeypatch.setattr(ton_pay, "_wallet_network", None)
    words = mnemonic_new(24)
    _, private_key = mnemonic_to_private_key(words)
    public_key = private_key_to_public_key(private_key)
    address = ton_pay._wallet_address("v4r2", public_key, -239)
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "mainnet")
    monkeypatch.setattr(settings, "treasury_mnemonic", " ".join(words))
    monkeypatch.setattr(settings, "treasury_address", address)
    monkeypatch.setattr(settings, "treasury_wallet_version", "auto")

    fetched: dict = {}

    async def fake_fetch(url: str) -> dict:
        fetched["url"] = url
        return {"liteservers": []}

    class Boom(Exception):
        pass

    def fake_from_config(config):
        fake_from_config.config = config
        raise Boom("доходим до построения провайдера")

    monkeypatch.setattr(ton_pay, "_fetch_remote_json", fake_fetch)
    monkeypatch.setattr("pytoniq.LiteBalancer.from_config", staticmethod(fake_from_config))
    monkeypatch.setattr(settings, "liteserver_config_url", "https://example.test/config.json")

    with pytest.raises(Boom):
        await ton_pay._get_wallet()
    assert fetched["url"] == "https://example.test/config.json"
    assert fake_from_config.config == {"liteservers": []}


async def test_dead_letter_alert_carries_reason(monkeypatch) -> None:
    """Алерт админу называет причину, а не только id."""
    monkeypatch.setattr(settings, "admin_ids", "4242")
    sent: list[tuple[int, str]] = []

    class Bot:
        async def send_message(self, chat_id, text):
            sent.append((chat_id, text))

    async with SessionLocal() as session:
        payout = Payout(
            round_id=8,
            kind="prize",
            amount_nanotons=1_000_000,
            dest_address="0:" + os.urandom(32).hex(),
            status="failed",
            alerted=False,
            attempts=5,
            last_error="Лайтсерверы не приняли перевод",
        )
        session.add(payout)
        await session.flush()
        payout_id = payout.id
        await session.commit()

    try:
        await ton_pay._alert_admin(Bot(), settings.ton_network)
        texts = [text for _chat, text in sent]
        assert any(str(payout_id) in t and "Лайтсерверы не приняли" in t for t in texts)
        # Дедуп: повторный вызов молчит.
        await ton_pay._alert_admin(Bot(), settings.ton_network)
        assert len(sent) == len(settings.admin_id_set)
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Payout, payout_id))
            await session.commit()
