"""Тесты сезонов мира: арка привязана к забегу, акты и финал Первого Лая."""

from __future__ import annotations

from datetime import datetime, timezone

from app import rounds as rounds_mod
from app.config import settings
from app.models import Card, Round, RoundStatus, StoryBeat, WinRule
from app.season import (
    act_line,
    is_run_finale,
    finale_instruction,
    run_position,
    season_block,
    season_key,
)
from app.story import _build_story_prompt


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_season_key_format() -> None:
    assert season_key(_utc(2026, 12, 31, 23, 59)) == "2026-12"
    assert season_key(datetime(2026, 1, 1)) == "2026-01"  # наивное время = UTC


def _one_month(monkeypatch) -> None:
    """Legacy-режим: арка = один календарный месяц (для точных границ)."""
    from app.config import settings

    monkeypatch.setattr(settings, "run_length_months", 1)


def test_finale_is_last_day_of_run(monkeypatch) -> None:
    """Забег, стартовавший 1-го числа, завершается последним календарным днём."""
    _one_month(monkeypatch)
    cases = [
        (_utc(2026, 8, 31), True),
        (_utc(2026, 2, 27), False),
        (_utc(2026, 12, 31), True),
        (_utc(2026, 4, 30), True),
    ]
    for moment, expected in cases:
        anchor_case = {"dom": 1, "key": f"{moment.year:04d}-{moment.month:02d}"}
        run_day, total = run_position(anchor_case, moment)
        assert is_run_finale(run_day, total) is expected


def test_leap_february_run_length(monkeypatch) -> None:
    monkeypatch.setattr(settings, "run_length_months", 1)
    anchor = {"dom": 1, "key": "2028-02"}
    _, total = run_position(anchor, _utc(2028, 2, 29))
    assert total == 29
    anchor = {"dom": 1, "key": "2026-02"}
    _, total = run_position(anchor, _utc(2026, 2, 28))
    assert total == 28


def test_act_progression_and_countdown() -> None:
    line_early = act_line(3, 31)
    assert line_early.lower().startswith("акт 1") and "осталось 28" in line_early
    line_mid = act_line(15, 31)
    assert line_mid.lower().startswith("акт 2") and "осталось 16" in line_mid
    line_crisis = act_line(26, 31)
    assert line_crisis.lower().startswith("акт 3") and "осталось 5" in line_crisis
    finale_line = act_line(31, 31)
    assert "ДЕНЬ ПЕРВОГО ЛАЯ" in finale_line


def test_run_wraps_after_month_length(monkeypatch) -> None:
    """Одномесячный забег циклится по границе своей арки (эпоха: первый сезон
    короткий — first_season_months; следующие — run_length_months)."""
    _one_month(monkeypatch)  # run_length_months=1
    monkeypatch.setattr(settings, "first_season_months", 1)
    anchor = {"dom": 24, "key": "2026-08"}

    # Сезон 1: 24 авг → 23 сен = 31 день. Последний день — финальный.
    last_day, total = run_position(anchor, _utc(2026, 9, 23))
    assert (last_day, total) == (31, 31)
    # На следующий день арка циклится: старт нового сезона.
    run_day, _total = run_position(anchor, _utc(2026, 9, 24))
    assert run_day == 1


def test_two_month_arc_is_default(monkeypatch) -> None:
    """Двухмесячная арка: при первом сезоне в два месяца 24 авг → 23 окт = 61 день."""
    monkeypatch.setattr(settings, "first_season_months", 2)
    monkeypatch.setattr(settings, "run_length_months", 2)
    anchor = {"dom": 24, "key": "2026-08"}
    _, total = run_position(anchor, _utc(2026, 10, 24))
    assert total == 61
    finale_day, _t = run_position(anchor, _utc(2026, 10, 23))
    from app.season import is_run_finale

    assert is_run_finale(finale_day, _t)


def test_finale_instruction_maps_balance_to_flavour() -> None:
    care_block = finale_instruction({"risk": 0, "care": 9, "cunning": 1})
    assert "ДЕНЬ ПЕРВОГО ЛАЯ" in care_block
    assert "дом" in care_block and "ловушка" in care_block and "зовом" in care_block
    risk_block = finale_instruction({"risk": 9, "care": 0, "cunning": 0})
    assert "обнажёнными клыками" in risk_block


def test_season_block_opener_on_first_days() -> None:
    anchor = {"dom": 1, "key": "2026-09"}
    opener = season_block(
        anchor=anchor,
        moment=_utc(2026, 9, 1, 11, 0),
        previous_season_summary="Лай был ловушкой: стая сломала капкан.",
    )
    assert "НОВЫЙ СЕЗОН" in opener
    assert "капкан" in opener  # осадок прошлого финала передан модели
    # Обычный день сезона — без опенер-блока.
    regular = season_block(anchor=anchor, moment=_utc(2026, 9, 10, 11, 0))
    assert "НОВЫЙ СЕЗОН" not in regular
    assert "осталось" in regular


