"""Tests for AI World Engine — генерация мира, выборов и последствий."""

from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import (
    Base,
    WorldCharacter,
    WorldChoice,
    WorldEvent,
    WorldLocation,
    WorldSnapshot,
)
from app.world_engine import (
    AIChoice,
    WorldContext,
    _build_world_prompt,
    _fallback_choices,
    _parse_ai_choices,
    create_world_snapshot,
    generate_ai_choices,
    get_world_context,
    record_choice,
    record_world_event,
)


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def sample_ai_response():
    """Пример ответа AI с выборами."""
    return {
        "choices": [
            {
                "title": "Тёмный коридор",
                "description": "Коридор уходит вглубь. Стены холодные, но ровные.",
                "consequence": "Можно идти вперёд — возможно, найдём что-то полезное.",
                "tag": "risk",
                "characters_involved": ["Лайнер"],
                "location": "Старый приют",
            },
            {
                "title": "Целительный мох",
                "description": "На стене растёт блестящий мох. Он выглядит как лекарство.",
                "consequence": "Мох светится зелёным. Если съесть — может помочь.",
                "tag": "care",
                "characters_involved": [],
                "location": None,
            },
            {
                "title": "Обмануть стражу",
                "description": "Страж стоит у двери. Можно отвлечь его шумом.",
                "consequence": "Если получится — пройдём. Если нет — будет хуже.",
                "tag": "cunning",
                "characters_involved": ["Архивариус"],
                "location": "Ворота",
            },
        ]
    }


@pytest.fixture
def sample_world_context():
    """Пример контекста мира."""
    return WorldContext(
        day_index=5,
        recent_choices=[
            {"day": 4, "text": "Идти вперёд", "tag": "risk", "won": True, "consequences": "[]"},
            {"day": 3, "text": "Отдохнуть", "tag": "care", "won": False, "consequences": "[]"},
        ],
        active_locations=[
            {"name": "Старый приют", "description": "Развалины здания", "atmosphere": "тихо", "times_visited": 2},
        ],
        active_characters=[
            {"name": "Лайнер", "role": "npc", "personality": "Торговец", "mood": "neutral", "trust_stay": 5},
        ],
        world_mood="tense",
        open_threads=["Поиск еды"],
        pack_needs={"hunger": 7, "thirst": 5, "health": 8},
        season="unknown",
    )


# ── Tests: AIChoice ────────────────────────────────────────────────────────


def test_ai_choice_creation():
    """AIChoice создаётся с правильными полями."""
    choice = AIChoice(
        title="Тестовый выбор",
        description="Описание",
        consequence="Последствие",
        tag="risk",
        characters_involved=["Лайнер"],
        location="Место",
    )
    assert choice.title == "Тестовый выбор"
    assert choice.tag == "risk"
    assert choice.characters_involved == ["Лайнер"]
    assert choice.location == "Место"


def test_ai_choice_defaults():
    """AIChoice имеет дефолтные значения."""
    choice = AIChoice(
        title="Тест",
        description="Описание",
        consequence="Последствие",
        tag="care",
        characters_involved=[],
    )
    assert choice.location is None


# ── Tests: _parse_ai_choices ───────────────────────────────────────────────


def test_parse_ai_choices_valid(sample_ai_response):
    """Парсинг валидного ответа AI."""
    text = json.dumps(sample_ai_response)
    choices = _parse_ai_choices(text)
    assert len(choices) == 3
    assert choices[0].title == "Тёмный коридор"
    assert choices[0].tag == "risk"
    assert choices[1].tag == "care"
    assert choices[2].tag == "cunning"


def test_parse_ai_choices_no_json():
    """Парсинг ответа без JSON."""
    choices = _parse_ai_choices("Просто текст без JSON")
    assert choices == []


def test_parse_ai_choices_invalid_json():
    """Парсинг невалидного JSON."""
    choices = _parse_ai_choices("{invalid json}")
    assert choices == []


