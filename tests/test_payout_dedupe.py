"""Анти-дубль выплат: сверка memo с историей казначея перед повторной отправкой.

Краш между вещанием перевода и коммитом «sent» оставляет строку в
sending → ретрай. Без сверки это второй реальный перевод. Сверка по memo
(уникальному way:<день>:<тип>#<id>) находит уже ушедший платёж и помечает
выплату sent без новой отправки.
"""

import base64
import os
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
