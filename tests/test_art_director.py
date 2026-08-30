"""Арт-директор: план дня, сборка промптов и текст-фри фолбэки."""

from pathlib import Path

import pytest

from app.art_director import (
    _build_art_prompt,
    build_image_prompt,
    compact_anchor,
    plan_day_art,
    short_image_prompt,
)
from app.config import settings
from app.story import render_cover


CHAPTER = {
    "title": "День 3. Ржавые ворота",
    "text": "Стая вышла к ржавым воротам склада.",
    "cover_prompt": "wide shot, stray dogs before rusted gates",
    "cards": [
        {"title": "Взлом", "description": "вскрыть ворота", "tag": "risk", "image_prompt": "dogs prying a rusted gate"},
        {"title": "Обход", "description": "искать лаз", "tag": "cunning", "image_prompt": "dogs sneaking along a fence"},
        {"title": "Посты", "description": "ждать подмоги", "tag": "care", "image_prompt": "dogs keeping watch by a fire"},
    ],
}


@pytest.fixture()
def offline_llm(monkeypatch):
    monkeypatch.setattr(settings, "use_free_story_llm", False)


async def test_offline_bible_is_single_cover_frame(offline_llm) -> None:
    """Новый мир: один сетевой кадр в день. Библия — только cover."""
    bible = await plan_day_art(CHAPTER)
    assert set(bible["shots"]) == {"cover"}
    assert bible["shots"]["cover"]["scene"]
    assert bible["palette"] and bible["lighting"] and bible["motifs"]


async def test_llm_bible_is_used_when_valid(monkeypatch) -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"palette":"rust orange over teal","lighting":"low sun through fog",'
                        '"motifs":["rusted iron","drifting sparks"],'
                        '"shots":{'
                        '"cover":{"scene":"aerial view of the pack at the gates, gate torn open","composition":"sweeping aerial"}}}'
                    )
                }
            }
        ]
    }

    async def fake_chat(messages, timeout=40, **kwargs):
        return payload, "test-model"

    monkeypatch.setattr("app.art_director._chat_completion", fake_chat)
    bible = await plan_day_art(CHAPTER, ["вчера стая взломала ворота"])
    assert bible["palette"] == "rust orange over teal"
    assert "gate" in bible["shots"]["cover"]["scene"]


async def test_broken_llm_answer_falls_back_offline(monkeypatch) -> None:
    async def fake_chat(messages, timeout=40, **kwargs):
        return {"choices": [{"message": {"content": "не JSON вообще"}}]}, "m"

    monkeypatch.setattr(settings, "use_free_story_llm", True)
    monkeypatch.setattr("app.art_director._chat_completion", fake_chat)
    bible = await plan_day_art(CHAPTER)
    assert set(bible["shots"]) == {"cover"}


async def test_build_prompt_contains_negatives_and_varies(offline_llm) -> None:
    bible = await plan_day_art(CHAPTER)
    p0 = build_image_prompt(bible, "cover", seed=11)
    assert "no text" in p0 and "no poster layout" in p0
    # Антидрейф стиля: free-модели уползают в фотореализм/аниме без запретов.
    for guard in ("photorealistic", "3D render", "anime screencap", "human hands"):
        assert guard in p0
    assert "rust" in p0 or "teal" in p0 or "ember" in p0
    # Вариативность от сида: другой день — другая киношная фактура.
    seeds = {build_image_prompt(bible, "cover", seed=s) for s in range(4)}
    assert len(seeds) == 4


def test_art_system_prompt_carries_color_semantics() -> None:
    """Палитра через сюжет: у каждого цвета мира — один хозяин."""
    from app.art_director import ART_SYSTEM_PROMPT

    for mark in (
        "красное свечение",
        "апострофа",
        "биолюминесцентная бирюза",
        "тёплое золото",
        "пыльно-серый",
    ):
        assert mark in ART_SYSTEM_PROMPT, f"нет семантики: {mark}"


