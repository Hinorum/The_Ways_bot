"""Слой 5 — мега-промпт: память живого мира заходит в единый вызов главы.

Контракты:
- world_block по умолчанию None — промпты побайтово не меняются;
- переданный блок появляется в промпте после блока карт;
- get_or_create_location / generate_ai_location покидают денежный конвейер
  rounds (мир слышит день ДО генерации, а не патчит текст задним числом);
- create_world_snapshot вызывается с llm_caller (снимок больше не падает
  на TypeError и не умирает молча);
- _generate_session_characters получил недостающие импорты (select /
  WorldCharacter) — генерация NPC не спотыкается о NameError.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import settings
from app.card_payload import _world_block_text
from app.models import WinRule
from app.story import (
    _build_story_prompt,
    _free_story_llm,
    generate_chapter,
)

ROUNDS_SRC = Path(__file__).resolve().parents[1] / "app" / "rounds.py"
STORY_SRC = Path(__file__).resolve().parents[1] / "app" / "story.py"

CHAPTER_PAYLOAD = {
    "title": "День 9. Тихий порт",
    "place": "Тихий порт",
    "text": "а" * 1500,
    "lore_summary": "л",
    "cover_prompt": "cover",
    "cards": [
        {"title": f"t{i}", "description": "d", "consequence": "c", "tag": tag}
        for i, tag in enumerate(("risk", "care", "cunning"))
    ],
}


def _as_payload() -> dict:
    return {"choices": [{"message": {"content": json.dumps(CHAPTER_PAYLOAD, ensure_ascii=False)}}]}


# ── Гейт world_block в промпте ─────────────────────────────────────────────


def test_prompt_world_block_absent_by_default() -> None:
    prompt = _build_story_prompt(3, ["вчера стая взломала ржавые ворота"], win_rule=WinRule.MAJORITY)
    assert "ПАМЯТЬ МИРА" not in prompt


def test_prompt_world_block_present_when_passed() -> None:
    prompt = _build_story_prompt(
        3,
        ["вчера"],
        win_rule=WinRule.MAJORITY,
        with_choices=True,
        world_block="- настроение лабиринта: grim\n- известные места: Старый приют (2 посещ.)",
    )
    assert "ПАМЯТЬ МИРА" in prompt
    assert "настроение лабиринта: grim" in prompt
    assert "Старый приют (2 посещ.)" in prompt


def test_prompt_world_block_comes_after_cards_block() -> None:
    prompt = _build_story_prompt(3, ["вчера"], with_choices=True, world_block="wb-marker")
    assert "В ЭТОМ ЖЕ ответе сгенерируй массив \"cards\"" in prompt
    assert prompt.index("ПАМЯТЬ МИРА") > prompt.index('сгенерируй массив "cards"')


def test_prompt_world_block_ignored_when_none_dataset() -> None:
    base = _build_story_prompt(3, ["вчера"], win_rule=WinRule.MAJORITY, with_choices=True)
    same = _build_story_prompt(3, ["вчера"], win_rule=WinRule.MAJORITY, with_choices=True, world_block=None)
    assert base == same


# ── Пересылка world_block по генераторам ──────────────────────────────────


async def test_free_story_llm_puts_world_block_into_prompt(monkeypatch) -> None:
    seen: dict = {}

    async def fake_chat(messages, timeout=40, **kwargs):
        seen["user_content"] = messages[1]["content"]
        return _as_payload(), "layer5-model"

    monkeypatch.setattr("app.story._chat_completion", fake_chat)
    result = await _free_story_llm(
        9,
        ["вчера"],
        win_rule=None,
        world_block="- незакрытые сюжетные линии: Мост сгорел",
    )
    assert result is not None
    assert "ПАМЯТЬ МИРА" in seen["user_content"]
    assert "незакрытые сюжетные линии: Мост сгорел" in seen["user_content"]


async def test_generate_chapter_forwards_world_block(monkeypatch) -> None:
    monkeypatch.setattr(settings, "use_free_story_llm", True)
    calls: list[dict] = []

    async def fake_free_llm(*args, **kwargs):
        calls.append(kwargs)
        return {"title": "Т", "text": "т" * 900, "lore_summary": "л", "place": "П"}

    monkeypatch.setattr("app.story._free_story_llm", fake_free_llm)
    await generate_chapter(3, ["вчера"], win_rule=None, world_block="wb-x")
    assert calls
    assert calls[0]["world_block"] == "wb-x"


# ── _world_block_text ──────────────────────────────────────────────────────


def test_world_block_text_maps_world_context() -> None:
    ctx = type(
        "WorldCtx",
        (),
        {
            "world_mood": "grim",
            "open_threads": ["Мост сгорел", "Пропавший щенок"],
            "active_locations": [
                {"name": "Старый приют", "times_visited": 2},
                {"name": "Тихий порт", "times_visited": 0},
            ],
            "active_characters": [
                {"name": "Лайнер", "trust_stay": 7},
                {"name": "Дневник", "trust_stay": 4},
            ],
        },
    )()
    text = _world_block_text(ctx)
    assert text is not None
    assert "настроение лабиринта: grim" in text
    assert "незакрытые сюжетные линии: Мост сгорел; Пропавший щенок" in text
    assert "Старый приют (2 посещ.)" in text
    assert "Лайнер" in text
    assert "Дневник" in text
    assert "доверие" not in text


def test_world_block_text_empty_world_is_none() -> None:
    assert _world_block_text(None) is None


# ── Source-контракты конвейера ─────────────────────────────────────────────


def test_rounds_single_voice_no_separate_location_call() -> None:
    src = ROUNDS_SRC.read_text(encoding="utf-8")
    assert "world_block=world_block," in src
    assert "get_or_create_location" not in src
    assert "generate_ai_location" not in src


def test_rounds_snapshot_call_receives_llm_caller() -> None:
    src = ROUNDS_SRC.read_text(encoding="utf-8")
    assert "create_world_snapshot(session, round_row.day_index, llm_caller=_chat_completion)" in src


def test_story_session_characters_imports_restored() -> None:
    # Импорты подняты на уровень модуля (п.5 гигиены импортов): функция не
    # спотыкается о NameError ни на select, ни на WorldCharacter, ни на
    # generate_ai_character — все имена резолвятся из шапки story.py.
    src = STORY_SRC.read_text(encoding="utf-8")
    header, _ = src.split("async def _generate_session_characters", 1)
    assert "from sqlalchemy import select" in src
    assert "from app.models import RULE_PHRASES, WorldCharacter" in header
    assert "from app.world_engine import WorldContext, generate_ai_character" in header
    body = src.split("async def _generate_session_characters", 1)[1]
    end = body.find("\ndef ")
    body = body[:end] if end != -1 else body
    assert "generate_ai_character(session, ctx, _chat_completion)" in body