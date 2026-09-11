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


def test_closed_month_loop_forces_dom_one(monkeypatch) -> None:
    """Замкнутый месячный цикл: якорь форсируется на 1-е число месяца, чтобы
    арка всегда была ровно один календарный месяц независимо от даты сброса."""
    from app.season import default_anchor

    monkeypatch.setattr(settings, "closed_month_loop", True)
    anchor = default_anchor(_utc(2026, 8, 24, 15, 0))
    assert anchor["dom"] == 1 and anchor["key"] == "2026-08"

    # Выключили — якорь снова от фактической даты сброса.
    monkeypatch.setattr(settings, "closed_month_loop", False)
    anchor2 = default_anchor(_utc(2026, 8, 24, 15, 0))
    assert anchor2["dom"] == 24 and anchor2["key"] == "2026-08"


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


# ── Волна 3: Исход из трёх дней, стена клятв, целостность стаи ──

from app.season import exodus_phase, exodus_instruction


def test_exodus_phase_layout() -> None:
    # За 2 дня до Лая — фаза 1 (выбор двери), за день — фаза 2 (кто несёт свет),
    # день Лая — фаза 3 (финал).
    assert exodus_phase(29, 31) == 1
    assert exodus_phase(30, 31) == 2
    assert exodus_phase(31, 31) == 3
    assert exodus_phase(28, 31) == 0
    assert exodus_phase(1, 31) == 0


def test_exodus_instruction_phases() -> None:
    door = exodus_instruction(1, {"care": 5}, vow_count=3)
    assert door is not None and "ВЫБОР ДВЕРИ" in door and "3" in door
    light = exodus_instruction(2, {"care": 5}, healed_memories=2)
    assert light is not None and "КТО НЕСЁТ СВЕТ" in light and "2/5" in light
    assert exodus_instruction(3, {"care": 5}) is None


def test_finale_instruction_mentions_vow_wall_and_wholeness() -> None:
    block = finale_instruction({"care": 5}, vow_count=4, healed_memories=3)
    assert "ДЕНЬ ПЕРВОГО ЛАЯ" in block
    assert "4" in block and "стеной" in block
    assert "3/5" in block


def test_finale_instruction_closes_the_count_mystery() -> None:
    # Развязка тайны: счёт короче потому, что пятой нечем назваться.
    block = finale_instruction({"care": 5})
    assert "на единицу короче" in block
    assert "нечем назваться" in block
    assert "имя или тишину" in block
    # Развязка нарративная: никаких вычитаний и платёжных слов.
    assert "вычитай" in block
    # Полный сезон бережёт тайну до финала: в обычные дни её нет.
    regular = finale_instruction({"care": 5})  # та же строчка всегда в финале —
    assert regular.count("на единицу короче") == 1


def test_season_block_injects_exodus_before_finale(monkeypatch) -> None:
    from app.config import settings
    monkeypatch.setattr(settings, "run_length_months", 1)
    ANCHOR = {"dom": 1, "key": "2026-08"}

    finale = season_block(anchor=ANCHOR, moment=_utc(2026, 8, 31, 11, 0), balance={"care": 3})
    assert "ДЕНЬ ПЕРВОГО ЛАЯ" in finale

    phase1 = season_block(anchor=ANCHOR, moment=_utc(2026, 8, 29, 11, 0))
    assert "ВЫБОР ДВЕРИ" in phase1

    phase2 = season_block(anchor=ANCHOR, moment=_utc(2026, 8, 30, 11, 0))
    assert "КТО НЕСЁТ СВЕТ" in phase2

    # Обычный день не содержит блоков Исхода.
    regular = season_block(anchor=ANCHOR, moment=_utc(2026, 8, 15, 11, 0))
    assert "ВЫБОР ДВЕРИ" not in regular and "КТО НЕСЁТ СВЕТ" not in regular


async def test_vow_wall_counts_unpicked_paths(session) -> None:
    from app.streaks import vow_wall_count

    for day, win, cards in (
        (1, "Тропа А", [("Тропа А", "care"), ("Тропа B", "risk"), ("Тропа C", "cunning")]),
        (2, "Тропа B", [("Тропа D", "care"), ("Тропа B", "risk")]),
    ):
        round_row = Round(
            day_index=day, status=RoundStatus.CLOSED, win_rule=WinRule.MAJORITY,
            rule_commitment="c", chapter_title="t", chapter_text="x", lore_summary="l",
            opens_at=_utc(2026, 9, day, 11, 0), voting_ends_at=_utc(2026, 9, day + 1, 10, 0),
            tally_ends_at=_utc(2026, 9, day + 1, 11, 0), winner_card=1, season="2026-09",
        )
        session.add(round_row)
        await session.flush()
        session.add(StoryBeat(day_index=day, winning_title=win, winning_text="x",
                              win_rule="majority", vote_counts="{}"))
        for pos, (title, tag) in enumerate(cards):
            session.add(Card(round_id=round_row.id, position=pos, title=title, tag=tag,
                             description="d", image_path="", consequence="c"))
    await session.commit()

    assert await vow_wall_count(session) == 3  # Тропа B, C (день 1) + Тропа D (день 2)