async def test_offline_motifs_include_campfire_circle(offline_llm) -> None:
    """Эмоциональное ядро серии — круг света стаи среди тёмных мотивов."""
    bible = await plan_day_art(CHAPTER)
    assert any("campfire light" in motif for motif in bible["motifs"])


async def test_short_prompt_is_compact(offline_llm) -> None:
    bible = await plan_day_art(CHAPTER)
    short = short_image_prompt(bible, "cover", seed=5)
    # Сжатый, но с полным антидрейф-хвостом: сцена ~28 слов + запреты.
    assert len(short.split()) < 85


def test_abstract_fallbacks_have_no_text_layer(tmp_path: Path) -> None:
    cover = tmp_path / "day9_cover.jpg"
    render_cover(cover, "День 9. Тихий порт")
    assert cover.exists() and cover.stat().st_size > 1000


async def test_anchor_continues_palette_offline(offline_llm) -> None:
    """Офлайн-палитра продолжается с якоря предыдущего дня, а не с нуля."""
    from app.art_director import _PALETTE_ROTATION

    anchor_palette = _PALETTE_ROTATION[1][0]
    bible = await plan_day_art(CHAPTER, anchor={"palette": anchor_palette})
    assert bible["palette"] == _PALETTE_ROTATION[2][0]


def test_prompt_carries_anchor_and_yesterday_core_for_llm() -> None:
    prompt = _build_art_prompt(
        CHAPTER,
        ["вчера стая взломала ржавые ворота"],
        anchor={"palette": "cold slate", "lighting": "moonlit rim", "motifs": ["iron ring"]},
    )
    assert "ПРЕДЫДУЩИЙ ДЕНЬ" in prompt and "cold slate" in prompt
    assert "локацию" in prompt  # требование сменить декорации сохранено
    # Новая драматургия: последствие вчерашнего канона — ядро кадра.
    assert "ЯДРО КАДРА" in prompt and "взломала ржавые ворота" in prompt
    # Кадр один: JSON-схема без слотов карт.
    assert '"0"' not in prompt and "ПУТЬ 0" not in prompt


async def test_intro_prompt_carries_world_and_heretic(offline_llm) -> None:
    """Стартовый кадр мира: палитра дня + сцена знакомства с Еретиком."""
    from app.art_director import build_intro_prompt, build_intro_short_prompt

    bible = await plan_day_art(CHAPTER)
    intro = build_intro_prompt(bible, seed=7)
    short = build_intro_short_prompt(bible)
    assert "portal rings" in intro and "old stitched maps" in intro
    for mark in ("no text", "flat 2D"):
        assert mark in intro
    assert len(short.split()) < 95  # сжатый, но сцена мира насыщенная


def test_compact_anchor_fits_state_limit() -> None:
    bible = {
        "palette": "x" * 300,
        "lighting": "y" * 300,
        "motifs": ["m" * 100, "n" * 100, "o" * 100],
    }
    import json

    blob = json.dumps(compact_anchor(bible), ensure_ascii=False)
    assert len(blob) <= 255


def test_character_motifs_detected_from_chapter_text() -> None:
    from app.art_director import character_motifs_for

    text = "Лайнер отсчитывает сдачу, а Архивариус шепчет над папками. Хозяин Ошибки молчит."
    motifs = character_motifs_for(text)
    assert len(motifs) == 3
    assert all("Liner" in m or "Archivist" in m or "Error Master" in m for m in motifs)
    assert character_motifs_for("Стая идёт через пустой город") == []
    # Детерминированность: тот же текст — тот же набор.
    assert motifs == character_motifs_for(text.lower())


def test_place_seed_is_stable_per_place_and_none_when_empty() -> None:
    from app.rounds import place_seed_for

    first = place_seed_for("Старый приют")
    assert first is not None
    assert first == place_seed_for("старый приют ")  # регистр и пробелы не важны
    assert place_seed_for(None) is None
    assert place_seed_for("") is None
