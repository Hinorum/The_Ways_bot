"""Призвания: разблокировка по счётчикам, хвосты эха, промпт-блок, выбор."""

from datetime import datetime, timedelta, timezone

from app.callings import (
    CALLINGS,
    calling_by_key,
    callings_prompt_block,
    echo_tail,
    unlocked_callings,
)
from app.models import Card, Player, Round, RoundStatus, Vote, WinRule


async def _closed_round(session, day_index: int, winner: int, rule=WinRule.MAJORITY, sealed=False):
    round_row = Round(
        day_index=day_index,
        status=RoundStatus.CLOSED,
        win_rule=rule,
        rule_commitment=f"c{day_index}",
        sealed=sealed,
        chapter_title="t",
        chapter_text="x",
        lore_summary="l",
        opens_at=datetime.now(timezone.utc) - timedelta(days=300 - day_index),
        voting_ends_at=datetime.now(timezone.utc) - timedelta(days=299 - day_index),
        tally_ends_at=datetime.now(timezone.utc) - timedelta(days=298 - day_index),
        winner_card=winner,
        vote_counts_json="{}",
    )
    session.add(round_row)
    await session.flush()
    for position, tag in enumerate(("care", "risk", "cunning")):
        session.add(
            Card(
                round_id=round_row.id,
                position=position,
                title=f"t{position}",
                description="d",
                consequence="c",
                image_path="",
                tag=tag,
            )
        )
    return round_row


async def test_nothing_unlocked_for_newcomer(session) -> None:
    session.add(Player(id=10, username="new"))
    await session.commit()
    progress = {
        "correct_picks": 0, "votes": 0, "care_votes": 0, "cunning_votes": 0,
        "heart_lead": 0, "minority_correct": 0, "sealed_correct": 0, "memory_hits": 0,
    }
    assert unlocked_callings(progress) == []


async def test_hound_and_bard_unlock_by_counters(session) -> None:
    session.add(Player(id=11, username="vet", correct_picks=6))
    for index in range(21):
        _round = await _closed_round(session, 100 + index, 0)
        session.add(Vote(round_id=_round.id, player_id=11, card_position=index % 3))
    await session.commit()
    from app.callings import calling_progress

    progress = await calling_progress(session, 11)
    keys = {c.key for c in unlocked_callings(progress)}
    assert {"hound", "bard"} <= keys
    assert progress["votes"] == 21
    assert progress["correct_picks"] == 6


async def test_trickster_mutant_guardian_conditions(session) -> None:
    session.add(Player(id=12, username="mixed", correct_picks=2))
    plan = [
        (WinRule.MINORITY, False), (WinRule.MINORITY, False), (WinRule.MAJORITY, True),
        (WinRule.MAJORITY, False), (WinRule.MAJORITY, False),
    ]
    for index, (rule, sealed) in enumerate(plan):
        _round = await _closed_round(session, 200 + index, 0, rule=rule, sealed=sealed)
        session.add(Vote(round_id=_round.id, player_id=12, card_position=0))
    await session.commit()
    from app.callings import calling_progress

    progress = await calling_progress(session, 12)
    assert progress["minority_correct"] == 2
    assert progress["sealed_correct"] == 1
    assert progress["care_votes"] == 5 and progress["cunning_votes"] == 0
    keys = {c.key for c in unlocked_callings(progress)}
    assert {"trickster", "mutant", "guardian"} <= keys


async def test_memory_hits_unlock_archivist(session) -> None:
    session.add(Player(id=13, username="mem"))
    from app.models import MemoryHit

    for round_id in (1, 2, 3):
        session.add(MemoryHit(player_id=13, round_id=round_id))
    await session.commit()
    from app.callings import calling_progress

    progress = await calling_progress(session, 13)
    assert progress["memory_hits"] >= 3
    assert any(c.key == "archivist" for c in unlocked_callings(progress))


async def test_prompt_block_lists_only_present(session) -> None:
    session.add_all([
        Player(id=14, username="a", calling="hound"),
        Player(id=15, username="b", calling="hound"),
        Player(id=16, username="c", calling="mutant"),
    ])
    await session.commit()
    block = await callings_prompt_block(session)
    assert block is not None and "Гончая-следопыт — 2" in block and "Мутант-слепень — 1" in block
    # Пустая стая — блок не строится.
    from sqlalchemy import delete

    await session.execute(delete(Player))
    await session.commit()
    assert await callings_prompt_block(session) is None


def test_echo_tails_cover_every_calling() -> None:
    for calling in CALLINGS:
        assert echo_tail(calling.key)
    assert echo_tail(None) in ("", None)


def test_lookup_unknown_key_is_none() -> None:
    assert calling_by_key("dragon") is None
    assert calling_by_key(None) is None
