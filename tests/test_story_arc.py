"""Арка месяца: этапы, миссии дня, приметы Лая, офлайн-пулы и вплетение в лор."""

from __future__ import annotations

from app.lore import compose_chapter
from app.story_arc import (
    _ARC_CARD_TITLES,
    _ARC_MISSION_SCENES,
    _HOWL_SIGNS,
    arc_block,
    arc_card_titles,
    arc_details_from_block,
    arc_secret,
    arc_stage,
    arc_stage_index,
    mission_for,
    mission_scene,
    sign_for,
    teaser_pool,
    whisper_pool,
    whisper_pool_for_stage,
)


def test_stage_buckets_cover_month() -> None:
    total = 28
    assert arc_stage_index(1, total) == 0      # приход
    assert arc_stage_index(2, total) == 0      # первые дни
    assert arc_stage_index(3, total) == 0      # граница (2/27 = 7.4%)
    assert arc_stage_index(4, total) == 1      # завязка
    assert arc_stage_index(14, total) == 3     # дилемма (середина)
    assert arc_stage_index(21, total) == 4     # подготовка (20/27 = 74%)
    assert arc_stage_index(23, total) == 5     # кульминация (22/27 = 81%)
    assert arc_stage_index(28, total) == 6     # финал
    # Двухдневная петля: всё сводится к последнему этапу.
    assert arc_stage_index(2, 2) == 6
    assert arc_stage_index(1, 1) == 6


def test_block_carries_stable_tokens() -> None:
    block = arc_block(12, 28, run_key="2026-09")
    assert "ЭТАП=3" in block
    assert "Миссия дня:" in block
    # Разбор кормит офлайн-сборку лора тем же текстом.
    details = arc_details_from_block(block)
    assert details["stage"] == 3
    assert details["mission"] in arc_stage(12, 28)["missions"]
    # Грабли не путают: без арки разбор пустой.
    assert arc_details_from_block(None) == {}
    assert arc_details_from_block("просто текст без арки") == {}


def test_missions_are_deterministic_and_in_pool() -> None:
    total = 28
    stage = arc_stage(9, total)
    assert mission_for(9, total, "2026-09") in stage["missions"]
    assert mission_for(9, total, "2026-09") == mission_for(9, total, "2026-09")


def test_whisper_and_teaser_pools_stage_bound() -> None:
    total = 28
    assert whisper_pool(1, total) == whisper_pool_for_stage(0)
    assert teaser_pool(21, total) == arc_stage(21, total)["teaser"]
    assert whisper_pool_for_stage(6), "финал не остаётся пустым для вечера"


def test_howl_signs_appear_on_milestone_stages_only() -> None:
    total = 28
    assert sign_for(1, total) is None        # приход
    assert sign_for(4, total) is not None    # завязка → примета №1
    assert sign_for(4, total)[0] == "№1"
    assert sign_for(14, total)[0] == "№2"    # дилемма
    assert sign_for(21, total) is None       # подготовка — без приметы
    assert sign_for(23, total)[0] == "№3"    # кульминация
    assert sign_for(28, total) is None       # финал
    # Текст приметы — из своего пула.
    assert sign_for(14, total)[1] in _HOWL_SIGNS[1]


def test_block_shows_sign_and_samsara_line() -> None:
    block = arc_block(14, 28, run_key="2026-09")
    assert "ПРИМЕТА ЛАЯ №2:" in block
    samsara = arc_block(1, 28, run_key="2026-10", previous_season_summary="прошлый месяц закрылся выбором дома")
    assert "сансара" in samsara.lower()
    assert "прошлый месяц закрылся выбором дома" in samsara
    assert arc_block(1, 1) == ""


def test_mission_flows_into_offline_chapter() -> None:
    """Офлайн-глава несёт миссию дня из арки (связный текст без сети)."""
    block = arc_block(12, 28, run_key="2026-09")
    mission = arc_details_from_block(block)["mission"]
    assert mission, "у этапа должна быть миссия"
    chapter = compose_chapter(
        12, ["Костёр стаи: появился общий костёр"], season_block=block, salt="t"
    )
    assert mission in chapter["text"]
    # Без арки текст строится без неё.
    plain = compose_chapter(12, ["Костёр стаи: появился общий костёр"], salt="t")
    assert mission not in plain["text"]


def test_offline_cards_wear_stage_titles() -> None:
    """Карты офлайн-дня надевают названия своего этапа (лицо месяца)."""
    block = arc_block(12, 28, run_key="2026-09")  # этап 3 «Дилемма»
    chapter = compose_chapter(
        12, ["Костёр стаи: появился общий костёр"], season_block=block, salt="arc-cards"
    )
    stage_titles: dict[str, list[str]] = {
        tag: list(arc_card_titles(3, tag)) for tag in ("risk", "care", "cunning")
    }
    for card in chapter["cards"]:
        assert card["title"] in stage_titles[card["tag"]], (
            f"карта {card['title']} не из пула этапа 3"
        )


def test_card_title_pools_cover_all_stages_and_meanings() -> None:
    assert len(_ARC_CARD_TITLES) == 7
    for stage_idx, by_tag in _ARC_CARD_TITLES.items():
        for tag in ("risk", "care", "cunning"):
            assert len(by_tag.get(tag, ())) >= 2, f"этап {stage_idx} / {tag} пуст"
    # Запрещённые для карты слова пока не просочились в названия.
    titles = " ".join(t for by_tag in _ARC_CARD_TITLES.values() for t in by_tag.values() for t in t)
    for banned in ("вчера", "голосова", "итог"):
        assert banned not in titles.lower()


def test_secret_revealed_on_milestone_blocks() -> None:
    assert arc_secret(1) and arc_secret(3)
    assert "СЕКРЕТ АРКИ" in arc_block(14, 28, run_key="2026-09")
    # На приходе / финале секрета нет.
    assert "СЕКРЕТ АРКИ" not in arc_block(1, 28, run_key="2026-09")
    assert "СЕКРЕТ АРКИ" not in arc_block(28, 28, run_key="2026-09")


def test_mission_scenes_translated_for_all_active_stages() -> None:
    """Каждая миссия дня имеет англ. сцену для обложки (детерминированно)."""
    for total in (20, 28, 30):
        for day in range(1, total + 1):
            mission = mission_for(day, total, run_key="2026-09")
            if not mission:  # этап «Финал» без миссий
                continue
            assert mission in _ARC_MISSION_SCENES, f"нет сцены: {mission}"
            assert mission_scene(mission), f"пустая сцена: {mission}"
    # Один и тот же день месяц к месяцу даёт ту же сцену.
    assert mission_scene(mission_for(12, 28, "2026-09")) == mission_scene(
        mission_for(12, 28, "2026-09")
    )
    assert mission_scene(None) == ""


def test_cover_prompt_wears_mission_scene() -> None:
    """Офлайн-обложка дня несёт визуал миссии арки (мини-связка арта)."""
    block = arc_block(12, 28, run_key="2026-09")  # этап 3, сцена из миссии
    chapter = compose_chapter(
        12, ["Костёр стаи: появился общий костёр"], season_block=block, salt="art"
    )
    scene = mission_scene(arc_details_from_block(block)["mission"])
    assert scene, "у этапа 3 должна быть сцена"
    assert scene in chapter["cover_prompt"]
    # Без арки — ровный промпт места без сцены.
    plain = compose_chapter(
        12, ["Костёр стаи: появился общий костёр"], season_block=None, salt="art"
    )
    assert scene not in plain["cover_prompt"]