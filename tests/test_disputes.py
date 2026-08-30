"""Тесты каркаса разрешения споров: подача, резолюция и компенсация-выплата."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import disputes as mod
from app.config import settings
from app.models import Dispute, Player, Payout
from app.ton_utils import to_nano


async def test_open_dispute_records_complaint(session: AsyncSession) -> None:
    res = await mod.open_dispute(session, round_id=5, player_id=7, reason="приз не пришёл")
    assert "открыт" in res
    d = (await session.execute(select(Dispute))).scalars().one()
    assert d.round_id == 5 and d.player_id == 7
    assert d.reason == "приз не пришёл"
    assert d.status == "open"


async def test_open_dispute_requires_player_and_reason(session: AsyncSession) -> None:
    assert "игрока" in await mod.open_dispute(session, 5, None, "x")
    assert "причину" in await mod.open_dispute(session, 5, 7, "  ")


async def test_resolve_and_reject_set_status(session: AsyncSession) -> None:
    await mod.open_dispute(session, 5, 7, "проблема")
    d = (await session.execute(select(Dispute))).scalars().one()
    assert "resolved" in await mod.resolve_dispute(session, d.id, "выплата отправлена повторно")
    d2 = await session.get(Dispute, d.id)
    assert d2.status == "resolved" and d2.resolved_at is not None
    assert d2.keeper_note == "выплата отправлена повторно"
    # Повторная резолюция отклоняется.
    assert "уже закрыт" in await mod.resolve_dispute(session, d.id, "x")


async def test_reject_dispute(session: AsyncSession) -> None:
    await mod.open_dispute(session, 5, 7, "не согласен")
    d = (await session.execute(select(Dispute))).scalars().one()
    assert "rejected" in await mod.reject_dispute(session, d.id, "без оснований")
    d2 = await session.get(Dispute, d.id)
    assert d2.status == "rejected"


async def test_compensate_creates_payout_for_wallet_player(session: AsyncSession) -> None:
    session.add(Player(id=7, wallet_address="EQw"))
    await session.commit()
    await mod.open_dispute(session, 5, 7, "недоплата")
    d = (await session.execute(select(Dispute))).scalars().one()
    res = await mod.compensate_dispute(session, d.id, "0.5", "возврат части приза")
    assert "компенсация" in res and "0.5" in res
    pay = (await session.execute(select(Payout))).scalars().one()
    assert pay.kind == "dispute"
    assert pay.amount_nanotons == to_nano(0.5)
    assert pay.player_id == 7
    assert pay.dest_address == "EQw"
    # Компенсация закрывает открытый спор.
    d2 = await session.get(Dispute, d.id)
    assert d2.status == "resolved"


async def test_compensate_refuses_walletless_and_bad_amount(session: AsyncSession) -> None:
    session.add(Player(id=8))
    await session.commit()
    await mod.open_dispute(session, 5, 8, "x")
    d = (await session.execute(select(Dispute))).scalars().one()
    assert "кошелёк" in await mod.compensate_dispute(session, d.id, "0.5")
    # Сумма проверяется даже при наличии кошелька.
    session.add(Player(id=9, wallet_address="EQv"))
    await session.commit()
    await mod.open_dispute(session, 5, 9, "y")
    rows = list((await session.execute(select(Dispute))).scalars().all())
    d2 = next(r for r in rows if r.player_id == 9)
    assert "числом" in await mod.compensate_dispute(session, d2.id, "abc")


async def test_disputes_flag_gates_open(session: AsyncSession, monkeypatch) -> None:
    monkeypatch.setattr(settings, "disputes_enabled", False)
    assert "выключено" in await mod.open_dispute(session, 5, 7, "x")
