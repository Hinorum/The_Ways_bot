"""Защита /refinalize от задвоения реальных выплат.

Перефинализация дня с уже ушедшими в блокчейн выплатами (status=sent)
пересоздала бы их ПОВТОРНО — игроку пришла бы вторая выплата той же суммы.
Повтор невозможен только пока ни одна строка раунда не ушла в сеть.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.handlers.admin import cmd_refinalize
from app.models import Payout, Round, RoundStatus, WinRule


def make_message(uid: int, day: int) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=uid),
        text=f"/refinalize {day}",
        bot=SimpleNamespace(),
        answer=AsyncMock(),
    )


async def _seed_closed_round(session, day_index: int, *, has_sent: bool) -> int:
    round_row = Round(
        day_index=day_index,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        rule_commitment="c",
        chapter_title="Эхо",
        chapter_text="т",

        opens_at=datetime.now(timezone.utc),
        voting_ends_at=datetime.now(timezone.utc),
        tally_ends_at=datetime.now(timezone.utc),
        payouts_finalized=True,
    )
    session.add(round_row)
    await session.flush()
    session.add(
        Payout(
            round_id=round_row.id,
            player_id=7,
            kind="prize",
            amount_nanotons=1_000_000_000,
            dest_address="0:" + "11" * 32,
            status="sent" if has_sent else "pending",
        )
    )
    await session.commit()
    return round_row.id


async def test_refinalize_refuses_when_anything_sent(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", "4242")
    async with SessionLocal() as session:
        round_id = await _seed_closed_round(session, 420, has_sent=True)

    msg = make_message(4242, 420)
    await cmd_refinalize(msg)

    body = msg.answer.call_args.args[0]
    assert "отменена" in body and "sent" in body
    async with SessionLocal() as session:
        row = await session.get(Round, round_id)
        payout_q = await session.execute(select(Payout).where(Payout.round_id == round_id))
        payouts = list(payout_q.scalars().all())
    # Флаг не сброшен, строка не пересоздана и не dismissed.
    assert row.payouts_finalized is True
    assert [p.status for p in payouts] == ["sent"]
    async with SessionLocal() as session:
        await session.delete(await session.get(Round, round_id))
        payout = (await session.execute(select(Payout).where(Payout.round_id == round_id))).scalar_one()
        await session.delete(payout)
        await session.commit()


async def test_refinalize_proceeds_when_nothing_sent(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", "4242")
    async with SessionLocal() as session:
        round_id = await _seed_closed_round(session, 421, has_sent=False)

    msg = make_message(4242, 421)
    await cmd_refinalize(msg)

    bodies = [call.args[0] for call in msg.answer.call_args_list]
    assert all("отменена" not in b for b in bodies)
    assert any("finalized сброшен" in b for b in bodies)
    async with SessionLocal() as session:
        row = await session.get(Round, round_id)
        payout_q = await session.execute(select(Payout).where(Payout.round_id == round_id))
        payouts = list(payout_q.scalars().all())
    # Не-sent строки dismissed, затем finalize_day_payouts снова забирает claim
    # (атомарный UPDATE ставит флаг True в начале повторной финализации).
    assert row.payouts_finalized is True
    assert [p.status for p in payouts] == ["dismissed"]
    async with SessionLocal() as session:
        await session.delete(await session.get(Round, round_id))
        payout = (await session.execute(select(Payout).where(Payout.round_id == round_id))).scalar_one()
        await session.delete(payout)
        await session.commit()