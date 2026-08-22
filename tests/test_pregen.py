"""Прегенерация: заготовка дня в час подсчёта, мгновенное открытие, мягкая деградация."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.art_director import offline_bible
from app.lore import compose_chapter
from app.models import Card, PreparedDay, Round, RoundStatus, WatcherState, WinRule
from app.rounds import create_next_round_detailed, prepare_next_day


@pytest.fixture(autouse=True)
def offline_generation(monkeypatch):
    """Сетевые генераторы заменены мгновенными: тестируем только конвейер."""
    from app import rounds as rounds_mod

    async def instant_chapter(day_index, beats, rule=None, echoes=None, distant_echoes=None):
        return compose_chapter(day_index, beats, rule, echoes)

    monkeypatch.setattr(rounds_mod, "generate_chapter", instant_chapter)
    monkeypatch.setattr(
        rounds_mod,
        "plan_day_art",
        AsyncMock(side_effect=lambda chapter, beats=None, anchor=None: offline_bible(chapter)),
    )
    monkeypatch.setattr(rounds_mod, "fetch_day_image", AsyncMock(return_value=True))


async def _seed_tallying_day(session) -> Round:
    now = datetime.now(timezone.utc)
    round_row = Round(
        day_index=5,
        status=RoundStatus.TALLYING,
        win_rule=WinRule.MAJORITY,
        rule_commitment="c:s",
        chapter_title="День пятый",
        chapter_text="Текст дня.",
        lore_summary="Канон.",
        cover_path="",
        opens_at=now - timedelta(hours=30),
        voting_ends_at=now - timedelta(hours=1),
        tally_ends_at=now + timedelta(minutes=40),
        winner_card=0,
        vote_counts_json='{"0": 3}',
    )
    session.add(round_row)
    await session.commit()
    return round_row


async def test_prepare_next_day_creates_prepared(session) -> None:
    round_row = await _seed_tallying_day(session)

    started = await prepare_next_day(session, round_row.day_index)
    assert started is True
    prepared = await session.get(PreparedDay, 6)
    assert prepared is not None
    payload = json.loads(prepared.payload)
    assert payload["day_index"] == 6
    assert payload["chapter_title"]
    assert len(payload["cards"]) == 3
    # Лок снят после успеха.
    assert await session.get(WatcherState, "pregen_lock:6") is None


async def test_prepare_skips_when_already_prepared_or_locked(session) -> None:
    round_row = await _seed_tallying_day(session)

    assert await prepare_next_day(session, round_row.day_index) is True
    # Повторный вызов (следующий тик) — не плодит вторую генерацию.
    assert await prepare_next_day(session, round_row.day_index) is False
    assert (await session.execute(select(PreparedDay))).scalars().all() != []


async def test_prepare_refuses_while_lock_is_fresh(session) -> None:
    round_row = await _seed_tallying_day(session)
    stamp = str(int(datetime.now(timezone.utc).timestamp()))
    session.add(WatcherState(key="pregen_lock:6", value=stamp))
    await session.commit()

    assert await prepare_next_day(session, round_row.day_index) is False
    assert await session.get(PreparedDay, 6) is None


async def test_create_consumes_prepared_without_regeneration(session, monkeypatch) -> None:
    from app import rounds as rounds_mod

    round_row = await _seed_tallying_day(session)
    assert await prepare_next_day(session, round_row.day_index) is True

    async def explode(*args, **kwargs):
        raise AssertionError("генерация не должна запускаться при готовой заготовке")

    monkeypatch.setattr(rounds_mod, "generate_chapter", explode)

    created_round, created = await create_next_round_detailed(session)
    assert created is True
    assert created_round.day_index == 6
    assert created_round.status == RoundStatus.OPEN
    assert created_round.chapter_title
    # Заготовка израсходована.
    assert await session.get(PreparedDay, 6) is None
    cards = (
        await session.execute(select(Card).where(Card.round_id == created_round.id))
    ).scalars().all()
    assert len(cards) == 3


async def test_corrupt_prepared_falls_back_to_full_generation(session) -> None:
    round_row = await _seed_tallying_day(session)
    session.add(PreparedDay(day_index=6, payload="{битый json"))
    await session.commit()

    new_round, created = await create_next_round_detailed(session)
    assert created is True
    assert new_round.day_index == 6
    assert new_round.chapter_title
    assert await session.get(PreparedDay, 6) is None


async def test_reset_clears_prepared_days(session) -> None:
    from app.rounds import reset_game

    session.add(PreparedDay(day_index=9, payload="заготовка старого мира"))
    await session.commit()

    await reset_game(session)
    assert (await session.execute(select(PreparedDay))).scalars().all() == []
