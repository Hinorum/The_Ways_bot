"""Приоритет ставящих при выборе пути (win_rule_prefers_staked).

Лечит MINORITY-патологию: когда побеждает наименьший счёт, путь, за которого
НИКТО не держит подтверждённую ставку TON, не должен выигрывать, пока есть
путь с реальными деньгами. По умолчанию выключен — исход строго по закону.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Card, Player, Round, RoundStatus, Stake, Vote, WinRule
from app.rounds import finish_tally, _prefer_staked, _staked_paths
from app.ton_utils import to_nano


async def tally_round(
    session: AsyncSession,
    rule: WinRule,
    path_votes: dict[int, list[int]],
    stakers: list[int],
) -> Round:
    """День в подсчёте. path_votes[путь] = список игроков (один голос на игрока);
    stakers — игроки с подтверждённой ставкой (путь из их единственного голоса)."""
    now = datetime.now(timezone.utc)
    round_row = Round(
        day_index=1,
        status=RoundStatus.TALLYING,
        win_rule=rule,
        rule_commitment="c",
        chapter_title="t",
        chapter_text="text",
        lore_summary="lore",
        opens_at=now - timedelta(hours=25),
        voting_ends_at=now - timedelta(hours=1),
        tally_ends_at=now,
        vote_counts_json="{}",
    )
    # Карты цепляем к транзиентному объекту до add — как в рабочих тестах фильма.
    for pos in (0, 1, 2):
        round_row.cards.append(
            Card(position=pos, title=f"t{pos}", description="d", consequence="к", image_path="")
        )
    session.add(round_row)
    await session.commit()
    for path, player_ids in path_votes.items():
        for pid in player_ids:
            session.add(Player(id=pid, wallet_address=f"wallet-{pid}"))
            session.add(Vote(round_id=round_row.id, player_id=pid, card_position=path))
    for pid in stakers:
        session.add(
            Stake(
                round_id=round_row.id,
                player_id=pid,
                amount_nanotons=to_nano(0.1),
                tx_hash=f"tx-{pid}",
                status="confirmed",
            )
        )
    await session.commit()
    return round_row


async def test_prefer_staked_pure_minority() -> None:
    counts = {0: 0, 1: 1, 2: 2}  # MINORITY: сырой победитель — путь 0 (0 голосов)
    winner, tied = _prefer_staked(counts, WinRule.MINORITY, "s", staked={1})
    assert winner == 1 and tied == [1]


async def test_prefer_staked_pure_majority() -> None:
    counts = {0: 3, 1: 4, 2: 0}  # MAJORITY: сырой — путь 1; за него денег нет
    winner, _ = _prefer_staked(counts, WinRule.MAJORITY, "s", staked={0})
    assert winner == 0


async def test_prefer_staked_no_stakes_keeps_raw(session: AsyncSession) -> None:
    counts = {0: 0, 1: 1, 2: 2}
    winner, tied = _prefer_staked(counts, WinRule.MINORITY, "s", staked=set())
    assert winner == 0 and tied == [0]


async def test_guard_default_off_keeps_raw_law(session: AsyncSession) -> None:
    round_row = await tally_round(
        session,
        WinRule.MINORITY,
        path_votes={1: [1], 2: [2, 3]},
        stakers=[1],
    )
    await finish_tally(session, round_row)
    loaded = await session.get(Round, round_row.id)
    # Без guard сырой MINORITY побеждает путь 0 (0 голосов), даже если он без ставки.
    assert loaded.winner_card == 0


async def test_guard_minority_blocks_unstaked_zero_vote_path(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "win_rule_prefers_staked", True)
    round_row = await tally_round(
        session,
        WinRule.MINORITY,
        path_votes={1: [1], 2: [2, 3]},
        stakers=[1],
    )
    await finish_tally(session, round_row)
    loaded = await session.get(Round, round_row.id)
    # Путь 0 без единого ставщика не побеждает — деньги решают: путь 1.
    assert loaded.winner_card == 1
    monkeypatch.undo()


async def test_guard_majority_blocks_unstaked_majority(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "win_rule_prefers_staked", True)
    round_row = await tally_round(
        session,
        WinRule.MAJORITY,
        path_votes={0: [1, 2], 1: [3, 4]},
        stakers=[1, 2],
    )
    await finish_tally(session, round_row)
    loaded = await session.get(Round, round_row.id)
    # Сырой MAJORITY: пути 0 и 1 поровну (ничья, решается жребием по обязательству).
    # За путь 0 есть ставки, за путь 1 — нет → в любом случае выигрывает путь 0.
    assert loaded.winner_card == 0
    monkeypatch.undo()


async def test_staked_paths_finds_confirmed_only(session: AsyncSession) -> None:
    round_row = await tally_round(
        session,
        WinRule.MAJORITY,
        path_votes={0: [1], 1: [2]},
        stakers=[1],
    )
    # Добавить ставку в статусе pending — она не считается.
    session.add(
        Stake(
            round_id=round_row.id,
            player_id=2,
            amount_nanotons=to_nano(0.1),
            tx_hash="tx-pend",
            status="pending",
        )
    )
    await session.commit()
    staked = await _staked_paths(session, round_row.id)
    assert staked == {0}
    rows = (await session.execute(select(Stake))).scalars().all()
    assert len(rows) == 2
