"""Тесты недельного лидерборда: ISO-недели, доли мест, выплата топ-3 игроков.

Ничья по верным путям решается большим вкладом Gram в неделю, затем — кто
раньше нажал Claim в /start; только потом меньшим player_id. Места — топ-3
ИГРОКА (не ступеней счёта) с долями 50/30/20.
"""

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
    WEEK_READY_KEY,
    _rank_window,
    _week_prize_amounts,
    settle_week_if_due,
)
from app.models import (
    LeaderboardClaim,
    Payout,
    Player,
    Round,
    RoundStatus,
    Stake,
    Vote,
    WatcherState,
    WeeklyPot,
    WinRule,
)
from app.ton_utils import to_nano
from app.weeks import iso_week_key, parse_prize_pcts, previous_week_key, week_bounds


async def _set_week_ready(session: AsyncSession, week_key: str) -> None:
    """Ставит флаг готовности недельного лидерборда (эпилог последнего дня недели записан)."""
    session.add(WatcherState(key=WEEK_READY_KEY, value=week_key))
    await session.commit()


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
        to_nano(10) * 50 // 100,
        to_nano(10) * 30 // 100,
        to_nano(10) * 20 // 100,
    ]
    assert rolled == 0
    # Достойных только двое: их места платим, незаполненное третье — в перенос.
    amounts, rolled = _week_prize_amounts(to_nano(10), 2)
    assert amounts == [to_nano(10) * 50 // 100, to_nano(10) * 30 // 100]
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


def _set_stake(session: AsyncSession, round_row: Round, pid: int, status: str = "confirmed") -> None:
    session.add(
        Stake(
            round_id=round_row.id,
            player_id=pid,
            amount_nanotons=to_nano(1),
            tx_hash="tx_" + os.urandom(16).hex(),
            status=status,
        )
    )


async def test_settle_week_pays_top3_by_places(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сильнейшие игроки получают 50%/30%; без кошелька или дней — мимо, доля переносится."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 4)
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
            # Ставили в течение недели только достойные (best и second) — один день.
            if offset == 0:
                for pid in (pid_best, pid_second):
                    _set_stake(session, round_row, pid)
        pot_total = to_nano(10)
        week_key = previous_week_key()
        session.add(WeeklyPot(week=week_key, nanotons=pot_total))
        await session.commit()
        try:
            await _set_week_ready(session, week_key)
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
            # Места только у достойных: best (место 1, 50%) и second (место 2, 30%).
            assert [(p.player_id, p.amount_nanotons) for p in rows] == [
                (pid_best, pot_total * 50 // 100),
                (pid_second, pot_total * 30 // 100),
            ]
            assert all(p.dest_address == wallets[p.player_id] for p in rows)
            # Незаполненное третье место (20%) переносится в копилку текущей недели.
            current_pot = (
                await session.execute(select(WeeklyPot).where(WeeklyPot.week != week_key))
            ).scalar_one()
            assert current_pot.nanotons == pot_total - pot_total * 50 // 100 - pot_total * 30 // 100
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
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (pid_best, pid_second, pid_nowallet, pid_lazy):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_settle_week_pays_top_three_individuals_not_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Места — топ-3 ИГРОКА по верным путям, а не ступени счёта: 7-7-5 платятся целиком.

    Двое с 7 верными и одинаковым вкладом Gram не делят ступень: меньший
    player_id забирает первое место (50%), второй — второе (30%), третьим идёт
    одиночка со счётом 5 (20%). Игрок со счётом 6, но без кошелька, не виден;
    счёт 3 — четвёртый, призов не имеет.
    """
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 4)
    base = 940_000
    pid_a7, pid_b7, pid_c5, pid_d3, pid_e6_lazy = (base + i for i in range(5))
    wallets = {pid: "0:" + os.urandom(32).hex() for pid in (pid_a7, pid_b7, pid_c5, pid_d3)}
    prev_start, _prev_end = week_bounds(previous_week_key())

    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pid_a7, username="alpha", wallet_address=wallets[pid_a7]),
                Player(id=pid_b7, username="beta", wallet_address=wallets[pid_b7]),
                Player(id=pid_c5, username="gamma", wallet_address=wallets[pid_c5]),
                Player(id=pid_d3, username="delta", wallet_address=wallets[pid_d3]),
                Player(id=pid_e6_lazy, username="lazy"),
            ]
        )
        rounds: list[Round] = []
        plan = {pid_a7: 7, pid_b7: 7, pid_c5: 5, pid_d3: 3, pid_e6_lazy: 6}
        day = 880_000
        for offset in range(7):
            round_row = await _seed_closed_round(
                session, day + offset, prev_start + timedelta(days=offset, hours=11)
            )
            rounds.append(round_row)
            for pid, count in plan.items():
                if offset < count:
                    session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
            # delta: 4 дня участия при трёх верных — четвёртый день промахом.
            if offset == 3:
                session.add(Vote(round_id=round_row.id, player_id=pid_d3, card_position=1))
            # Ставили в течение недели все с кошельком (кроме lazy).
            if offset == 0:
                for pid in (pid_a7, pid_b7, pid_c5, pid_d3):
                    _set_stake(session, round_row, pid)
        pot_total = to_nano(10)
        week_key = previous_week_key()
        session.add(WeeklyPot(week=week_key, nanotons=pot_total))
        await session.commit()
        try:
            await _set_week_ready(session, week_key)
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
            first = pot_total * 50 // 100
            second = pot_total * 30 // 100
            third = pot_total * 20 // 100
            amounts = {(p.player_id): p.amount_nanotons for p in rows}
            # Места — игроки: a7 и b7 равны (верность и Gram) — меньший id первым.
            assert amounts[pid_a7] == first
            assert amounts[pid_b7] == second
            assert amounts[pid_c5] == third
            assert pid_d3 not in amounts  # четвёртый по верным путям — без приза
            assert pid_e6_lazy not in amounts  # без кошелька и стажа — вне радара
            assert len(rows) == 3
            assert all(p.dest_address == wallets[p.player_id] for p in rows)
            # Копилка недели выплачена до нанотона.
            assert sum(amounts.values()) == pot_total
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == WEEKLY_MARKER_KEY))
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (pid_a7, pid_b7, pid_c5, pid_d3, pid_e6_lazy):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_settle_week_two_tied_roll_third_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """Двое абсолютно равных (верность и Gram): первое и второе места, третье — в перенос."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 4)
    base = 950_000
    pids = [base, base + 1]
    wallets = {pid: "0:" + os.urandom(32).hex() for pid in pids}
    prev_start, _prev_end = week_bounds(previous_week_key())

    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pids[0], username="one", wallet_address=wallets[pids[0]]),
                Player(id=pids[1], username="two", wallet_address=wallets[pids[1]]),
            ]
        )
        rounds: list[Round] = []
        day = 890_000
        for offset in range(4):
            round_row = await _seed_closed_round(
                session, day + offset, prev_start + timedelta(days=offset, hours=11)
            )
            rounds.append(round_row)
            for pid in pids:
                session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
            if offset == 0:
                for pid in pids:
                    _set_stake(session, round_row, pid)
        pot_total = to_nano(10)
        week_key = previous_week_key()
        session.add(WeeklyPot(week=week_key, nanotons=pot_total))
        await session.commit()
        try:
            await _set_week_ready(session, week_key)
            assert await settle_week_if_due(bot=None) is True
            rows = (
                (await session.execute(select(Payout).where(Payout.kind == "weekly")))
                .scalars()
                .all()
            )
            first = pot_total * 50 // 100
            second = pot_total * 30 // 100
            by_pid = {p.player_id: p.amount_nanotons for p in rows}
            assert by_pid[pids[0]] == first  # ничья по Gram — меньший player_id выше
            assert by_pid[pids[1]] == second
            current_pot = (
                await session.execute(select(WeeklyPot).where(WeeklyPot.week != week_key))
            ).scalar_one()
            assert current_pot.nanotons == pot_total - first - second  # 20% — в перенос
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == WEEKLY_MARKER_KEY))
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in pids:
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
            await _set_week_ready(session, week_key)
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


