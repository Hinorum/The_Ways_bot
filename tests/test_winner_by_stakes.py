"""Новая механика (winner_by_stakes=True): ставки в Gram решают исход дня.

Закон дня (majority/minority/median) применяется к суммам подтверждённых
ставок на каждом пути. Путь без грамма участвует в подсчёте как 0.
День, где нет ни одного грамма, решается бесплатными голосами (fallback).
Бесплатные голоса на исход НЕ влияют — они питают только лидерборд
(score/correct_picks/стрики): верность = голос совпал с путём, выбранным
ставками. Легаси-режим (winner_by_stakes=False) проверен в
test_winrule_stake_guard.py.
"""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Card, Player, Round, RoundStatus, Stake, Vote, WinRule
from app.rounds import count_stakes_for_tally, count_votes_for_tally, finish_tally
from app.tally import award_points, format_results
from app.ton_utils import to_nano


def _round_row(rule: WinRule, day_index: int) -> Round:
    now = datetime.now(UTC)
    round_row = Round(
        day_index=day_index,
        status=RoundStatus.TALLYING,
        win_rule=rule,
        chapter_title="t",
        chapter_text="text",
        opens_at=now - timedelta(hours=25),
        voting_ends_at=now - timedelta(hours=1),
        tally_ends_at=now,
        vote_counts_json="{}",
    )
    for pos in (0, 1, 2):
        round_row.cards.append(
            Card(position=pos, title=f"t{pos}", description="d", consequence="к", image_path="")
        )
    return round_row


async def _seed_day(
    session: AsyncSession,
    rule: WinRule,
    votes: dict[int, list[int]],
    stakes: dict[int, list[tuple[int, float]]],
    day_index: int = 1,
) -> Round:
    """День в подсчёте. votes[путь] = игроки; stakes[путь] = [(игрок, Gram)].

    Ставщик без явного голоса в votes автоматически голосует за путь своей
    ставки (ставка привязана к пути через голос игрока).
    """
    round_row = _round_row(rule, day_index)
    session.add(round_row)
    await session.commit()
    seen: set[int] = set()
    for path, pids in votes.items():
        for pid in pids:
            session.add(Player(id=pid))
            seen.add(pid)
            session.add(Vote(round_id=round_row.id, player_id=pid, card_position=path))
    for path, entries in stakes.items():
        for pid, gram in entries:
            if pid not in seen:
                session.add(Player(id=pid))
                seen.add(pid)
                session.add(Vote(round_id=round_row.id, player_id=pid, card_position=path))
            session.add(
                Stake(
                    round_id=round_row.id,
                    player_id=pid,
                    amount_nanotons=to_nano(gram),
                    tx_hash=f"tx-{path}-{pid}",
                    status="confirmed",
                )
            )
    await session.commit()
    return round_row


async def test_majority_stakes_beat_free_votes(session: AsyncSession) -> None:
    # Сердце за путь 0 (5 голосов), деньги за путь 1 (3 Gram против 1 Gram).
    round_row = await _seed_day(
        session,
        WinRule.MAJORITY,
        votes={0: [1, 2, 3, 4, 5], 1: [6], 2: []},
        stakes={0: [(1, 1.0)], 1: [(6, 3.0)]},
    )
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 1
    # Решающий счёт ставок сохраняется; голоса — отдельно, для лидерборда.
    assert json.loads(closed.stake_counts_json) == {"0": to_nano(1), "1": to_nano(3), "2": 0}
    assert json.loads(closed.vote_counts_json) == {"0": 5, "1": 1, "2": 0}


async def test_minority_prefers_least_funded_path(session: AsyncSession) -> None:
    # Нулевой путь участвует как 0 Gram: MINORITY по ставкам → путь 2.
    round_row = await _seed_day(
        session,
        WinRule.MINORITY,
        votes={0: [1], 1: [2], 2: [3]},
        stakes={0: [(1, 2.0)], 1: [(2, 1.0)]},
    )
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 2


async def test_median_takes_middle_bank(session: AsyncSession) -> None:
    # Ставки: 0→1 Gram, 1→3 Gram, 2→5 Gram. MEDIAN — среднее значение 3 → путь 1.
    round_row = await _seed_day(
        session,
        WinRule.MEDIAN,
        votes={0: [1], 1: [2], 2: [3]},
        stakes={0: [(1, 1.0)], 1: [(2, 3.0)], 2: [(3, 5.0)]},
    )
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 1


async def test_day_without_stakes_falls_back_to_votes(session: AsyncSession) -> None:
    # Граммов на день нет вовсе — победителя выводят голоса, без stake-счёта.
    round_row = await _seed_day(
        session, WinRule.MAJORITY, votes={0: [1], 1: [2], 2: [3, 4]}, stakes={}
    )
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 2
    assert closed.stake_counts_json is None
    assert json.loads(closed.vote_counts_json) == {"0": 1, "1": 1, "2": 2}