def test_parse_ai_choices_missing_fields():
    """Парсинг ответа с неполными данными."""
    response = {
        "choices": [
            {"title": "Тест", "description": "Описание"},  # Нет consequence и tag
            {"title": "Тест2", "description": "Описание2", "consequence": "Последствие", "tag": "risk"},
        ]
    }
    text = json.dumps(response)
    choices = _parse_ai_choices(text)
    assert len(choices) == 1  # Только второй проходит валидацию
    assert choices[0].tag == "risk"


def test_parse_ai_choices_invalid_tag():
    """Парсинг ответа с невалидным тегом."""
    response = {
        "choices": [
            {"title": "Тест", "description": "Описание", "consequence": "Последствие", "tag": "invalid"},
        ]
    }
    text = json.dumps(response)
    choices = _parse_ai_choices(text)
    assert len(choices) == 1
    assert choices[0].tag == "custom"  # Конвертируется в custom


def test_parse_ai_choices_truncation():
    """Парсинг ответа с длинными строками."""
    response = {
        "choices": [
            {
                "title": "A" * 200,  # Длиннее 120
                "description": "B" * 600,  # Длиннее 500
                "consequence": "C" * 600,
                "tag": "risk",
            },
        ]
    }
    text = json.dumps(response)
    choices = _parse_ai_choices(text)
    assert len(choices) == 1
    assert len(choices[0].title) <= 120
    assert len(choices[0].description) <= 500
    assert len(choices[0].consequence) <= 500


# ── Tests: _fallback_choices ───────────────────────────────────────────────


def test_fallback_choices_high_hunger():
    """Фолбэк-выборы при высоком голоде."""
    ctx = WorldContext(
        day_index=1,
        recent_choices=[],
        active_locations=[],
        active_characters=[],
        world_mood="tense",
        open_threads=[],
        pack_needs={"hunger": 8, "thirst": 5, "health": 10},
        season="unknown",
    )
    choices = _fallback_choices(ctx)
    assert len(choices) == 3
    assert any("Голодный" in c.title for c in choices)


def test_fallback_choices_low_health():
    """Фолбэк-выборы при низком здоровье."""
    ctx = WorldContext(
        day_index=1,
        recent_choices=[],
        active_locations=[],
        active_characters=[],
        world_mood="tense",
        open_threads=[],
        pack_needs={"hunger": 5, "thirst": 5, "health": 3},
        season="unknown",
    )
    choices = _fallback_choices(ctx)
    assert len(choices) == 3
    assert any("Целительный" in c.title for c in choices)


def test_fallback_choices_always_3():
    """Фолбэк всегда возвращает 3 выбора."""
    ctx = WorldContext(
        day_index=1,
        recent_choices=[],
        active_locations=[],
        active_characters=[],
        world_mood="tense",
        open_threads=[],
        pack_needs={"hunger": 5, "thirst": 5, "health": 10},
        season="unknown",
    )
    choices = _fallback_choices(ctx)
    assert len(choices) == 3


# ── Tests: _build_world_prompt ─────────────────────────────────────────────


def test_build_world_prompt_basic():
    """Базовый промпт содержит правила."""
    ctx = WorldContext(
        day_index=1,
        recent_choices=[],
        active_locations=[],
        active_characters=[],
        world_mood="tense",
        open_threads=[],
        pack_needs={"hunger": 5, "thirst": 5, "health": 10},
        season="unknown",
    )
    prompt = _build_world_prompt(ctx)
    assert "Ведущий" in prompt
    assert "3 ВЫБОРА" in prompt
    assert "JSON" in prompt


def test_build_world_prompt_with_locations():
    """Промпт содержит локации."""
    ctx = WorldContext(
        day_index=1,
        recent_choices=[],
        active_locations=[
            {"name": "Старый приют", "description": "Развалины", "atmosphere": "тихо", "times_visited": 2},
        ],
        active_characters=[],
        world_mood="tense",
        open_threads=[],
        pack_needs={"hunger": 5, "thirst": 5, "health": 10},
        season="unknown",
    )
    prompt = _build_world_prompt(ctx)
    assert "ЛОКАЦИИ:" in prompt
    assert "Старый приют" in prompt