def test_story_prompt_carries_season_and_place_fields() -> None:
    prompt = _build_story_prompt(
        30,
        ["Путь: стая у моста"],
        season_block="СЕГОДНЯ — ДЕНЬ ПЕРВОГО ЛАЯ, финал сезона.",
        places_block='- «Мост из костей»: стая перешла его без потерь.',
    )
    assert "ДЕНЬ ПЕРВОГО ЛАЯ" in prompt
    assert "Мост из костей" in prompt
    assert '"place"' in prompt  # поле места в JSON-схеме ответа


# ---------- Интеграционные: баланс тегов и память мест ----------


async def test_season_tag_balance_counts_winner_tags(session) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession  # noqa: F401

    async def mk_round(day: int, season: str, winner: int, tags: list[str], closed=True):
        round_row = Round(
            day_index=day,
            status=RoundStatus.CLOSED if closed else RoundStatus.OPEN,
            win_rule=WinRule.MAJORITY,
            rule_commitment="c",
            chapter_title="t",
            chapter_text="text",
            lore_summary="lore",
            opens_at=_utc(2026, 9, 1, 11, 0),
            voting_ends_at=_utc(2026, 9, 2, 10, 0),
            tally_ends_at=_utc(2026, 9, 2, 11, 0),
            winner_card=winner if closed else None,
            season=season,
        )
        session.add(round_row)
        await session.flush()
        for pos, tag in enumerate(tags):
            session.add(Card(round_id=round_row.id, position=pos, title=f"T{pos}",
                             description="d", image_path="", consequence="c", tag=tag))
        return round_row

    await mk_round(1, "2026-09", 0, ["risk", "care", "cunning"])
    await mk_round(2, "2026-09", 2, ["care", "care", "cunning"])
    # Чужой сезон не считается.
    await mk_round(90, "2026-08", 1, ["risk", "risk", "risk"])
    # Открытый день сезона тоже нет.
    await mk_round(30, "2026-09", 0, ["cunning", "care", "care"], closed=False)
    await session.commit()

    balance = await rounds_mod.season_tag_balance(session, "2026-09")
    assert balance == {"risk": 1, "care": 0, "cunning": 1}
    assert await rounds_mod.season_tag_balance(session, "2026-08") == {"risk": 1, "care": 0, "cunning": 0}


async def test_previous_season_summary_takes_last_beat(session) -> None:
    session.add(Round(
        day_index=50, status=RoundStatus.CLOSED, win_rule=WinRule.MINORITY,
        rule_commitment="c", chapter_title="t", chapter_text="x", lore_summary="l",
        opens_at=_utc(2026, 8, 30, 11, 0), voting_ends_at=_utc(2026, 8, 31, 10, 0),
        tally_ends_at=_utc(2026, 8, 31, 11, 0), winner_card=1, season="2026-08",
    ))
    session.add(StoryBeat(day_index=49, winning_title="Старый путь", winning_text="старый след",
                          win_rule="majority", vote_counts="{}"))
    session.add(StoryBeat(day_index=50, winning_title="Первый Лай", winning_text="стая выбрала дом",
                          win_rule="minority", vote_counts="{}"))
    await session.commit()

    summary = await rounds_mod.previous_season_summary(session, "2026-09")
    assert summary is not None and summary.startswith("Первый Лай")
    # Нет прошлого сезона — нет осадка.
    assert await rounds_mod.previous_season_summary(session, "2099-05") is None


async def test_places_memory_lists_recent_named_rounds(session) -> None:
    for day, place in ((60, "Мост из костей"), (61, "Ярмарка Лайнеров"), (62, None)):
        session.add(Round(
            day_index=day, status=RoundStatus.CLOSED, win_rule=WinRule.MAJORITY,
            rule_commitment="c", chapter_title="t", chapter_text="x", lore_summary="l",
            opens_at=_utc(2026, 9, 1, 11, 0), voting_ends_at=_utc(2026, 9, 2, 10, 0),
            tally_ends_at=_utc(2026, 9, 2, 11, 0), place=place, season="2026-09",
        ))
        if place:
            session.add(StoryBeat(day_index=day, winning_title=f"Канон {place}",
                                  winning_text="след остался", win_rule="majority",
                                  vote_counts="{}"))
    await session.commit()

    block = await rounds_mod.places_memory_block(session)
    assert block is not None
    assert "Мост из костей" in block and "Ярмарка Лайнеров" in block
    assert "след остался" in block
