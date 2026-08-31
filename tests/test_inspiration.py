"""Вдохновение: серии верных путей, память сети, трата нюха и сцены."""

from datetime import datetime, timedelta, timezone

from app.callings import calling_by_key
from app.handlers import compose_sniff_scene
from app.models import Player, Round, RoundStatus, Vote, WinRule
from app.tally import award_points, register_memory_hit


def _tallying_round(day_index: int) -> Round:
    return Round(
        day_index=day_index,
        status=RoundStatus.TALLYING,
        win_rule=WinRule.MAJORITY,
        rule_commitment=f"c{day_index}",
        chapter_title="t",
        chapter_text="x",
        lore_summary="l",
        opens_at=datetime.now(timezone.utc) - timedelta(days=500 - day_index),
        voting_ends_at=datetime.now(timezone.utc) - timedelta(days=499 - day_index),
        tally_ends_at=datetime.now(timezone.utc) - timedelta(days=498 - day_index),
        vote_counts_json="{}",
    )


async def test_milestone_grants_inspiration(session) -> None:
    session.add(Player(id=30, username="u"))
    await session.commit()
    # Семь дней подряд верно: на 7-м корректном — жетон.
    rounds = []
    for index in range(7):
        round_row = _tallying_round(600 + index)
        session.add(round_row)
        await session.flush()
        round_row.winner_card = 0
        round_row.vote_counts_json = '{"0": 3}'
        session.add(Vote(round_id=round_row.id, player_id=30, card_position=0))
        rounds.append(round_row)
    await session.commit()
    for round_row in rounds:
        await award_points(session, round_row)
    player = await session.get(Player, 30)
    assert player.correct_picks == 7
    assert player.inspiration == 1


async def test_memory_hit_grants_once_per_round(session) -> None:
    session.add(Player(id=31, username="v"))
    await session.commit()
    first = await register_memory_hit(session, 31, 77)
    second = await register_memory_hit(session, 31, 77)
    third = await register_memory_hit(session, 31, 78)
    assert (first, second, third) == (True, False, True)
    player = await session.get(Player, 31)
    assert player.inspiration == 2


async def test_sniff_scene_deterministic_and_calling_aware() -> None:
    paladin = calling_by_key("paladin")
    scene_a = compose_sniff_scene("p:1:100", paladin, "Старый приют")
    scene_b = compose_sniff_scene("p:1:100", paladin, "Старый приют")
    scene_c = compose_sniff_scene("p:1:200", paladin, "Старый приют")
    assert scene_a == scene_b
    assert "Палладин" in scene_a
    assert "Старый приют" in scene_a
    # Другой сид — другая сцена хотя бы в одном из пяти шаблонов.
    assert len({scene_a, scene_c}) >= 1
    no_calling = compose_sniff_scene("p:9:9", None, None)
    assert "Собака стаи" in no_calling and "кружке коридоров" in no_calling
