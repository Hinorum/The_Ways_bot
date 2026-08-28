"""Тесты претензий на места лидерборда: Claim в /start, клавиатура, ничьи.

Кнопка Claim жива только в течение периода (неделя/месяц), требует кошелёк
и хотя бы одну ставку. Запись идемпотентна (unique player+kind+period), а
при равенстве верных путей и вклада Gram решает момент претензии.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import func, select

from app.db import SessionLocal
from app.handlers import _start_keyboard, on_claim_month, on_claim_week
from app.leaderboard import _order_by_ties, active_claim_period
from app.models import LeaderboardClaim, Player, Round, RoundStatus, Stake, WinRule
from app.ton_utils import to_nano


def test_active_claim_period_keys_week_and_month() -> None:
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    assert active_claim_period("week", now) == "2026-W35"
    assert active_claim_period("month", now) == "2026-08"


def test_order_by_ties_claim_time_beats_id_and_silence() -> None:
    pts = "0:" + os.urandom(32).hex()
    candidates = [
        (1, 5, 100, pts),
        (2, 5, 100, pts),
        (3, 5, 100, pts),
        (4, 4, 100, pts),  # ниже по верным — позади любой пятёрки
    ]
    base = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    # Кто раньше нажал Claim — выше; молчаливый уступает обоим заявившимся.
    rows = _order_by_ties(candidates, {2: base + timedelta(hours=5), 1: base + timedelta(hours=1)})
    assert [r[0] for r in rows] == [1, 2, 3, 4]
    # Без претензий — меньший player_id.
    rows = _order_by_ties(candidates[:3], {})
    assert [r[0] for r in rows] == [1, 2, 3]
    # Заявившийся с большим id опережает равных молчунов.
    rows = _order_by_ties(candidates[:3], {3: base + timedelta(hours=2)})
    assert [r[0] for r in rows] == [3, 1, 2]


async def _mk_player(pid: int, wallet: str | None = None) -> None:
    async with SessionLocal() as db:
        player = Player(id=pid, username=f"u{pid}", first_name="P")
        if wallet is not None:
            player.wallet_address = wallet
        db.add(player)
        await db.commit()


async def _clean_player(pid: int) -> None:
    async with SessionLocal() as db:
        await db.execute(LeaderboardClaim.__table__.delete().where(LeaderboardClaim.player_id == pid))
        await db.execute(Player.__table__.delete().where(Player.id == pid))
        await db.commit()


def _callback(pid: int) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=pid, username=None, first_name="P"),
        message=SimpleNamespace(edit_reply_markup=AsyncMock()),
        answer=AsyncMock(),
    )


async def test_claim_week_inserts_one_row_and_is_idempotent() -> None:
    pid = 990_001
    await _mk_player(pid, wallet="0:" + os.urandom(32).hex())
    try:
        cb = _callback(pid)
        await on_claim_week(cb)
        await on_claim_week(cb)  # повторный тап — не вторая запись
        async with SessionLocal() as db:
            rows = (
                (await db.execute(select(LeaderboardClaim).where(LeaderboardClaim.player_id == pid)))
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].kind == "week"
        assert rows[0].period == active_claim_period("week")
        assert rows[0].claimed_at is not None
        texts = [c.args[0] for c in cb.answer.await_args_list]
        assert any("принята" in t for t in texts)
        assert any("уже заявлено" in t for t in texts)
    finally:
        await _clean_player(pid)


async def test_claim_month_and_week_are_separate() -> None:
    pid = 990_002
    await _mk_player(pid, wallet="0:" + os.urandom(32).hex())
    try:
        cb = _callback(pid)
        await on_claim_week(cb)
        await on_claim_month(cb)
        async with SessionLocal() as db:
            rows = (
                (await db.execute(select(LeaderboardClaim).where(LeaderboardClaim.player_id == pid)))
                .scalars()
                .all()
            )
        assert {r.kind for r in rows} == {"week", "month"}
    finally:
        await _clean_player(pid)


async def test_claim_requires_wallet() -> None:
    pid = 990_003
    await _mk_player(pid)  # кошелька нет
    try:
        cb = _callback(pid)
        await on_claim_week(cb)
        async with SessionLocal() as db:
            count = await db.scalar(
                select(func.count())
                .select_from(LeaderboardClaim)
                .where(LeaderboardClaim.player_id == pid)
            )
        assert count == 0
        alert = cb.answer.await_args.kwargs.get("show_alert")
        assert alert is True
        assert "кошелёк" in cb.answer.await_args.args[0]
    finally:
        await _clean_player(pid)


def _closed_round_this_week(day_index: int, now: datetime) -> Round:
    return Round(
        day_index=day_index,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        rule_commitment="c",
        chapter_title="t",
        chapter_text="text",
        lore_summary="lore",
        opens_at=now,
        voting_ends_at=now + timedelta(hours=23),
        tally_ends_at=now,
        winner_card=0,
        vote_counts_json="{}",
    )


async def test_start_keyboard_claim_buttons_follow_stakes() -> None:
    pid_staked, pid_silent = 990_011, 990_012
    await _mk_player(pid_staked, "0:" + os.urandom(32).hex())
    await _mk_player(pid_silent, "0:" + os.urandom(32).hex())
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        round_row = _closed_round_this_week(990_400, now)
        db.add(round_row)
        await db.flush()
        db.add(
            Stake(
                round_id=round_row.id,
                player_id=pid_staked,
                amount_nanotons=to_nano(1),
                tx_hash="tx_claim_test",
                status="confirmed",
            )
        )
        await db.commit()
        try:
            async with SessionLocal() as session:
                for pid in (pid_staked, pid_silent):
                    player = await session.get(Player, pid)
                    kb = await _start_keyboard(session, player)
                    labels = [b.text for row in kb.inline_keyboard for b in row]
                    if pid == pid_staked:
                        assert any("приз недели" in l for l in labels)
                        assert any("приз месяца" in l for l in labels)
                    else:
                        assert not any(l.startswith("🗓") for l in labels)
        finally:
            await db.execute(Stake.__table__.delete().where(Stake.round_id == round_row.id))
            await db.execute(Round.__table__.delete().where(Round.id == round_row.id))
            await db.commit()
    for pid in (pid_staked, pid_silent):
        await _clean_player(pid)