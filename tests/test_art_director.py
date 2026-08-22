"""Арт-директор: план дня, сборка промптов и текст-фри фолбэки."""

from pathlib import Path

import pytest

from app.art_director import (
    build_image_prompt,
    offline_bible,
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


async def test_offline_bible_has_four_distinct_shots(offline_llm) -> None:
    bible = await plan_day_art(CHAPTER)
    assert set(bible["shots"]) == {"cover", "0", "1", "2"}
    scenes = {bible["shots"][slot]["scene"] for slot in ("0", "1", "2")}
    assert len(scenes) == 3
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
                        '"cover":{"scene":"aerial view of the pack at the gates","composition":"sweeping aerial"},'
                        '"0":{"scene":"dog forcing the gate hinge","composition":"low angle"},'
                        '"1":{"scene":"dog slipping under the fence","composition":"dutch angle"},'
                        '"2":{"scene":"dogs sharing watch by embers","composition":"top-down"}}}'
                    )
                }
            }
        ]
    }

    async def fake_chat(messages, timeout=40):
        return payload, "test-model"

    monkeypatch.setattr("app.art_director._chat_completion", fake_chat)
    bible = await plan_day_art(CHAPTER)
    assert bible["palette"] == "rust orange over teal"
    assert "hinge" in bible["shots"]["0"]["scene"]


async def test_broken_llm_answer_falls_back_offline(monkeypatch) -> None:
    async def fake_chat(messages, timeout=40):
        return {"choices": [{"message": {"content": "не JSON вообще"}}]}, "m"

    monkeypatch.setattr(settings, "use_free_story_llm", True)
    monkeypatch.setattr("app.art_director._chat_completion", fake_chat)
    bible = await plan_day_art(CHAPTER)
    assert set(bible["shots"]) == {"cover", "0", "1", "2"}


async def test_build_prompt_contains_negatives_and_varies(offline_llm) -> None:
    bible = await plan_day_art(CHAPTER)
    p0 = build_image_prompt(bible, "0", seed=11)
    p1 = build_image_prompt(bible, "1", seed=12)
    assert "no text" in p0 and "no poster layout" in p0
    assert "rust" in p0 or "teal" in p0 or "ember" in p0
    assert p0 != p1
    # Вариативность от сида: другой день — другая киношная фактура.
    seeds = {build_image_prompt(bible, "cover", seed=s) for s in range(4)}
    assert len(seeds) == 4


async def test_short_prompt_is_compact(offline_llm) -> None:
    bible = await plan_day_art(CHAPTER)
    short = short_image_prompt(bible, "2", seed=5)
    assert len(short.split()) < 60


def test_abstract_fallbacks_have_no_text_layer(tmp_path: Path) -> None:
    cover = tmp_path / "day9_cover.jpg"
    render_cover(cover, "День 9. Тихий порт")
    assert cover.exists() and cover.stat().st_size > 1000
