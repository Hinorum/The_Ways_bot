from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.echoes import collect_due_echoes
from app.lore import compose_chapter
from app.models import Base, Card, LoreEcho, Player, Round, RoundStatus, WinRule, Vote
from app.rounds import create_next_round_detailed, finish_tally, get_round
from app.tally import award_points
from app.voting import cast_vote


def _round_kwargs(day_index: int) -> dict:
    now = datetime.now(timezone.utc)
    return dict(
        day_index=day_index,
        status=RoundStatus.TALLYING,
        win_rule=WinRule.MAJORITY,
        rule_commitment="commit",
        chapter_title=f"День {day_index}",
        chapter_text="text",
        lore_summary="lore",
        opens_at=now - timedelta(hours=23),
        voting_ends_at=now - timedelta(hours=1),
        tally_ends_at=now,
    )


async def _make_round(session, day_index: int = 1) -> Round:
    round_row = Round(**_round_kwargs(day_index))
    session.add(round_row)
    await session.flush()
    for position in range(3):
        session.add(
            Card(
                round_id=round_row.id,
                position=position,
                title=f"Карта {position}",
                description="d",
                image_path="p",
                consequence=f"след {position}",
            )
        )
    await session.commit()
    return await get_round(session, round_row.id)


async def _add_player_and_vote(session, player_id: int, round_id: int, position: int) -> None:
    if await session.get(Player, player_id) is None:
        session.add(Player(id=player_id, username=f"u{player_id}", first_name=f"P{player_id}"))
    session.add(Vote(round_id=round_id, player_id=player_id, card_position=position))
    await session.commit()


async def test_cast_vote_rejects_invalid_position(session) -> None:
    round_row = await _make_round(session)
    session.add(Player(id=1))
    await session.commit()
    assert await cast_vote(session, round_row, 1, 5) == "invalid"
    assert await cast_vote(session, round_row, 1, -1) == "invalid"


async def test_cast_vote_rejects_duplicate(session) -> None:
    round_row = await _make_round(session)
    round_row.status = RoundStatus.OPEN
    await session.commit()
    session.add(Player(id=1))
    await session.commit()
    assert await cast_vote(session, round_row, 1, 0) == "ok"
    assert await cast_vote(session, round_row, 1, 2) == "already"


async def test_finish_tally_closes_once(session) -> None:
    round_row = await _make_round(session)
    for pid, pos in [(1, 0), (2, 0), (3, 1)]:
        await _add_player_and_vote(session, pid, round_row.id, pos)
    finished, closed = await finish_tally(session, round_row)
    assert closed is True
    assert finished.winner_card == 0
    again, closed_again = await finish_tally(session, finished)
    assert closed_again is False


async def test_award_points_once_per_close(session) -> None:
    round_row = await _make_round(session)
    for pid, pos in [(1, 0), (2, 0), (3, 1)]:
        await _add_player_and_vote(session, pid, round_row.id, pos)
    finished, closed = await finish_tally(session, round_row)
    assert closed
    winners = await award_points(session, finished)
    assert winners == 2
    p1 = await session.get(Player, 1)
    assert p1.score == 11 and p1.correct_picks == 1
    p3 = await session.get(Player, 3)
    assert p3.score == 1 and p3.correct_picks == 0


async def test_every_choice_spawns_echoes(session) -> None:
    round_row = await _make_round(session)
    for pid, pos in [(1, 0), (2, 1), (3, 1)]:
        await _add_player_and_vote(session, pid, round_row.id, pos)
    finished, closed = await finish_tally(session, round_row)
    assert closed
    echoes = (await session.execute(select(LoreEcho))).scalars().all()
    assert len(echoes) == 3
    winner_echoes = [e for e in echoes if e.strength == 3]
    loser_echoes = [e for e in echoes if e.status == "dormant" and e.strength < 3]
    assert len(winner_echoes) == 1
    assert len(loser_echoes) == 2


async def test_echo_surfaces_and_chains(session) -> None:
    round_row = await _make_round(session)
    for pid, pos in [(1, 0), (2, 1), (3, 1)]:
        await _add_player_and_vote(session, pid, round_row.id, pos)
    finished, _closed = await finish_tally(session, round_row)
    day = finished.day_index + 5
    due = await collect_due_echoes(session, day)
    assert due
    assert all(e.status == "surfaced" for e in due)
    assert any(e.strength == 3 for e in due)
    chained = (
        await session.execute(select(LoreEcho).where(LoreEcho.born_day == day))
    ).scalars().all()
    assert any("след" in e.title for e in chained)


async def test_weak_echoes_may_fade(session) -> None:
    await _make_round(session)
    for i in range(1, 21):
        session.add(
            LoreEcho(
                born_day=1,
                source_day=1,
                kind="память",
                title=f"T{i}",
                description=f"D{i}",
                strength=1,
                earliest_day=2,
                status="dormant",
            )
        )
    await session.commit()
    surfaced = await collect_due_echoes(session, 10, limit=20)
    weak = (
        await session.execute(select(LoreEcho).where(LoreEcho.strength == 1))
    ).scalars().all()
    statuses = {row.status for row in weak}
    assert len(surfaced) == len([row for row in weak if row.status == "surfaced"])
    assert statuses == {"surfaced", "faded"}


def test_compose_weaves_echo_subtly() -> None:
    echo = SimpleNamespace(
        kind="угроза",
        title="Ржавый ключ",
        description="В каноне появляется ржавый ключ.",
    )
    woven = compose_chapter(9, [], WinRule.MAJORITY, [echo])
    assert "В каноне появляется ржавый ключ." in woven["text"]
    assert any("Ржавый ключ" in card["consequence"] for card in woven["cards"])
    low = woven["text"].lower()
    for banned in ("эхо", "отголосок", "прошлый день", "голосование"):
        assert banned not in low


def test_compose_has_cover_prompt() -> None:
    chapter = compose_chapter(7, ["Костёр стаи: появился общий костёр"], WinRule.MEDIAN)
    assert chapter["cover_prompt"]
    assert "no text" in chapter["cover_prompt"]


async def test_create_round_builds_cover_and_tags(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "use_free_images", False)
    monkeypatch.setattr(settings, "use_free_story_llm", False)
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rounds.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        round_row, created = await create_next_round_detailed(session)
        assert created is True
        assert round_row.cover_path.endswith("_cover.jpg")
        from pathlib import Path

        assert Path(round_row.cover_path).exists()
        assert len(round_row.cards) == 3
        assert {card.tag for card in round_row.cards} == {"risk", "care", "cunning"}
    await engine.dispose()
