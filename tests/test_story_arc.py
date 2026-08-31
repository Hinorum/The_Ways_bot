"""Арка месяца: акты, миссии дня, приметы Лая, офлайн-пулы и вплетение в лор."""

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
    """Границы актов: 0.0-0.27 Вход, 0.27-0.60 Поиск, 0.60-0.93 Кризис, 0.93-1.01 Финал."""
    total = 28
    assert arc_stage_index(1, total) == 0      # Вход: (0)/(27) = 0.0
    assert arc_stage_index(2, total) == 0      # Вход: (1)/(27) = 0.037
    assert arc_stage_index(3, total) == 0      # Вход: (2)/(27) = 0.074
    assert arc_stage_index(4, total) == 0      # Вход: (3)/(27) = 0.111
    assert arc_stage_index(9, total) == 1      # Поиск: (8)/(27) = 0.296
    assert arc_stage_index(14, total) == 1     # Поиск: (13)/(27) = 0.481
    assert arc_stage_index(21, total) == 2     # Кризис: (20)/(27) = 0.741
    assert arc_stage_index(27, total) == 3     # Финал: (26)/(27) = 0.963
    assert arc_stage_index(28, total) == 3     # Финал: (27)/(27) = 1.0
    # Двухдневная петля: последний день попадает в финал.
    assert arc_stage_index(2, 2) == 3


def test_block_carries_stable_tokens() -> None:
    block = arc_block(12, 28, run_key="2026-09")
    # День 12: 11/27 = 0.407 -> Поиск (акт 1)
    assert "ЭТАП=1" in block
    assert "Миссия дня:" in block
    details = arc_details_from_block(block)
    assert details["stage"] == 1
    assert details["mission"] in arc_stage(12, 28)["missions"]
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
    assert whisper_pool_for_stage(3), "Финал не остаётся пустым для вечера"


def test_block_shows_memory_line() -> None:
    block = arc_block(14, 28, run_key="2026-09")
    assert "Миссия дня:" in block
    memory = arc_block(1, 28, run_key="2026-10", previous_season_summary="прошлый месяц закрылся выбором дома")
    assert "прошлый месяц закрылся выбором дома" in memory
    assert arc_block(1, 1) == ""


def test_mission_flows_into_offline_chapter() -> None:
    """Офлайн-глава несёт миссию дня из арки (связный текст без сети)."""
    block = arc_block(12, 28, run_key="2026-09")
    mission = arc_details_from_block(block)["mission"]
    assert mission, "у акта должна быть миссия"
    chapter = compose_chapter(
        12, ["Костёр стаи: появился общий костёр"], season_block=block, salt="t"
    )
    assert mission in chapter["text"]
    plain = compose_chapter(12, ["Костёр стаи: появился общий костёр"], salt="t")
    assert mission not in plain["text"]


def test_offline_cards_wear_stage_titles() -> None:
    """Карты офлайн-дня надевают названия своего акта (лицо месяца)."""
    block = arc_block(12, 28, run_key="2026-09")  # акт 1 «Поиск»
    chapter = compose_chapter(
        12, ["Костёр стаи: появился общий костёр"], season_block=block, salt="arc-cards"
    )
    stage_titles: dict[str, list[str]] = {
        tag: list(arc_card_titles(1, tag)) for tag in ("risk", "care", "cunning")
    }
    for card in chapter["cards"]:
        assert card["title"] in stage_titles[card["tag"]], (
            f"карта {card['title']} не из пула акта 1"
        )


def test_card_title_pools_cover_all_stages_and_meanings() -> None:
    assert len(_ARC_CARD_TITLES) == 4
    for stage_idx, by_tag in _ARC_CARD_TITLES.items():
        for tag in ("risk", "care", "cunning"):
            assert len(by_tag.get(tag, ())) >= 2, f"акт {stage_idx} / {tag} пуст"
    titles = " ".join(t for by_tag in _ARC_CARD_TITLES.values() for t in by_tag.values() for t in t)
    for banned in ("вчера", "голосова", "итог"):
        assert banned not in titles.lower()


def test_secret_revealed_on_milestone_blocks() -> None:
    # Приметы и секреты на ступенях 1 (Поиск), 2 (Кризис), 3 (Финал)
    assert arc_secret(1) and arc_secret(2) and arc_secret(3)
    # День 14 ->	stage 1 (Поиск): примета №1 + секрет
    block_14 = arc_block(14, 28, run_key="2026-09")
    assert "ПРИМЕТА ЛАЯ №1:" in block_14
    assert "СЕКРЕТ АРКИ" in block_14
    # День 1 (Вход): без приметы, без секрета
    assert "СЕКРЕТ АРКИ" not in arc_block(1, 28, run_key="2026-09")


def test_mission_scenes_translated_for_all_active_stages() -> None:
    """Каждая миссия дня имеет англ. сцену для обложки (детерминированно)."""
    for total in (20, 28, 30):
        for day in range(1, total + 1):
            mission = mission_for(day, total, run_key="2026-09")
            if not mission:
                continue
            assert mission in _ARC_MISSION_SCENES, f"нет сцены: {mission}"
            assert mission_scene(mission), f"пустая сцена: {mission}"
    assert mission_scene(mission_for(12, 28, "2026-09")) == mission_scene(
        mission_for(12, 28, "2026-09")
    )
    assert mission_scene(None) == ""


def test_cover_prompt_wears_mission_scene() -> None:
    """Офлайн-обложка дня несёт визуал миссии арки (мини-связка арта)."""
    block = arc_block(12, 28, run_key="2026-09")
    chapter = compose_chapter(
        12, ["Костёр стаи: появился общий костёр"], season_block=block, salt="art"
    )
    scene = mission_scene(arc_details_from_block(block)["mission"])
    assert scene, "у акта 1 должна быть сцена"
    assert scene in chapter["cover_prompt"]
    plain = compose_chapter(
        12, ["Костёр стаи: появился общий костёр"], season_block=None, salt="art"
    )
    assert scene not in plain["cover_prompt"]