async def test_rank_window_orders_ties_by_gram_then_id(session: AsyncSession) -> None:
    """Равные по верным путям: больший вклад Gram выше; при равном Gram — меньший id.

    Дни участия больше не влияют на порядок — они лишь порог для стажа.
    """
    now = datetime.now(timezone.utc)
    steady, lucky = 900_001, 900_002
    session.add_all([Player(id=steady, username="steady"), Player(id=lucky, username="lucky")])
    # steady: 3 дня участия, из них 2 верных (в последний день промахнулся);
    # lucky: ровно те же 2 верных, но всего за 2 дня. Ставок нет — вклад Gram 0.
    rounds = []
    for offset in range(3):
        round_row = await _seed_closed_round(session, 830_000 + offset, now - timedelta(days=offset + 1))
        rounds.append(round_row)
        session.add(Vote(round_id=round_row.id, player_id=steady, card_position=0 if offset < 2 else 1))
        if offset < 2:
            session.add(Vote(round_id=round_row.id, player_id=lucky, card_position=0))
    await session.commit()

    rows = await _rank_window(session, now - timedelta(days=10), now + timedelta(minutes=1), by="opens_at")
    assert rows[0] == (steady, 2, 3, 0)
    assert rows[1] == (lucky, 2, 2, 0)

    await session.execute(Vote.__table__.delete())
    for round_row in rounds:
        await session.delete(round_row)


