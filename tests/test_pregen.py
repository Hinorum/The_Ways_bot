"""Прегенерация: заготовка дня в час подсчёта, мгновенное открытие, мягкая деградация."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.art_director import offline_bible
from app.lore import compose_chapter
from app.models import Card, PreparedDay, Round, RoundStatus, StoryBeat, WatcherState, WinRule
from app.rounds import (
    PREPARED_PAYLOAD_VERSION,
    create_next_round_detailed,
    patch_prepared_day,
    prepare_next_day,
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


async def test_prepare_next_day_creates_prepared(session) -> None:
    round_row = await _seed_tallying_day(session)

    started = await prepare_next_day(session, round_row.day_index)
    assert started is True
    prepared = await session.get(PreparedDay, 6)
    assert prepared is not None
    payload = json.loads(prepared.payload)
    assert payload["day_index"] == 6
    # Фаза 1 кладёт три полные ветки дня (по одной на путь «вчера»).
    assert set(payload["branches"].keys()) == {"0", "1", "2"}
    for branch in payload["branches"].values():
        assert branch["chapter_title"]
        assert len(branch["cards"]) == 3
    # Плоской top-level главы ещё нет: её разложит патч победителя (фаза 2).
    assert not payload.get("chapter_title")
    # Лок снят после успеха.
    assert await session.get(WatcherState, "pregen_lock:6") is None


async def test_pregen_phase_one_builds_three_branches(session) -> None:
    """Фаза 1 (час подсчёта): три полные ветки дня — по одной на каждый из
    трёх путей «вчера». Итог ещё не вскрыт, поэтому ветки не патчатся — их
    выберет patch_prepared_day по фактическому победителю."""
    round_row = await _seed_tallying_day(session)
    assert await prepare_next_day(session, round_row.day_index) is True
    payload = json.loads((await session.get(PreparedDay, 6)).payload)
    assert int(payload["v"]) == PREPARED_PAYLOAD_VERSION
    branches = payload["branches"]
    assert len(branches) == 3
    # Каждая ветка — полная глава с началом от своего итога (не «затычка»).
    for branch in branches.values():
        assert branch["chapter_title"]
        assert branch["chapter_text"]
        assert len(branch["cards"]) == 3
    # Сетевой вызов генератора не понадобился при извлечении веток.
    assert not payload.get("chapter_title")


async def test_patch_applies_outcome_and_survives_materialization(session, monkeypatch) -> None:
    """    Фаза 2: по победителю (winner_card=0) выбрана его ветка, разложена в
    плоский payload и нарисована её обложка; материализация мгновенная."""
    round_row = await _seed_tallying_day(session)
    assert await prepare_next_day(session, round_row.day_index) is True

    session.add(
        StoryBeat(
            day_index=5,
            winning_title="Кабель в зубах",
            winning_text="Кабель удержался. Стая получила час форы.",
            win_rule="majority",
            vote_counts="{}",
        )
    )
    round_row.epilogue_text = "Ночь прошла тревожно: кабель трещал во сне."
    await session.commit()

    payload_before = json.loads((await session.get(PreparedDay, 6)).payload)
    expected_branch = payload_before["branches"]["0"]

    assert await patch_prepared_day(session, round_row) is True
    patched = json.loads((await session.get(PreparedDay, 6)).payload)
    assert patched["chapter_text"] == expected_branch["chapter_text"]
    assert "branches" not in patched
    assert patched.get("cover_path", "").endswith("_cover.jpg")

    created_round, created = await create_next_round_detailed(session)
    assert created is True
    assert created_round.chapter_text == expected_branch["chapter_text"]


async def test_patch_without_today_outcome_is_noop(session, monkeypatch) -> None:
    """Патч до записи StoryBeat ничего не делает и не генерирует зря."""
    round_row = await _seed_tallying_day(session)
    assert await prepare_next_day(session, round_row.day_index) is True

    assert await patch_prepared_day(session, round_row) is False


async def test_patch_offline_fallback_when_llm_silent(session, monkeypatch) -> None:
    """Патч берёт ветку победителя из готовых (даже если генерации были офлайн)."""

    round_row = await _seed_tallying_day(session)
    assert await prepare_next_day(session, round_row.day_index) is True
    session.add(
        StoryBeat(
            day_index=5,
            winning_title="Кабель в зубах",
            winning_text="Кабель удержался.",
            win_rule="majority",
            vote_counts="{}",
        )
    )
    await session.commit()

    payload_before = json.loads((await session.get(PreparedDay, 6)).payload)
    expected_branch = payload_before["branches"]["0"]  # ветка под winner_card=0

    assert await patch_prepared_day(session, round_row) is True
    patched = json.loads((await session.get(PreparedDay, 6)).payload)
    assert patched["chapter_text"] == expected_branch["chapter_text"]


async def test_patch_skips_when_no_prepared(session, monkeypatch) -> None:
    """Заготовки нет (сеть упала в фазе 1) — день честно соберётся синхронно."""
    round_row = await _seed_tallying_day(session)
    session.add(
        StoryBeat(
            day_index=5,
            winning_title="t",
            winning_text="x",
            win_rule="majority",
            vote_counts="{}",
        )
    )
    await session.commit()

    async def explode(**kwargs):
        raise AssertionError("без заготовки патчить нечего")

    assert await patch_prepared_day(session, round_row) is False


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
    await _seed_tallying_day(session)
    session.add(PreparedDay(day_index=6, payload="{битый json"))
    await session.commit()

    new_round, created = await create_next_round_detailed(session)
    assert created is True
    assert new_round.day_index == 6
    assert new_round.chapter_title
    assert await session.get(PreparedDay, 6) is None


async def test_prepared_from_other_version_is_discarded(session) -> None:
    """Заготовка чужой версии формата не материализуется: мир честно
    генерируется заново, а устаревшая строка удаляется."""
    await _seed_tallying_day(session)
    stale = json.dumps(
        {
            "v": 999,
            "day_index": 6,
            "rule": "majority",
            "commitment": "c:s",
            "chapter_title": "Устаревший формат",
            "chapter_text": "текст",
            "lore_summary": "лор",
            "cover_path": "",
            "cards": [],
        }
    )
    session.add(PreparedDay(day_index=6, payload=stale))
    await session.commit()

    new_round, created = await create_next_round_detailed(session)
    assert created is True
    assert new_round.chapter_title != "Устаревший формат"
    assert len(new_round.cards) == 3
    assert await session.get(PreparedDay, 6) is None


@pytest.mark.slow
async def test_reset_clears_prepared_days(session) -> None:
    from app.rounds import reset_game

    session.add(PreparedDay(day_index=9, payload="заготовка старого мира"))
    await session.commit()

    await reset_game(session)
    assert (await session.execute(select(PreparedDay))).scalars().all() == []
