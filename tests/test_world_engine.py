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
    AICharacter,
    AILocation,
    WorldContext,
    _build_character_prompt,
    _build_location_prompt,
    _build_world_prompt,
    _fallback_choices,
    _parse_ai_character,
    _parse_ai_choices,
    _parse_ai_location,
    create_world_snapshot,
    generate_ai_character,
    generate_ai_choices,
    generate_ai_location,
    get_or_create_character,
    get_or_create_location,
    get_world_context,
    record_choice,
    record_world_event,
    update_character_state,
    update_location_visit,
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


# ── Tests: AI Location Generation ──────────────────────────────────────────


def test_ai_location_creation():
    """AILocation создаётся с правильными полями."""
    loc = AILocation(
        name="Тестовая локация",
        description="Описание",
        atmosphere="Атмосфера",
        dangers="Опасности",
        resources="Ресурсы",
        scene="image prompt",
    )
    assert loc.name == "Тестовая локация"
    assert loc.atmosphere == "Атмосфера"
    assert loc.scene == "image prompt"


def test_parse_ai_location_valid():
    """Парсинг валидного ответа AI с локацией."""
    response = '{"name": "Тёмный Грот", "description": "Пещера с сталактитами", "atmosphere": "Холодно и сыро", "dangers": "Обвалы", "resources": "Вода", "scene": "dark cave with stalactites"}'
    loc = _parse_ai_location(response)
    assert loc is not None
    assert loc.name == "Тёмный Грот"
    assert loc.scene == "dark cave with stalactites"


def test_parse_ai_location_no_json():
    """Парсинг ответа без JSON."""
    loc = _parse_ai_location("Просто текст")
    assert loc is None


def test_parse_ai_location_missing_fields():
    """Парсинг ответа с неполными данными."""
    response = '{"name": "Тест"}'  # Нет description и atmosphere
    loc = _parse_ai_location(response)
    assert loc is None


def test_parse_ai_location_truncation():
    """Парсинг ответа с длинными строками."""
    response = json.dumps({
        "name": "A" * 200,
        "description": "B" * 600,
        "atmosphere": "C" * 400,
        "dangers": "D" * 300,
        "resources": "E" * 300,
        "scene": "F" * 300,
    })
    loc = _parse_ai_location(response)
    assert loc is not None
    assert len(loc.name) <= 120
    assert len(loc.description) <= 500
    assert len(loc.atmosphere) <= 300
    assert len(loc.scene) <= 200


def test_build_location_prompt_basic():
    """Базовый промпт для генерации локации."""
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
    prompt = _build_location_prompt(ctx)
    assert "НОВУЮ локацию" in prompt
    assert "JSON" in prompt
    assert "name" in prompt


def test_build_location_prompt_with_existing():
    """Промпт содержит существующие локации (не повторять)."""
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
    prompt = _build_location_prompt(ctx)
    assert "СУЩЕСТВУЮЩИЕ ЛОКАЦИИ" in prompt
    assert "Старый приют" in prompt


def test_build_location_prompt_with_mood():
    """Промпт содержит настроение мира."""
    ctx = WorldContext(
        day_index=1,
        recent_choices=[],
        active_locations=[],
        active_characters=[],
        world_mood="chaotic",
        open_threads=[],
        pack_needs={"hunger": 5, "thirst": 5, "health": 10},
        season="unknown",
    )
    prompt = _build_location_prompt(ctx)
    assert "хаотичная атмосфера" in prompt