async def test_healed_memories_counts_accepted_layers(session) -> None:
    from app.dog_memories import healed_memories_count
    from app.models import DogMemory

    session.add(DogMemory(dog_key="баркод", kind="birth", summary="s", state="healed", created_day=0))
    session.add(DogMemory(dog_key="стежка", kind="birth", summary="s", state="recalled", created_day=0))
    session.add(DogMemory(dog_key="вектор", kind="birth", summary="s", state="suppressed", created_day=0))
    await session.commit()

    assert await healed_memories_count(session) == 1


# ── Волна 4: тайна мира — пересчёт, эвакуация, Еретик сезона 2+ ──

from app.season import recount_day


def test_recount_day_once_before_crisis(monkeypatch) -> None:
    # Один длинный забег: пересчёт на ~3/4 пути, но никогда в кризис/пролог.
    for run_day, total in (
        (23, 31),  # 3/4 от 31 = 23 — пересчёт
        (24, 31),
        (22, 31),
        (1, 31),
        (30, 31),
        (8, 31),
    ):
        mark = recount_day(run_day, total)
        assert isinstance(mark, bool)
    assert recount_day(23, 31) is True
    assert recount_day(24, 31) is False
    assert recount_day(30, 31) is False  # кризис
    assert recount_day(3, 31) is False   # пролог


def test_season_block_injects_recount_once_in_long_run(monkeypatch) -> None:
    from app.config import settings
    monkeypatch.setattr(settings, "run_length_months", 2)
    monkeypatch.setattr(settings, "first_season_months", 2)
    ANCHOR = {"dom": 1, "key": "2026-08"}

    # Двухмесячная арка: 24 авг → 23 окт = 61 день. Пересчёт ≈ 45.
    block45 = season_block(anchor=ANCHOR, moment=_utc(2026, 9, 14, 11, 0))  # run_day 45
    assert "ПЕРЕСЧЁТ" in block45
    block46 = season_block(anchor=ANCHOR, moment=_utc(2026, 9, 15, 11, 0))  # run_day 46
    assert "ПЕРЕСЧЁТ" not in block46


def test_heretic_prompt_block_season2_hint() -> None:
    from app.season import heretic_prompt_block

    s1 = heretic_prompt_block("2026-08", 2, run_day=10, season=1)
    s2 = heretic_prompt_block("2026-08", 2, run_day=10, season=2)
    assert "ПРАВИЛА ЕРЕТИКА" in s1 and "ПЕРЕСЧЁТ ЗНАЕТ ЕГО ИНАЧЕ" not in s1
    assert "ПЕРЕСЧЁТ ЗНАЕТ ЕГО ИНАЧЕ" in s2
    # Опциональная сигнатура остаётся совместимой.
    legacy = heretic_prompt_block("2026-08", 2, run_day=10)
    assert legacy is not None and "ПРАВИЛА ЕРЕТИКА" in legacy


def test_finale_season2_closes_heretic_line() -> None:
    s2 = finale_instruction({"care": 5}, season=2)
    assert "Еретик" in s2
    assert "быть в счёте" in s2
    # Сезон 1 — Еретика нет в финале.
    s1 = finale_instruction({"care": 5}, season=1)
    assert "Еретик" not in s1


def test_recount_day_with_vows_adds_prose(monkeypatch) -> None:
    from app.config import settings
    monkeypatch.setattr(settings, "run_length_months", 2)
    monkeypatch.setattr(settings, "first_season_months", 2)
    ANCHOR = {"dom": 1, "key": "2026-08"}
    # Пересчёт на 3/4 пути (run_day 45) + клятвы: про звук клятв от пересчёта.
    block45 = season_block(anchor=ANCHOR, moment=_utc(2026, 9, 14, 11, 0), vow_count=3)
    assert "ПЕРЕСЧЁТ" in block45
    assert "клятвы" in block45
    # Без клятв — только ПЕРЕСЧЁТ, без касания клятв.
    block45_no_vows = season_block(anchor=ANCHOR, moment=_utc(2026, 9, 14, 11, 0), vow_count=0)
    assert "клятвы" not in block45_no_vows
