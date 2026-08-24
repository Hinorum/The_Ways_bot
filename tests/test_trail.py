"""След игрока: оси из истории голосов, клетки сетки, порог малой выборки."""

from datetime import datetime, timedelta, timezone

from app.models import Card, Player, Round, RoundStatus, Vote, WinRule
from app.trail import MIN_VOTES, trail_cell, trail_line, trail_name, trail_stats


def _closed_round(session, day_index: int, winner: int) -> Round:
    round_row = Round(
        day_index=day_index,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        rule_commitment="c",
        chapter_title="t",
        chapter_text="x",
        lore_summary="l",
        opens_at=datetime.now(timezone.utc) - timedelta(days=1000 - day_index),
        voting_ends_at=datetime.now(timezone.utc) - timedelta(days=999 - day_index),
        tally_ends_at=datetime.now(timezone.utc) - timedelta(days=998 - day_index),
        winner_card=winner,
        vote_counts_json="{}",
    )
    session.add(round_row)
    return round_row


async def _seed_day(session, day_index: int, player_id: int, chosen: int, winner: int = 0):
    round_row = _closed_round(session, day_index, winner)
    await session.flush()
    for position, tag in enumerate(("care", "risk", "cunning")):
        session.add(
            Card(
                round_id=round_row.id,
                position=position,
                title=f"p{position}",
                description="d",
                consequence="c",
                image_path="",
                tag=tag,
            )
        )
    session.add(Vote(round_id=round_row.id, player_id=player_id, card_position=chosen))
    return round_row


async def test_cold_start_returns_none(session) -> None:
    session.add(Player(id=1, username="new"))
    await session.commit()
    assert await trail_stats(session, 1) is None


async def test_axes_and_cell(session) -> None:
    session.add(Player(id=2, username="steady"))
    # 12 дней: 9 голосов за победителя (хор), все care-пути (сердце).
    for index in range(12):
        await _seed_day(session, 700 + index, 2, chosen=0 if index < 9 else 1)
    await session.commit()
    stats = await trail_stats(session, 2)
    assert stats is not None and stats["total"] == 12
    assert abs(stats["conformity"] - 0.75) < 1e-9
    assert stats["order"] > 0.3
    assert stats["moral"] == 1.0
    name = trail_name(stats["order"], stats["moral"])
    assert name in ("Пастух", "Овчарка устава")


async def test_bunt_and_fang_cell(session) -> None:
    session.add(Player(id=3, username="bunt"))
    for index in range(MIN_VOTES + 4):
        # Все голоса мимо победителя и всегда cunning.
        await _seed_day(session, 800 + index, 3, chosen=2, winner=0)
    await session.commit()
    stats = await trail_stats(session, 3)
    assert stats is not None
    assert trail_cell(stats["order"], stats["moral"]) == (-1, -1)
    assert trail_name(stats["order"], stats["moral"]) == "Грызло"


async def test_open_rounds_do_not_count(session) -> None:
    session.add(Player(id=4, username="u"))
    await _seed_day(session, 900, 4, chosen=0)
    open_round = Round(
        day_index=901,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        rule_commitment="c",
        chapter_title="t",
        chapter_text="x",
        lore_summary="l",
        opens_at=datetime.now(timezone.utc),
        voting_ends_at=datetime.now(timezone.utc) + timedelta(hours=10),
        tally_ends_at=datetime.now(timezone.utc) + timedelta(hours=11),
        vote_counts_json="{}",
    )
    session.add(open_round)
    await session.flush()
    session.add(Vote(round_id=open_round.id, player_id=4, card_position=0))
    await session.commit()
    stats = await trail_stats(session, 4)
    # Один закрытый день — ниже порога.
    assert stats is None


def test_pure_helpers() -> None:
    assert trail_cell(0.9, 0.9) == (1, 1)
    assert trail_cell(-0.5, 0.1) == (-1, 0)
    assert trail_cell(0.1, -0.8) == (0, -1)
    line = trail_line(
        {
            "order": 0.5,
            "moral": 0.0,
            "total": 20,
            "conformity": 0.75,
            "heart_share": 0.6,
            "fang_share": 0.1,
        }
    )
    assert "След" in line and "20 голосам" in line