def test_build_world_prompt_with_characters():
    """Промпт содержит персонажей."""
    ctx = WorldContext(
        day_index=1,
        recent_choices=[],
        active_locations=[],
        active_characters=[
            {"name": "Лайнер", "role": "npc", "personality": "Торговец", "mood": "neutral", "trust_stay": 5},
        ],
        world_mood="tense",
        open_threads=[],
        pack_needs={"hunger": 5, "thirst": 5, "health": 10},
        season="unknown",
    )
    prompt = _build_world_prompt(ctx)
    assert "ПЕРСОНАЖИ:" in prompt
    assert "Лайнер" in prompt


def test_build_world_prompt_with_recent_choices():
    """Промпт содержит последние выборы."""
    ctx = WorldContext(
        day_index=5,
        recent_choices=[
            {"day": 4, "text": "Идти вперёд", "tag": "risk", "won": True, "consequences": "[]"},
        ],
        active_locations=[],
        active_characters=[],
        world_mood="tense",
        open_threads=[],
        pack_needs={"hunger": 5, "thirst": 5, "health": 10},
        season="unknown",
    )
    prompt = _build_world_prompt(ctx)
    assert "ПОСЛЕДНИЕ ВЫБОРЫ СТАИ:" in prompt
    assert "Идти вперёд" in prompt


def test_build_world_prompt_with_needs():
    """Промпт содержит потребности стаи."""
    ctx = WorldContext(
        day_index=1,
        recent_choices=[],
        active_locations=[],
        active_characters=[],
        world_mood="tense",
        open_threads=[],
        pack_needs={"hunger": 8, "thirst": 3, "health": 7},
        season="unknown",
    )
    prompt = _build_world_prompt(ctx)
    assert "голод=8" in prompt
    assert "жажда=3" in prompt
    assert "здоровье=7" in prompt


# ── Tests: DB Operations (async) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_choice():
    """Запись выбора в БД."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with maker() as session:
        choice = AIChoice(
            title="Тестовый выбор",
            description="Описание",
            consequence="Последствие",
            tag="risk",
            characters_involved=["Лайнер"],
            location="Место",
        )
        world_choice = await record_choice(session, day_index=1, choice=choice, votes_count=5, won=True)
        await session.commit()

        assert world_choice.day_index == 1
        assert world_choice.choice_text == "Описание"
        assert world_choice.choice_tag == "risk"
        assert world_choice.votes_count == 5
        assert world_choice.won is True
        assert "Лайнер" in world_choice.characters_involved
        assert world_choice.location == "Место"

    await engine.dispose()


@pytest.mark.asyncio
async def test_record_world_event():
    """Запись события мира в БД."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with maker() as session:
        event = await record_world_event(
            session,
            day_index=1,
            event_type="appearance",
            description="Появился новый NPC",
            characters_involved=["НовыйNPC"],
            locations_involved=["Старый приют"],
            impact="Отношения изменились",
        )
        await session.commit()

        assert event.day_index == 1
        assert event.event_type == "appearance"
        assert "НовыйNPC" in event.characters_involved
        assert "Старый приют" in event.locations_involved

    await engine.dispose()


