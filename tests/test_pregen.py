"""Инлайн-день: новый день рендерится сразу целиком (без двухфазной
прегенерации), библия дня сохраняется в watcher_state, остатки заготовок
чистятся, а банк повторов дедуплицирует формулировки и места канона."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.art_director import offline_bible
from app.lore import compose_chapter
from app.models import Card, PreparedDay, Round, RoundStatus, StoryBeat, WatcherState, WinRule
from app.rounds import (
    _day_bible_key,
    _load_day_bible,
    _save_day_bible,
    create_next_round_detailed,
    recent_repeats_block,
    reset_game,
)


@pytest.fixture(autouse=True)
def offline_generation(monkeypatch):
    """Сетевые генераторы заменены мгновенными: тестируем только конвейер."""
    from app import rounds as rounds_mod

    async def instant_chapter(
        day_index,
        beats,
        rule=None,
        echoes=None,
        distant_echoes=None,
        season_block=None,
        villain_block=None,
        sealed=False,
        pending_outcome=False,
        **kwargs,
    ):
        return compose_chapter(
            day_index, beats, rule, echoes, distant_echoes,
            season_block=season_block, villain_line=villain_block,
            sealed=sealed, pending_outcome=pending_outcome,
        )

    monkeypatch.setattr(rounds_mod, "generate_chapter", instant_chapter)
    monkeypatch.setattr(
        rounds_mod,
        "plan_day_art",
        AsyncMock(side_effect=lambda chapter, beats=None, anchor=None, extra_motifs=None: offline_bible(chapter)),
    )
    monkeypatch.setattr(rounds_mod, "fetch_day_image", AsyncMock(return_value=True))


async def _seed_day(session, day_index: int, chapter_title: str = "День") -> Round:
    now = datetime.now(timezone.utc)
    round_row = Round(
        day_index=day_index,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        rule_commitment="c:s",
        chapter_title=chapter_title,
        chapter_text="Текст дня.",
        lore_summary="Канон.",
        cover_path="",
        place="Старый приют",
        opens_at=now - timedelta(hours=30),
        voting_ends_at=now - timedelta(hours=1),
        tally_ends_at=now + timedelta(minutes=40),
        winner_card=0,
        vote_counts_json='{"0": 3}',
    )
    for position in (0, 1, 2):
        round_row.cards.append(
            Card(
                position=position,
                title=f"Тропа {position}",
                description="Описание.",
                consequence=f"Итог пути {position}.",
                image_path="",
            )
        )
    session.add(round_row)
    await session.commit()
    return round_row


async def test_new_day_is_rendered_inline(session) -> None:
    """Нет заготовки — день рендерится сразу целиком и открывается."""
    await _seed_day(session, 5)

    new_round, created = await create_next_round_detailed(session)
    assert created is True
    assert new_round.day_index == 6
    assert new_round.status == RoundStatus.OPEN
    assert new_round.chapter_title
    cards = (
        await session.execute(select(Card).where(Card.round_id == new_round.id))
    ).scalars().all()
    assert len(cards) == 3


async def test_first_day_renders_on_empty_db(session) -> None:
    new_round, created = await create_next_round_detailed(session)
    assert created is True
    assert new_round.day_index == 1
    assert new_round.chapter_title


async def test_stale_prepared_row_is_cleared(session) -> None:
    """Остаток старой двухфазной прегенерации не перезаписывает новый день."""
    await _seed_day(session, 5)
    session.add(PreparedDay(day_index=6, payload="заготовка вчерашней ночи"))
    await session.commit()

    new_round, created = await create_next_round_detailed(session)
    assert created is True
    assert new_round.chapter_title
    assert await session.get(PreparedDay, 6) is None


async def test_duplicate_day_race_returns_existing(session) -> None:
    """День уже открыт тиком — повторный вызов не эскалирует в следующий."""
    existing = await _seed_day(session, 6)
    row, created = await create_next_round_detailed(session, base_day_index=5)
    assert created is False
    assert row.day_index == existing.day_index


async def test_day_bible_roundtrip_via_watcher_state(session) -> None:
    """Полная библия дня хранится под art_bible:{day_index} и читается обратно."""
    bible = {
        "palette": "rust orange over teal",
        "lighting": "low sun through fog",
        "shots": {"cover": {"scene": "aerial view of the pack at the gates"}},
        "motifs": ["rusted iron", "drifting sparks"],
    }
    await _save_day_bible(session, 3, bible)

    row = await session.get(WatcherState, _day_bible_key(3))
    assert row is not None and row.value
    loaded = await _load_day_bible(session, 3)
    assert loaded["palette"] == bible["palette"]
    assert loaded["shots"]["cover"]["scene"] == bible["shots"]["cover"]["scene"]

    # Нет записи — None без исключений.
    assert await _load_day_bible(session, 99) is None


async def test_create_round_persists_day_bible(session) -> None:
    await _seed_day(session, 5)
    await create_next_round_detailed(session)

    bible = await _load_day_bible(session, 6)
    assert bible is not None
    assert bible.get("shots")


async def test_recent_repeats_block_gathers_canon(session) -> None:
    """Банк повторов собирает места и формулировки последних дней."""
    await _seed_day(session, 4, chapter_title="День четвёртый")
    await _seed_day(session, 5, chapter_title="День пятый")

    block = await recent_repeats_block(session, 7)
    assert block is not None
    assert "Старый приют" in block
    assert "Тропа" in block
    # Окно шире дня: день под самими собой не попадает (day_index 4 в окне).
    block_again = await recent_repeats_block(session, 8)
    assert block_again is not None


async def test_recent_repeats_block_empty(session) -> None:
    await _seed_day(session, 1)
    # Ни одного прошлого дня в окне ≥7 — блока нет.
    assert await recent_repeats_block(session, 1) is None


@pytest.mark.slow
async def test_reset_clears_prepared_rows_and_stale_day_bibles(session) -> None:
    session.add(PreparedDay(day_index=9, payload="заготовка старого мира"))
    await _save_day_bible(session, 9, {"shots": {}, "palette": "x", "lighting": "y"})
    await session.commit()

    await reset_game(session)
    assert (await session.execute(select(PreparedDay))).scalars().all() == []
    # Старые библии стёрты; сброс открыл новый первый день — его библия своя.
    stale = (
        await session.execute(
            select(WatcherState).where(WatcherState.key == _day_bible_key(9))
        )
    ).scalar_one_or_none()
    assert stale is None
    fresh = (
        await session.execute(
            select(WatcherState).where(WatcherState.key == _day_bible_key(1))
        )
    ).scalar_one_or_none()
    assert fresh is not None and fresh.value


async def test_added_for_roundtrip_storybeat_safety(session) -> None:
    """StoryBeat-запись дня не мешает materialize следующего (регрессия фазы 2)."""
    await _seed_day(session, 5)
    session.add(
        StoryBeat(
            day_index=5,
            winning_title="Кабель в зубах",
            winning_text="Кабель удержался. Стая получила час форы.",
            win_rule="majority",
            vote_counts="{}",
        )
    )
    await session.commit()

    new_round, created = await create_next_round_detailed(session)
    assert created is True
    assert await _load_day_bible(session, new_round.day_index) is not None