@pytest.mark.asyncio
async def test_generate_ai_location_with_llm():
    """Генерация AI-локации с mock LLM."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def mock_llm_caller(messages, temperature=0.9, max_tokens=800, want_json=True):
        return {
            "name": "Кристальный зал",
            "description": "Зал с świeтящими кристаллами на стенах",
            "atmosphere": "Тепло и свет",
            "dangers": "Кристаллы могут ослепить",
            "resources": "Целебная энергия кристаллов",
            "scene": "crystal hall glowing walls",
        }

    ctx = WorldContext(
        day_index=1,
        recent_choices=[],
        active_locations=[],
        active_characters=[],
        world_mood="hopeful",
        open_threads=[],
        pack_needs={"hunger": 5, "thirst": 5, "health": 10},
        season="unknown",
    )

    async with maker() as session:
        loc = await generate_ai_location(session, ctx, mock_llm_caller)
        assert loc is not None
        assert loc.name == "Кристальный зал"
        assert loc.scene == "crystal hall glowing walls"

    await engine.dispose()


@pytest.mark.asyncio
async def test_generate_ai_location_fallback():
    """Генерация AI-локации возвращает None при ошибке LLM (фолбэк в get_or_create_location)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def failing_llm_caller(messages, temperature=0.9, max_tokens=800, want_json=True):
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
        loc = await generate_ai_location(session, ctx, failing_llm_caller)
        # При ошибке LLM возвращает None
        assert loc is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_get_or_create_location_existing():
    """Получение существующей локации."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with maker() as session:
        # Добавляем локацию
        loc = WorldLocation(
            name="Тестовая локация",
            description="Описание",
            atmosphere="Атмосфера",
            created_day=1,
            times_visited=0,
        )
        session.add(loc)
        await session.commit()

        ctx = WorldContext(
            day_index=2,
            recent_choices=[],
            active_locations=[
                {"name": "Тестовая локация", "description": "Описание", "atmosphere": "Атмосфера", "times_visited": 0},
            ],
            active_characters=[],
            world_mood="tense",
            open_threads=[],
            pack_needs={"hunger": 5, "thirst": 5, "health": 10},
            season="unknown",
        )

        async def mock_llm_caller(messages, temperature=0.9, max_tokens=800, want_json=True):
            # Не должен вызываться — есть существующая локация
            raise Exception("LLM не должен вызываться")

        result = await get_or_create_location(session, ctx, mock_llm_caller)
        assert result.name == "Тестовая локация"

    await engine.dispose()


@pytest.mark.asyncio
async def test_get_or_create_location_new():
    """Создание новой локации когда нет существующих."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def mock_llm_caller(messages, temperature=0.9, max_tokens=800, want_json=True):
        return {
            "name": "Новая локация",
            "description": "Описание новой локации",
            "atmosphere": "Атмосфера",
            "dangers": "Опасности",
            "resources": "Ресурсы",
            "scene": "new location scene",
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
        result = await get_or_create_location(session, ctx, mock_llm_caller)
        assert result.name == "Новая локация"

        # Проверяем что локация сохранилась в БД
        from sqlalchemy import select
        q = select(WorldLocation).where(WorldLocation.name == "Новая локация")
        db_result = await session.execute(q)
        db_loc = db_result.scalar_one_or_none()
        assert db_loc is not None
        assert db_loc.description == "Описание новой локации"

    await engine.dispose()


@pytest.mark.asyncio
async def test_update_location_visit():
    """Обновление статистики посещения локации."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with maker() as session:
        # Добавляем локацию
        loc = WorldLocation(
            name="Тестовая локация",
            description="Описание",
            atmosphere="Атмосфера",
            created_day=1,
            times_visited=0,
        )
        session.add(loc)
        await session.commit()

        # Обновляем посещение
        await update_location_visit(session, "Тестовая локация", day_index=5)
        await session.commit()

        # Проверяем
        from sqlalchemy import select
        q = select(WorldLocation).where(WorldLocation.name == "Тестовая локация")
        result = await session.execute(q)
        updated_loc = result.scalar_one_or_none()
        assert updated_loc.times_visited == 1
        assert updated_loc.last_visited_day == 5

    await engine.dispose()


# ── Tests: AI Character Generation ─────────────────────────────────────────


def test_ai_character_creation():
    """AICharacter создаётся с правильными полями."""
    char = AICharacter(
        name="ТестовыйNPC",
        role="npc",
        personality="Характер персонажа",
        flaw="Слабость",
        virtue="Сила",
        moral_alignment="complex",
        mood="friendly",
        speech_style="Говорит коротко",
    )
    assert char.name == "ТестовыйNPC"
    assert char.role == "npc"
    assert char.moral_alignment == "complex"
    assert char.speech_style == "Говорит коротко"


def test_parse_ai_character_valid():
    """Парсинг валидного ответа AI с персонажем."""
    response = json.dumps({
        "name": "Странник",
        "role": "npc",
        "personality": "Молчаливый путник",
        "flaw": "Не доверяет",
        "virtue": "Помогает",
        "moral_alignment": "complex",
        "mood": "neutral",
        "speech_style": "Говорит мало",
    })
    char = _parse_ai_character(response)
    assert char is not None
    assert char.name == "Странник"
    assert char.moral_alignment == "complex"


def test_parse_ai_character_no_json():
    """Парсинг ответа без JSON."""
    char = _parse_ai_character("Просто текст")
    assert char is None


def test_parse_ai_character_missing_fields():
    """Парсинг ответа с неполными данными."""
    response = json.dumps({"name": "Тест"})  # Нет personality
    char = _parse_ai_character(response)
    assert char is None


def test_parse_ai_character_truncation():
    """Парсинг ответа с длинными строками."""
    response = json.dumps({
        "name": "A" * 100,
        "personality": "B" * 400,
        "flaw": "C" * 200,
        "virtue": "D" * 200,
        "moral_alignment": "neutral",
        "mood": "friendly",
        "speech_style": "E" * 200,
    })
    char = _parse_ai_character(response)
    assert char is not None
    assert len(char.name) <= 80
    assert len(char.personality) <= 300
    assert len(char.flaw) <= 150
    assert len(char.speech_style) <= 150


def test_build_character_prompt_basic():
    """Базовый промпт для генерации персонажа."""
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
    prompt = _build_character_prompt(ctx)
    assert "НОВОГО персонажа" in prompt
    assert "JSON" in prompt
    assert "name" in prompt
    assert "personality" in prompt


def test_build_character_prompt_with_existing():
    """Промпт содержит существующих персонажей (не повторять)."""
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
    prompt = _build_character_prompt(ctx)
    assert "СУЩЕСТВУЮЩИЕ ПЕРСОНАЖИ" in prompt
    assert "Лайнер" in prompt


@pytest.mark.asyncio
async def test_generate_ai_character_with_llm():
    """Генерация AI-персонажа с mock LLM."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def mock_llm_caller(messages, temperature=0.9, max_tokens=800, want_json=True):
        return {
            "name": "НовыйNPC",
            "role": "npc",
            "personality": "Интересный персонаж",
            "flaw": "Слабость",
            "virtue": "Сила",
            "moral_alignment": "complex",
            "mood": "curious",
            "speech_style": "Говорит загадками",
        }

    ctx = WorldContext(
        day_index=1,
        recent_choices=[],
        active_locations=[],
        active_characters=[],
        world_mood="hopeful",
        open_threads=[],
        pack_needs={"hunger": 5, "thirst": 5, "health": 10},
        season="unknown",
    )

    async with maker() as session:
        char = await generate_ai_character(session, ctx, mock_llm_caller)
        assert char is not None
        assert char.name == "НовыйNPC"
        assert char.moral_alignment == "complex"

    await engine.dispose()


@pytest.mark.asyncio
async def test_generate_ai_character_fallback():
    """Генерация AI-персонажа возвращает None при ошибке LLM."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def failing_llm_caller(messages, temperature=0.9, max_tokens=800, want_json=True):
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
        char = await generate_ai_character(session, ctx, failing_llm_caller)
        assert char is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_get_or_create_character_existing():
    """Получение существующего персонажа."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with maker() as session:
        # Добавляем персонажа
        char = WorldCharacter(
            name="ТестовыйNPC",
            role="npc",
            personality="Характер",
            created_day=1,
        )
        session.add(char)
        await session.commit()

        ctx = WorldContext(
            day_index=2,
            recent_choices=[],
            active_locations=[],
            active_characters=[
                {"name": "ТестовыйNPC", "role": "npc", "personality": "Характер", "mood": "neutral", "trust_stay": 5},
            ],
            world_mood="tense",
            open_threads=[],
            pack_needs={"hunger": 5, "thirst": 5, "health": 10},
            season="unknown",
        )

        async def mock_llm_caller(messages, temperature=0.9, max_tokens=800, want_json=True):
            raise Exception("LLM не должен вызываться")

        result = await get_or_create_character(session, ctx, mock_llm_caller, role="npc")
        assert result.name == "ТестовыйNPC"

    await engine.dispose()


@pytest.mark.asyncio
async def test_get_or_create_character_new():
    """Создание нового персонажа когда нет существующих."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def mock_llm_caller(messages, temperature=0.9, max_tokens=800, want_json=True):
        return {
            "name": "НовыйNPC",
            "role": "npc",
            "personality": "Интересный персонаж",
            "flaw": "Слабость",
            "virtue": "Сила",
            "moral_alignment": "complex",
            "mood": "curious",
            "speech_style": "Говорит загадками",
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
        result = await get_or_create_character(session, ctx, mock_llm_caller, role="npc")
        assert result.name == "НовыйNPC"

        # Проверяем что персонаж сохранился в БД
        from sqlalchemy import select
        q = select(WorldCharacter).where(WorldCharacter.name == "НовыйNPC")
        db_result = await session.execute(q)
        db_char = db_result.scalar_one_or_none()
        assert db_char is not None
        assert db_char.personality == "Интересный персонаж"

    await engine.dispose()


@pytest.mark.asyncio
async def test_update_character_state():
    """Обновление состояния персонажа."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with maker() as session:
        # Добавляем персонажа
        char = WorldCharacter(
            name="ТестовыйNPC",
            role="npc",
            personality="Характер",
            trust_stay=5,
            created_day=1,
        )
        session.add(char)
        await session.commit()

        # Обновляем состояние
        await update_character_state(
            session,
            character_name="ТестовыйNPC",
            mood="friendly",
            trust_delta=2,
            day_index=5,
        )
        await session.commit()

        # Проверяем
        from sqlalchemy import select
        q = select(WorldCharacter).where(WorldCharacter.name == "ТестовыйNPC")
        result = await session.execute(q)
        updated_char = result.scalar_one_or_none()
        assert updated_char.mood == "friendly"
        assert updated_char.trust_stay == 7
        assert updated_char.last_seen_day == 5

    await engine.dispose()