@pytest.mark.asyncio
async def test_get_world_context_empty():
    """Получение контекста мира из пустой БД."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with maker() as session:
        ctx = await get_world_context(session, day_index=1)
        assert ctx.day_index == 1
        assert ctx.recent_choices == []
        assert ctx.active_locations == []
        assert ctx.active_characters == []
        assert ctx.world_mood == "tense"
        assert ctx.open_threads == []

    await engine.dispose()


@pytest.mark.asyncio
async def test_get_world_context_with_data():
    """Получение контекста мира с данными."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with maker() as session:
        # Добавляем локацию
        loc = WorldLocation(
            name="Тестовая локация",
            description="Описание",
            atmosphere="атмосфера",
            created_day=1,
        )
        session.add(loc)

        # Добавляем персонажа
        char = WorldCharacter(
            name="ТестовыйNPC",
            role="npc",
            personality="Характер",
            created_day=1,
        )
        session.add(char)

        # Добавляем выбор
        choice = WorldChoice(
            day_index=1,
            choice_text="Тестовый выбор",
            choice_tag="risk",
        )
        session.add(choice)

        # Добавляем снимок
        snapshot = WorldSnapshot(
            day_index=0,
            mood="hopeful",
            summary="Вчерашний день",
        )
        session.add(snapshot)

        await session.commit()

        ctx = await get_world_context(session, day_index=2)
        assert ctx.day_index == 2
        assert len(ctx.recent_choices) == 1
        assert len(ctx.active_locations) == 1
        assert len(ctx.active_characters) == 1
        assert ctx.world_mood == "hopeful"

    await engine.dispose()


@pytest.mark.asyncio
async def test_create_world_snapshot():
    """Создание снимка мира."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def mock_llm_caller(messages, temperature=0.8, max_tokens=500, want_json=False):
        return {
            "mood": "chaotic",
            "summary": "День был полон событий",
            "open_threads": ["Незавершённый сюжет"],
            "world_trend": "Мир меняется",
        }

    async with maker() as session:
        snapshot = await create_world_snapshot(session, day_index=1, llm_caller=mock_llm_caller)
        await session.commit()

        assert snapshot.day_index == 1
        assert snapshot.mood == "chaotic"
        assert snapshot.summary == "День был полон событий"
        assert "Незавершённый сюжет" in snapshot.open_threads

    await engine.dispose()


@pytest.mark.asyncio
async def test_generate_ai_choices_with_llm():
    """Генерация AI-выборов с mock LLM."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def mock_llm_caller(messages, temperature=0.9, max_tokens=1500, want_json=True):
        return {
            "choices": [
                {
                    "title": "AI Выбор 1",
                    "description": "Описание от AI",
                    "consequence": "Последствие",
                    "tag": "risk",
                    "characters_involved": [],
                    "location": None,
                },
                {
                    "title": "AI Выбор 2",
                    "description": "Описание от AI 2",
                    "consequence": "Последствие 2",
                    "tag": "care",
                    "characters_involved": [],
                    "location": None,
                },
                {
                    "title": "AI Выбор 3",
                    "description": "Описание от AI 3",
                    "consequence": "Последствие 3",
                    "tag": "cunning",
                    "characters_involved": [],
                    "location": None,
                },
            ]
        }

    ctx = WorldContext(
        day_index=1,
        recent_choices=[],
        active_locations=[],
        active_characters=[],
        world_mood="tense",
        open_threads=[],
        pack_needs={"hunger": 5, "thirst": 5, "health": 10},
        season="unknown",
    )

    async with maker() as session:
        choices = await generate_ai_choices(session, ctx, mock_llm_caller)
        assert len(choices) == 3
        assert choices[0].title == "AI Выбор 1"
        assert choices[0].tag == "risk"

    await engine.dispose()


@pytest.mark.asyncio
async def test_generate_ai_choices_fallback():
    """Генерация AI-выборов с fallback при ошибке LLM."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def failing_llm_caller(messages, temperature=0.9, max_tokens=1500, want_json=True):
        raise Exception("LLM недоступен")

    ctx = WorldContext(
        day_index=1,
        recent_choices=[],
        active_locations=[],
        active_characters=[],
        world_mood="tense",
        open_threads=[],
        pack_needs={"hunger": 5, "thirst": 5, "health": 10},
        season="unknown",
    )

    async with maker() as session:
        choices = await generate_ai_choices(session, ctx, failing_llm_caller)
        assert len(choices) == 3  # Фолбэк возвращает 3 выбора

    await engine.dispose()