async def test_pending_stakes_not_counted(session: AsyncSession) -> None:
    # Только confirmed: зависшая ставка 99 Gram пути 1 не перевешивает.
    round_row = await _seed_day(
        session,
        WinRule.MAJORITY,
        votes={0: [1], 1: [2]},
        stakes={0: [(1, 1.0)]},
    )
    session.add(
        Stake(
            round_id=round_row.id,
            player_id=2,
            amount_nanotons=to_nano(99),
            tx_hash="tx-pending",
            status="pending",
        )
    )
    await session.commit()
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 0


async def test_stake_without_vote_has_no_path(session: AsyncSession) -> None:
    # Ставка без голоса ни к какому пути не привязана — в счёт путей не входит,
    # день фактически без ставок: fallback на голоса.
    round_row = await _seed_day(
        session, WinRule.MAJORITY, votes={0: [1]}, stakes={}
    )
    session.add(Player(id=7))
    session.add(
        Stake(
            round_id=round_row.id,
            player_id=7,
            amount_nanotons=to_nano(9),
            tx_hash="tx-novote",
            status="confirmed",
        )
    )
    await session.commit()
    counts = await count_stakes_for_tally(session, round_row.id)
    assert counts == {0: 0, 1: 0, 2: 0}
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 0
    assert closed.stake_counts_json is None


async def test_revote_redirects_stake_weight(session: AsyncSession) -> None:
    # Игрок сменил голос (протокол смены выбора): его ставка усиливает новый путь.
    round_row = await _seed_day(
        session,
        WinRule.MAJORITY,
        votes={0: [1]},
        stakes={0: [(1, 3.0)]},
    )
    vote = (
        await session.execute(
            select(Vote).where(Vote.round_id == round_row.id, Vote.player_id == 1)
        )
    ).scalar_one()
    vote.card_position = 1
    await session.commit()
    counts = await count_stakes_for_tally(session, round_row.id)
    assert counts == {0: 0, 1: to_nano(3), 2: 0}
    # Исход тоже сместится: MAJORITY по ставкам теперь путь 1.
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 1


async def test_leaderboard_follows_stake_decided_winner(session: AsyncSession) -> None:
    # Сердце за путь 0 (1,2,3 голосуют), деньги (5 Gram) за путь 1 (игрок 4).
    # MAJORITY по ставкам → путь 1. Верными на лидерборде признаны те, кто
    # голосовал за путь 1, несмотря на перевес бесплатных голосов за путь 0.
    round_row = await _seed_day(
        session,
        WinRule.MAJORITY,
        votes={0: [1, 2, 3], 1: [4], 2: []},
        stakes={0: [(1, 1.0)], 1: [(4, 5.0)]},
    )
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 1
    await award_points(session, closed)
    rows = (await session.execute(select(Player))).scalars().all()
    res = {p.id: (p.score, p.correct_picks) for p in rows}
    # Все голосовавшие +1; верный голос (игрок 4) +10 и correct_picks=1.
    assert res[1] == (1, 0)
    assert res[2] == (1, 0)
    assert res[3] == (1, 0)
    assert res[4] == (11, 1)


async def test_vote_counts_tally_is_separate(session: AsyncSession) -> None:
    # count_votes_for_tally по-прежнему считает голоса, а count_stakes_for_tally
    # — Gram: два независимых счёта, решает только второй при winner_by_stakes.
    round_row = await _seed_day(
        session,
        WinRule.MAJORITY,
        votes={0: [1, 2], 1: [3], 2: []},
        stakes={0: [(1, 0.5)], 1: [(3, 4.0)]},
    )
    assert await count_votes_for_tally(session, round_row.id) == {0: 2, 1: 1, 2: 0}
    assert await count_stakes_for_tally(session, round_row.id) == {
        0: to_nano(0.5),
        1: to_nano(4.0),
        2: 0,
    }
    closed, _ = await finish_tally(session, round_row)
    assert closed.winner_card == 1


async def test_results_post_stake_decided_text(session: AsyncSession) -> None:
    # Финальный пост итогов: исход решили ставки — кадр уцелел по счёту Gram,
    # а строки «на волоске» для ставок больше нет (она осталась голосам).
    rnd = Round(
        day_index=5,
        win_rule=WinRule.MAJORITY,
        winner_card=2,
        vote_counts_json='{"0": 5, "1": 1, "2": 0}',
        stake_counts_json=json.dumps(
            {"0": 1_000_000_000, "1": 3_000_000_000, "2": 3_010_000_000}
        ),
    )
    for pos, title in ((0, "Тропа A"), (1, "Тропа B"), (2, "Тропа C")):
        rnd.cards.append(
            Card(position=pos, title=title, description="d", consequence="к", image_path="")
        )
    text = format_results(
        rnd,
        path_stakes={0: 1_000_000_000, 1: 3_000_000_000, 2: 3_010_000_000},
        multiplier=None,
    )
    assert "Тропа B" in text
    assert "Кадр дня уцелел по счёту Gram" in text
    assert "на волоске" not in text