"""Тесты недельного лидерборда: ISO-недели, доли мест, выплата топ-3."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal
from app.leaderboard import (
    WEEKLY_MARKER_KEY,
    _week_prize_amounts,
    settle_week_if_due,
    weekly_top,
)
from app.models import Payout, Player, Round, RoundStatus, Vote, WatcherState, WeeklyPot, WinRule
from app.ton_utils import to_nano
from app.weeks import iso_week_key, parse_prize_pcts, previous_week_key, week_bounds


def test_iso_week_key_and_bounds() -> None:
    moment = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)  # понедельник
    assert iso_week_key(moment) == "2026-W35"
    assert iso_week_key(moment.replace(tzinfo=None)) == "2026-W35"
    start, end = week_bounds("2026-W35")
    assert start == datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)


def test_previous_week_key_across_year_boundary() -> None:
    assert previous_week_key(datetime(2026, 8, 26, tzinfo=timezone.utc)) == "2026-W34"
    assert previous_week_key(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "2025-W52"


def test_parse_prize_pcts_filters_garbage_and_caps_at_three() -> None:
    assert parse_prize_pcts("20,30,50") == [20, 30, 50]
    assert parse_prize_pcts(" 10 , x , -5 , 40 ") == [10, 40]
    assert parse_prize_pcts("10,20,30,40") == [10, 20, 30]
    assert parse_prize_pcts("") == []


def test_week_prize_amounts_dust_and_rollover() -> None:
    amounts, rolled = _week_prize_amounts(to_nano(10), 3)
    assert amounts == [
        to_nano(10) * 20 // 100,
        to_nano(10) * 30 // 100,
        to_nano(10) * 50 // 100,
    ]
    assert rolled == 0
    # Достойных только двое: их места платим, незаполненное третье — в перенос.
    amounts, rolled = _week_prize_amounts(to_nano(10), 2)
    assert amounts[:1] == [to_nano(10) * 20 // 100]
    assert sum(amounts) + rolled == to_nano(10)
    # Пыль не теряется и не уезжает в перенос.
    amounts, rolled = _week_prize_amounts(999, 3)
    assert sum(amounts) + rolled == 999


async def _seed_closed_round(session: AsyncSession, day_index: int, opens_at: datetime, winner_card: int = 0) -> Round:
    round_row = Round(
        day_index=day_index,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        rule_commitment="c",
        chapter_title="t",
        chapter_text="text",
        lore_summary="lore",
        opens_at=opens_at,
        voting_ends_at=opens_at + timedelta(hours=23),
        tally_ends_at=opens_at + timedelta(hours=24),
        winner_card=winner_card,
        vote_counts_json="{}",
        payouts_finalized=True,
    )
    session.add(round_row)
    await session.flush()
    return round_row


async def test_settle_week_pays_top3_by_places(monkeypatch: pytest.MonkeyPatch) -> None:
    """Топ-3 недели получают 20%/30%/50%; без кошелька или дней — мимо, доля переносится."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 4)
    monkeypatch.setattr(settings, "weekly_prize_pcts", "20,30,50")
    base = 850_000
    pid_best, pid_second, pid_nowallet, pid_lazy = (base + i for i in range(4))
    wallets = {
        pid_best: "0:" + os.urandom(32).hex(),
        pid_second: "0:" + os.urandom(32).hex(),
        pid_lazy: "0:" + os.urandom(32).hex(),
    }
    prev_start, prev_end = week_bounds(previous_week_key())

    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pid_best, username="best", wallet_address=wallets[pid_best]),
                Player(id=pid_second, username="second", wallet_address=wallets[pid_second]),
                Player(id=pid_nowallet, username="nowallet"),  # лидер без кошелька — пропуск
                Player(id=pid_lazy, username="lazy", wallet_address=wallets[pid_lazy]),  # 2 дня — мало
            ]
        )
        rounds: list[Round] = []
        # best: 5 верных за неделю; second: 4 верных; lazy: 2 верных; nowallet: 7 верных.
        plan = {pid_best: 5, pid_second: 4, pid_nowallet: 7, pid_lazy: 2}
        day = 800_000
        for offset in range(7):
            round_row = await _seed_closed_round(
                session, day + offset, prev_start + timedelta(days=offset, hours=11)
            )
            rounds.append(round_row)
            for pid, count in plan.items():
                if offset < count:
                    session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
        pot_total = to_nano(10)
        week_key = previous_week_key()
        session.add(WeeklyPot(week=week_key, nanotons=pot_total))
        await session.commit()
        try:
            assert await settle_week_if_due(bot=None) is True
            rows = (
                (
                    await session.execute(
                        select(Payout).where(Payout.kind == "weekly").order_by(Payout.player_id.asc())
                    )
                )
                .scalars()
                .all()
            )
            # Места только у достойных: best (место 1, 20%) и second (место 2, 30%).
            assert [(p.player_id, p.amount_nanotons) for p in rows] == [
                (pid_best, pot_total * 20 // 100),
                (pid_second, pot_total * 30 // 100),
            ]
            assert all(p.dest_address == wallets[p.player_id] for p in rows)
            # Незаполненное третье место (50%) переносится в копилку текущей недели.
            current_pot = (
                await session.execute(select(WeeklyPot).where(WeeklyPot.week != week_key))
            ).scalar_one()
            assert current_pot.nanotons == pot_total - pot_total * 20 // 100 - pot_total * 30 // 100
            # Выплаченная неделя закрыта, метка переведена.
            assert (await session.scalar(select(WeeklyPot.nanotons).where(WeeklyPot.week == week_key))) is None
            marker = await session.get(WatcherState, WEEKLY_MARKER_KEY)
            assert marker is not None and marker.value == previous_week_key()
            # Идемпотентность.
            assert await settle_week_if_due(bot=None) is False
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == WEEKLY_MARKER_KEY))
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (pid_best, pid_second, pid_nowallet, pid_lazy):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_settle_week_waits_without_eligible_players(monkeypatch: pytest.MonkeyPatch) -> None:
    """Достойных нет — метка не двигается, горш ждёт следующего цикла."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 4)
    base = 860_000
    pid = base
    prev_start, _prev_end = week_bounds(previous_week_key())
    async with SessionLocal() as session:
        session.add(Player(id=pid, username="lonely"))  # без кошелька
        round_row = await _seed_closed_round(session, 810_001, prev_start + timedelta(hours=11))
        session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
        week_key = previous_week_key()
        session.add(WeeklyPot(week=week_key, nanotons=to_nano(3)))
        await session.commit()
        try:
            assert await settle_week_if_due(bot=None) is False
            pot_left = await session.scalar(select(WeeklyPot.nanotons).where(WeeklyPot.week == week_key))
            assert pot_left == to_nano(3)
            assert await session.get(WatcherState, WEEKLY_MARKER_KEY) is None
        finally:
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == WEEKLY_MARKER_KEY))
            await session.execute(WeeklyPot.__table__.delete())
            await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
            await session.delete(round_row)
            player = await session.get(Player, pid)
            if player is not None:
                await session.delete(player)
            await session.commit()


async def test_settle_week_postponed_until_last_day_finalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Последний день недели ещё не финализирован — выплату откладываем целиком."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    base = 870_000
    async with SessionLocal() as session:
        prev_start, prev_end = week_bounds(previous_week_key())
        # Незакрытый день в прошлой неделе (последний день закрывается уже в понедельник).
        unfinished = Round(
            day_index=820_001,
            status=RoundStatus.TALLYING,
            win_rule=WinRule.MAJORITY,
            rule_commitment="c",
            chapter_title="t",
            chapter_text="text",
            lore_summary="lore",
            opens_at=prev_end - timedelta(hours=13),
            voting_ends_at=prev_end + timedelta(hours=10),
            tally_ends_at=prev_end + timedelta(hours=11),
            winner_card=None,
            vote_counts_json="{}",
        )
        session.add(unfinished)
        session.add(Player(id=base, username="u", wallet_address="0:" + os.urandom(32).hex()))
        done = await _seed_closed_round(session, 820_002, prev_start + timedelta(hours=11))
        session.add(Vote(round_id=done.id, player_id=base, card_position=0))
        session.add(WeeklyPot(week=previous_week_key(), nanotons=to_nano(1)))
        await session.commit()
        try:
            assert await settle_week_if_due(bot=None) is False
            assert await session.get(WatcherState, WEEKLY_MARKER_KEY) is None
        finally:
            await session.execute(WeeklyPot.__table__.delete())
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == WEEKLY_MARKER_KEY))
            await session.delete(unfinished)
            await session.execute(Vote.__table__.delete().where(Vote.round_id == done.id))
            await session.delete(done)
            player = await session.get(Player, base)
            if player is not None:
                await session.delete(player)
            await session.commit()


async def test_weekly_top_orders_by_correct_then_days(session: AsyncSession) -> None:
    """Ничья по верным путям решается в пользу более постоянного игрока."""
    now = datetime.now(timezone.utc)
    steady, lucky = 900_001, 900_002
    session.add_all([Player(id=steady, username="steady"), Player(id=lucky, username="lucky")])
    # steady: 3 дня участия, из них 2 верных (в последний день промахнулся);
    # lucky: ровно те же 2 верных, но всего за 2 дня.
    rounds = []
    for offset in range(3):
        round_row = await _seed_closed_round(session, 830_000 + offset, now - timedelta(days=offset + 1))
        rounds.append(round_row)
        session.add(Vote(round_id=round_row.id, player_id=steady, card_position=0 if offset < 2 else 1))
        if offset < 2:
            session.add(Vote(round_id=round_row.id, player_id=lucky, card_position=0))
    await session.commit()

    rows = await weekly_top(session, now - timedelta(days=10), now + timedelta(minutes=1))
    assert rows[0] == (steady, 2, 3)
    assert rows[1] == (lucky, 2, 2)

    await session.execute(Vote.__table__.delete())
    for round_row in rounds:
        await session.delete(round_row)

