"""Тона Следа: окраска эха/нюха клеткой сетки."""

from datetime import datetime, timedelta, timezone

from app.trail import trail_stats, trail_tint_line


def _stats(order: float, moral: float) -> dict:
    return {"order": order, "moral": moral, "total": 20,
            "conformity": 0.75, "heart_share": 0.6, "fang_share": 0.1}


def test_tint_covers_all_nine_cells() -> None:
    from app.trail import TRAIL_CELLS

    for x in (-1, 0, 1):
        for y in (-1, 0, 1):
            line = trail_tint_line(_stats(x * 0.9, y * 0.9))
            assert line is not None and "Твой След" in line
            assert f"«{TRAIL_CELLS[(x, y)]}»" in line


def test_tint_none_without_stats() -> None:
    assert trail_tint_line(None) is None
    assert trail_tint_line({}) is None


async def test_tint_from_real_history(session) -> None:

    from app.models import Card, Player, Round, RoundStatus, Vote, WinRule

    session.add(Player(id=70, username="tinter"))
    round_row = Round(
        day_index=1500,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        rule_commitment="c",
        chapter_title="t",
        chapter_text="x",
        lore_summary="l",
        opens_at=datetime.now(timezone.utc) - timedelta(days=2),
        voting_ends_at=datetime.now(timezone.utc) - timedelta(days=1),
        tally_ends_at=datetime.now(timezone.utc),
        winner_card=0,
        vote_counts_json="{}",
    )
    session.add(round_row)
    await session.flush()
    for position, tag in enumerate(("care", "risk", "cunning")):
        session.add(Card(round_id=round_row.id, position=position, title=f"t{position}",
                         description="d", consequence="c", image_path="", tag=tag))
    session.add(Vote(round_id=round_row.id, player_id=70, card_position=0))
    await session.commit()
    # Один день — ниже порога MIN_VOTES: тона нет.
    stats = await trail_stats(session, 70)
    assert stats is None
    assert trail_tint_line(stats) is None