async def test_rank_window_gram_breaks_tie(session: AsyncSession) -> None:
    """Тот же счёт верных: игрок с бОльшим вкладом Gram в неделе стоит выше."""
    now = datetime.now(timezone.utc)
    light, heavy = 900_101, 900_102  # heavy выше по id, но побеждать должен по Gram
    session.add_all([Player(id=light, username="light"), Player(id=heavy, username="heavy")])
    round_row = await _seed_closed_round(session, 830_500, now - timedelta(hours=5))
    session.add(Vote(round_id=round_row.id, player_id=light, card_position=0))
    session.add(Vote(round_id=round_row.id, player_id=heavy, card_position=0))
    session.add(Stake(round_id=round_row.id, player_id=light, amount_nanotons=to_nano(1), tx_hash="tx_light", status="confirmed"))
    session.add(Stake(round_id=round_row.id, player_id=heavy, amount_nanotons=to_nano(3), tx_hash="tx_heavy", status="confirmed"))
    await session.commit()

    rows = await _rank_window(session, now - timedelta(days=1), now + timedelta(minutes=1), by="opens_at")
    assert rows[0] == (heavy, 1, 1, to_nano(3))
    assert rows[1] == (light, 1, 1, to_nano(1))

    await session.execute(Vote.__table__.delete())
    await session.execute(Stake.__table__.delete())
    await session.delete(round_row)


