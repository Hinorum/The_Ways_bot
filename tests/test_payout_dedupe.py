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
