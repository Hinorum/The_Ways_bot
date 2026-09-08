"""Единый повествовательный канон: один объект читает прошлое за один заход."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import LoreEcho, Round, StoryBeat
from app.narrative.canon import StoryCanon, _closing_hook, load_canon


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_closing_hook_respects_sentence_boundaries() -> None:
    text = "Первое предложение. Второе предложение! И хвост"
    # Ведущий пробел после точки — легаси-поведение _closing_hook, сохранено
    # байт-в-байт при переезде из rounds в canon (промпты не сдвигаются).
    assert _closing_hook(text) == " Второе предложение!"

    assert _closing_hook("Короткое предложение") == "Короткое предложение"
    assert _closing_hook("") == ""


async def test_load_canon_lines_match_previous_beats(session) -> None:
    from app.rounds import previous_beats

    canon = await load_canon(session)
    beats = await previous_beats(session)
    assert canon.lines == beats
    assert canon.titles == [line.split(":", 1)[0] for line in beats if line]


async def test_load_canon_order_and_window_cap(session) -> None:
    for day in range(1, 16):
        session.add(
            StoryBeat(
                day_index=day,
                winning_title=f"Глава {day}",
                winning_text="итог",
                hook_text="крючок",
                win_rule="majority",
                vote_counts="{}",
            )
        )
        session.add(
            Round(
                day_index=day,
                win_rule="majority",
                rule_commitment="c",
                chapter_title="Глава",
                chapter_text="текст",
                lore_summary="канон",
                opens_at=_now() + timedelta(hours=1),
                voting_ends_at=_now() + timedelta(hours=23),
                tally_ends_at=_now() + timedelta(hours=24),
                epilogue_text="эпилог",
            )
        )
    await session.commit()

    canon = await load_canon(session)
    assert len(canon.lines) == 12
    assert canon.lines[0].startswith("Глава 4:")  # старшие — раньше, окно 12
    assert canon.lines[11].startswith("Глава 15:")
    assert canon.day_indexes == [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    assert canon.last_title == "Глава 15"


async def test_load_canon_without_round_does_not_crash(session) -> None:
    session.add(
        StoryBeat(
            day_index=1,
            winning_title="Глава",
            winning_text="итог",
            win_rule="majority",
            vote_counts="{}",
        )
    )
    await session.commit()

    canon = await load_canon(session)
    assert canon.lines and "итог" in canon.lines[0]


async def test_load_canon_surfaces_due_echo(session) -> None:
    session.add(
        LoreEcho(
            born_day=1,
            source_day=1,
            kind="память",
            title="След",
            description="описание",
            strength=3,  # strength=3 не растворяется (нет fade-шанса)
            earliest_day=3,
            status="dormant",
        )
    )
    await session.commit()

    canon = await load_canon(session, day_index=3)
    assert len(canon.echoes) == 1
    assert canon.echoes[0].title == "След"
    assert canon.echoes[0].status == "surfaced"
    assert canon.echoes[0].surfaced_day == 3


async def test_load_canon_without_day_index_skips_echoes(session) -> None:
    session.add(
        LoreEcho(
            born_day=1,
            source_day=1,
            kind="память",
            title="След",
            description="описание",
            strength=3,
            earliest_day=1,
            status="dormant",
        )
    )
    await session.commit()

    canon = await load_canon(session)
    assert canon.echoes == []

    rows = (await session.execute(select(LoreEcho))).scalars().all()
    assert all(row.status == "dormant" for row in rows)


def test_story_canon_properties_empty() -> None:
    canon = StoryCanon()
    assert canon.lines == []
    assert canon.titles == []
    assert canon.last_title is None
    assert not canon.has_history