async def test_settle_week_excludes_player_without_stake(monkeypatch: pytest.MonkeyPatch) -> None:
    """Одинаково достойные, но не поставивший игрок не получает приз недели."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 1)
    base = 960_000
    pid_staked, pid_no_stake = base, base + 1
    wallets = {pid: "0:" + os.urandom(32).hex() for pid in (pid_staked, pid_no_stake)}
    prev_start, _ = week_bounds(previous_week_key())

    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pid_staked, username="staked", wallet_address=wallets[pid_staked]),
                Player(id=pid_no_stake, username="nostake", wallet_address=wallets[pid_no_stake]),
            ]
        )
        rounds: list[Round] = []
        day = 840_000
        for offset in range(3):
            round_row = await _seed_closed_round(
                session, day + offset, prev_start + timedelta(days=offset, hours=11)
            )
            rounds.append(round_row)
            for pid in (pid_staked, pid_no_stake):
                session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
            if offset == 0:
                _set_stake(session, round_row, pid_staked)
        session.add(WeeklyPot(week=previous_week_key(), nanotons=to_nano(10)))
        await session.commit()
        try:
            assert await settle_week_if_due(bot=None) is True
            paid = {
                p.player_id
                for p in (
                    await session.execute(select(Payout).where(Payout.kind == "weekly"))
                ).scalars()
            }
            assert pid_staked in paid
            assert pid_no_stake not in paid
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == WEEKLY_MARKER_KEY))
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (pid_staked, pid_no_stake):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_settle_week_refunded_stake_counts_rejected_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Возврат ставки (refunded) считается, нарушитель (rejected) — нет."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 1)
    base = 970_000
    pid_refunded, pid_rejected = base, base + 1
    wallets = {pid: "0:" + os.urandom(32).hex() for pid in (pid_refunded, pid_rejected)}
    prev_start, _ = week_bounds(previous_week_key())

    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pid_refunded, username="refunded", wallet_address=wallets[pid_refunded]),
                Player(id=pid_rejected, username="rejected", wallet_address=wallets[pid_rejected]),
            ]
        )
        rounds: list[Round] = []
        day = 845_000
        for offset in range(3):
            round_row = await _seed_closed_round(
                session, day + offset, prev_start + timedelta(days=offset, hours=11)
            )
            rounds.append(round_row)
            for pid in (pid_refunded, pid_rejected):
                session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
            if offset == 0:
                _set_stake(session, round_row, pid_refunded, status="refunded")
                _set_stake(session, round_row, pid_rejected, status="rejected")
        session.add(WeeklyPot(week=previous_week_key(), nanotons=to_nano(10)))
        await session.commit()
        try:
            assert await settle_week_if_due(bot=None) is True
            paid = {
                p.player_id
                for p in (
                    await session.execute(select(Payout).where(Payout.kind == "weekly"))
                ).scalars()
            }
            assert pid_refunded in paid
            assert pid_rejected not in paid
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == WEEKLY_MARKER_KEY))
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (pid_refunded, pid_rejected):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_rank_window_honors_correct_cap(monkeypatch) -> None:
    """Анти-гринд: верные пути сверх потолка не надувают счёт недели."""
    base = 970_200
    pid_a, pid_b = base, base + 1
    prev_start, _ = week_bounds(previous_week_key())

    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pid_a, username="grinder"),
                Player(id=pid_b, username="steady"),
            ]
        )
        rounds: list[Round] = []
        for offset in range(5):
            round_row = await _seed_closed_round(
                session, 846_000 + offset, prev_start + timedelta(days=offset, hours=11)
            )
            rounds.append(round_row)
            # grinder верно каждый день, steady — лишь дважды.
            session.add(Vote(round_id=round_row.id, player_id=pid_a, card_position=0))
            if offset < 2:
                session.add(Vote(round_id=round_row.id, player_id=pid_b, card_position=0))
        await session.commit()
        try:
            top = await _rank_window(session, prev_start, week_bounds(previous_week_key())[1], by="opens_at", limit=10)
            scored = {pid: correct for pid, correct, _days, _gram in top}
            assert scored[pid_a] == 5  # без потолка — все пять верных
            monkeypatch.setattr(settings, "leaderboard_correct_cap", 2)
            try:
                capped = await _rank_window(session, prev_start, week_bounds(previous_week_key())[1], by="opens_at", limit=10)
                assert {pid: c for pid, c, _d, _g in capped}[pid_a] == 2  # срезано до потолка
            finally:
                monkeypatch.undo()
        finally:
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (pid_a, pid_b):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_settle_week_claim_breaks_tie(monkeypatch: pytest.MonkeyPatch) -> None:
    """Равные по верным путям и вкладу Gram: кто раньше нажал Claim — выше."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 1)
    base = 980_000
    pid_early, pid_late = base, base + 1
    wallets = {pid: "0:" + os.urandom(32).hex() for pid in (pid_early, pid_late)}
    prev_start, _ = week_bounds(previous_week_key())
    week_key = previous_week_key()

    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pid_early, username="early", wallet_address=wallets[pid_early]),
                Player(id=pid_late, username="late", wallet_address=wallets[pid_late]),
            ]
        )
        rounds: list[Round] = []
        day = 847_000
        for offset in range(3):
            round_row = await _seed_closed_round(
                session, day + offset, prev_start + timedelta(days=offset, hours=11)
            )
            rounds.append(round_row)
            for pid in (pid_early, pid_late):
                session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
            if offset == 0:
                for pid in (pid_early, pid_late):
                    _set_stake(session, round_row, pid)
        session.add(
            LeaderboardClaim(
                player_id=pid_early, kind="week", period=week_key,
                claimed_at=prev_start + timedelta(days=1, hours=1),
            )
        )
        session.add(
            LeaderboardClaim(
                player_id=pid_late, kind="week", period=week_key,
                claimed_at=prev_start + timedelta(days=5, hours=1),
            )
        )
        session.add(WeeklyPot(week=week_key, nanotons=to_nano(10)))
        await session.commit()
        try:
            assert await settle_week_if_due(bot=None) is True
            by_pid = {
                p.player_id: p.amount_nanotons
                for p in (await session.execute(select(Payout).where(Payout.kind == "weekly"))).scalars()
            }
            # Ранний Claim перевешивает даже равенство по id-порядку не нарушая 50/30.
            assert by_pid == {
                pid_early: to_nano(10) * 50 // 100,
                pid_late: to_nano(10) * 30 // 100,
            }
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(LeaderboardClaim.__table__.delete())
            await session.execute(
                WatcherState.__table__.delete().where(WatcherState.key == WEEKLY_MARKER_KEY)
            )
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (pid_early, pid_late):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_settle_week_claimer_beats_silent_rival(monkeypatch: pytest.MonkeyPatch) -> None:
    """Заявивший Claim опережает равного, который кнопку не жал, даже при бОльшем id."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "weekly_min_days", 1)
    base = 981_000
    pid_quiet, pid_claimer = base, base + 1  # claimer выше по id
    wallets = {pid: "0:" + os.urandom(32).hex() for pid in (pid_quiet, pid_claimer)}
    prev_start, _ = week_bounds(previous_week_key())
    week_key = previous_week_key()

    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pid_quiet, username="quiet", wallet_address=wallets[pid_quiet]),
                Player(id=pid_claimer, username="claimer", wallet_address=wallets[pid_claimer]),
            ]
        )
        rounds: list[Round] = []
        day = 848_000
        for offset in range(2):
            round_row = await _seed_closed_round(
                session, day + offset, prev_start + timedelta(days=offset, hours=11)
            )
            rounds.append(round_row)
            for pid in (pid_quiet, pid_claimer):
                session.add(Vote(round_id=round_row.id, player_id=pid, card_position=0))
            if offset == 0:
                for pid in (pid_quiet, pid_claimer):
                    _set_stake(session, round_row, pid)
        session.add(
            LeaderboardClaim(
                player_id=pid_claimer, kind="week", period=week_key,
                claimed_at=prev_start + timedelta(days=1, hours=1),
            )
        )
        session.add(WeeklyPot(week=week_key, nanotons=to_nano(10)))
        await session.commit()
        try:
            assert await settle_week_if_due(bot=None) is True
            by_pid = {
                p.player_id: p.amount_nanotons
                for p in (await session.execute(select(Payout).where(Payout.kind == "weekly"))).scalars()
            }
            assert by_pid == {
                pid_claimer: to_nano(10) * 50 // 100,
                pid_quiet: to_nano(10) * 30 // 100,
            }
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "weekly"))
            await session.execute(LeaderboardClaim.__table__.delete())
            await session.execute(
                WatcherState.__table__.delete().where(WatcherState.key == WEEKLY_MARKER_KEY)
            )
            await session.execute(WeeklyPot.__table__.delete())
            for round_row in rounds:
                await session.execute(Vote.__table__.delete().where(Vote.round_id == round_row.id))
                await session.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
                await session.delete(round_row)
            for pid in (pid_quiet, pid_claimer):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


def test_weighted_amounts_distributes_full_pot(monkeypatch) -> None:
    """Сглаживание месячной копилки: веса нормируются по получателям."""
    from app.leaderboard import _weighted_amounts

    payable = [(1, 9, "w1"), (2, 7, "w2"), (3, 5, "w3")]
    weights = [60, 30, 10]
    amounts = _weighted_amounts(1000, payable, weights)
    assert sum(amounts) == 1000
    assert amounts == [600, 300, 100]

    # Весов меньше, чем получателей — последний вес повторяется, горш делится.
    amounts = _weighted_amounts(1000, payable[:2], [100])
    assert sum(amounts) == 1000
    assert amounts[0] == 500 and amounts[1] == 500

    assert _weighted_amounts(0, payable, weights) == []
    assert _weighted_amounts(1000, [], weights) == []


