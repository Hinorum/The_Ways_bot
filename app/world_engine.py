"""AI World Engine — генерация мира, выборов и последствий.

Заменяет фиксированные карты на AI-генерируемые выборы.
Мир создаётся динамически: локации, персонажи, события — всё генерируется AI
и сохраняется в БД для консистентности.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    WorldChoice,
    WorldCharacter,
    WorldEvent,
    WorldLocation,
    WorldSnapshot,
)

logger = logging.getLogger(__name__)

# ── Data classes ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AIChoice:
    """Один AI-сгенерированный выбор."""

    title: str
    description: str
    consequence: str
    tag: str  # risk | care | cunning | custom
    characters_involved: list[str]
    location: str | None = None
    food_cost: int = 0  # Сколько еды тратится (-)
    water_cost: int = 0  # Сколько воды тратится (-)
    health_risk: int = 0  # Максимальный урон здоровью (-)
    trust_change: int = 0  # Изменение trust (+/-)
    emotional_consequence: str = ""  # Эмоциональное описание
    npc_reactions: list = None  # Реакции NPC [{name, reaction}]


@dataclass(frozen=True)
class WorldContext:
    """Контекст мира для генерации AI-выборов."""

    day_index: int
    recent_choices: list[dict]  # последние выборы
    active_locations: list[dict]  # активные локации
    active_characters: list[dict]  # активные персонажи
    world_mood: str  # tense | peaceful | chaotic | hopeful | grim
    open_threads: list[str]  # незавершённые сюжетные линии
    pack_needs: dict  # hunger, thirst, health
    season: str  # текущий сезон


# ── World State Queries ────────────────────────────────────────────────────


async def get_world_context(
    session: AsyncSession, day_index: int, pack_state: dict | None = None,
    season: str | None = None,
) -> WorldContext:
    """Собирает контекст мира для генерации AI-выборов."""

    # Последние 10 выборов
    recent_q = (
        select(WorldChoice)
        .order_by(WorldChoice.day_index.desc())
        .limit(10)
    )
    recent_result = await session.execute(recent_q)
    recent_choices = [
        {
            "day": c.day_index,
            "text": c.choice_text,
            "tag": c.choice_tag,
            "won": c.won,
            "consequences": c.consequences_json,
        }
        for c in recent_result.scalars().all()
    ]

    # Активные локации
    loc_q = select(WorldLocation).where(WorldLocation.is_active == True)
    loc_result = await session.execute(loc_q)
    active_locations = [
        {
            "name": l.name,
            "description": l.description,
            "atmosphere": l.atmosphere,
            "times_visited": l.times_visited,
        }
        for l in loc_result.scalars().all()
    ]

    # Активные персонажи
    char_q = select(WorldCharacter).where(WorldCharacter.is_alive == True)
    char_result = await session.execute(char_q)
    active_characters = [
        {
            "name": c.name,
            "role": c.role,
            "personality": c.personality,
            "mood": c.mood,
            "trust_stay": c.trust_stay,
        }
        for c in char_result.scalars().all()
    ]

    # Последний снимок мира
    snap_q = (
        select(WorldSnapshot)
        .order_by(WorldSnapshot.day_index.desc())
        .limit(1)
    )
    snap_result = await session.execute(snap_q)
    snapshot = snap_result.scalar_one_or_none()

    world_mood = snapshot.mood if snapshot else "tense"
    open_threads = json.loads(snapshot.open_threads) if snapshot and snapshot.open_threads else []

    return WorldContext(
        day_index=day_index,
        recent_choices=recent_choices,
        active_locations=active_locations,
        active_characters=active_characters,
        world_mood=world_mood,
        open_threads=open_threads,
        pack_needs=pack_state or {"hunger": 5, "thirst": 5, "health": 10},
        season=season or "unknown",
    )


# ── AI Choice Generation ──────────────────────────────────────────────────


def _build_world_prompt(ctx: WorldContext) -> str:
    """Строит промпт для AI-генерации выборов на основе текущего состояния мира."""

    parts = [
        "Ты — Ведущий (Dungeon Master) игры. Мир живёт и меняется от выборов игроков.",
        "Сгенерируй 3 варианта выбора для стаи бездомных собак, которые бродят по лабиринту.",
        "",
        "КРИТИЧЕСКИЕ ПРАВИЛА:",
        "- Каждый выбор ОБЯЗАТЕЛЬНО имеет последствия, которые повлияют на мир",
        "- Выборы должны быть ТРУДНЫМИ дилеммами без очевидно правильного ответа",
        "- Никаких метакомментариев — только художественный текст",
        "- Пиши простым русским языком, короткими предложениями",
        "- Каждый выбор — уникальная ситуация, не повторяй предыдущие",
        "",
    ]

    # Контекст мира
    if ctx.active_locations:
        parts.append("ЛОКАЦИИ:")
        for loc in ctx.active_locations[:5]:
            parts.append(f"- {loc['name']}: {loc['description'][:100]}")
        parts.append("")

    if ctx.active_characters:
        parts.append("ПЕРСОНАЖИ:")
        for char in ctx.active_characters[:5]:
            parts.append(
                f"- {char['name']} ({char['role']}): {char['personality'][:80]}, "
                f"доверие к стае: {char['trust_stay']}/10"
            )
        parts.append("")

    if ctx.recent_choices:
        parts.append("ПОСЛЕДНИЕ ВЫБОРЫ СТАИ:")
        for choice in ctx.recent_choices[:5]:
            won_mark = " [ВЫБРАН]" if choice["won"] else ""
            parts.append(f"- День {choice['day']}: {choice['text'][:80]}{won_mark}")
        parts.append("")

    # Потребности стаи
    needs = ctx.pack_needs
    parts.append(
        f"ПОТРЕБНОСТИ СТАИ: голод={needs.get('hunger', 5)}, "
        f"жажда={needs.get('thirst', 5)}, здоровье={needs.get('health', 10)}"
    )

    if ctx.open_threads:
        parts.append(f"НЕЗАВЕРШЁННЫЕ СЮЖЕТЫ: {'; '.join(ctx.open_threads[:3])}")

    parts.extend([
        "",
        "СГЕНЕРИРУЙ 3 ВЫБОРА. Формат JSON:",
        '{',
        '  "choices": [',
        '    {',
        '      "title": "Краткое название (2-5 слов)",',
        '      "description": "Описание ситуации (1-2 предложения)",',
        '      "consequence": "Что произойдёт при выборе (1-2 предложения)",',
        '      "tag": "risk|care|cunning",',
        '      "characters_involved": ["имя"],',
        '      "location": "название локации или null",',
        '      "food_cost": 0,',
        '      "water_cost": 0,',
        '      "health_risk": 0,',
        '      "trust_change": 0,',
        '      "emotional_consequence": "Эмоциональное описание (1-3 предложения)",',
        '      "npc_reactions": [{"name": "имя", "reaction": "что сказал/подумал"}]',
        '    }',
        '  ]',
        '}',
        "",
        "СТОИМОСТЬ ВЫБОРА (обязательные поля):",
        "- food_cost: сколько еды тратится (0-3). 0 = бесплатно, 3 = дорого",
        "- water_cost: сколько воды тратится (0-3)",
        "- health_risk: максимальный урон здоровью (0-5). 0 = безопасно, 5 = смертельно",
        "- trust_change: изменение доверия (-3 до +3). -3 = предательство, +3 = героизм",
        "",
        "ЭМОЦИОНАЛЬНОЕ ОПИСАНИЕ (обязательно):",
        "- Что увидели псы в момент выбора",
        "- Что почувствовали (страх, надежду, гордость, боль)",
        "- Как изменилась атмосфера вокруг",
        "- Что останется в памяти стаи",
        "- 1-3 предложения, красивый русский язык",
        "",
        "РЕАКЦИИ NPC (обязательно):",
        "- 1-3 персонажа из characters_involved",
        "- Что они сказали или подумали",
        "- Как отреагировали на выбор",
        "- Их характер и позиция",
        "- 1-2 предложения на персонажа",
        "",
        "ПРАВИЛА ДЛЯ ЦЕН:",
        "- risk: health_risk >= 2, food_cost >= 1",
        "- care: trust_change >= 1, food_cost >= 1",
        "- cunning: health_risk >= 1, trust_change <= 0",
        "- Каждый выбор должен иметь ХОТЯ БЫ ОДНУ ненулевую стоимость",
        "- Дорогие выборы дают больше награды (опиши в consequence)",
        "",
        "ТРЕБОВАНИЯ К TAG:",
        "- risk: опасный путь, шанс потерять или получить много",
        "- care: забота, помощь, но ценой",
        "- cunning: хитрость, обман, но может не сработать",
        "",
        "Каждый выбор должен:",
        "1. Быть связан с текущим состоянием мира",
        "2. Иметь конкретные последствия",
        "3. Вовлекать хотя бы одного персонажа",
        "4. Происходить в определённой локации",
        "5. Иметь конкретную стоимость (еда/вода/здоровье/доверие)",
        "6. Иметь эмоциональное описание",
        "7. Иметь реакции NPC",
    ])

    return "\n".join(parts)


def _parse_ai_choices(response_text: str) -> list[AIChoice]:
    """Парсит ответ AI и возвращает список выборов."""

    # Ищем JSON в ответе
    json_match = re.search(r'\{[\s\S]*"choices"[\s\S]*\}', response_text)
    if not json_match:
        logger.warning("AIWorldEngine: не найден JSON в ответе AI")
        return []

    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError:
        logger.warning("AIWorldEngine: ошибка парсинга JSON")
        return []

    choices = []
    for item in data.get("choices", []):
        if not all(k in item for k in ("title", "description", "consequence", "tag")):
            continue

        tag = item["tag"]
        if tag not in ("risk", "care", "cunning", "custom"):
            tag = "custom"

        choices.append(
            AIChoice(
                title=item["title"][:120],
                description=item["description"][:500],
                consequence=item["consequence"][:500],
                tag=tag,
                characters_involved=item.get("characters_involved", []),
                location=item.get("location"),
            )
        )

    return choices[:3]  # Максимум 3 выбора


async def generate_ai_choices(
    session: AsyncSession,
    ctx: WorldContext,
    llm_caller,
) -> list[AIChoice]:
    """Генерирует 3 AI-выбора на основе контекста мира.

    Args:
        session: сессия БД
        ctx: контекст мира
        llm_caller: async callable (messages, temperature, max_tokens, want_json) -> dict | None
    """

    prompt = _build_world_prompt(ctx)
    messages = [{"role": "user", "content": prompt}]

    try:
        result = await llm_caller(messages, temperature=0.9, max_tokens=1500, want_json=True)
    except Exception as e:
        logger.warning("AIWorldEngine: LLM не ответил: %s", e)
        result = None

    if not result or "choices" not in (result[0] if isinstance(result, tuple) else result):
        # Фолбэк: генерируем простые выборы на основе контекста
        return _fallback_choices(ctx)

    response = result[0] if isinstance(result, tuple) else result
    choices_text = response.get("choices", response)
    if isinstance(choices_text, str):
        choices = _parse_ai_choices(choices_text)
    else:
        # Уже dict
        choices = []
        for item in choices_text if isinstance(choices_text, list) else []:
            if isinstance(item, dict) and all(k in item for k in ("title", "description", "consequence", "tag")):
                # Парсим npc_reactions
                npc_reactions_raw = item.get("npc_reactions", [])
                npc_reactions = []
                if isinstance(npc_reactions_raw, list):
                    for r in npc_reactions_raw:
                        if isinstance(r, dict) and "name" in r and "reaction" in r:
                            npc_reactions.append({
                                "name": str(r["name"])[:50],
                                "reaction": str(r["reaction"])[:200],
                            })

                choices.append(
                    AIChoice(
                        title=item["title"][:120],
                        description=item["description"][:500],
                        consequence=item["consequence"][:500],
                        tag=item.get("tag", "custom"),
                        characters_involved=item.get("characters_involved", []),
                        location=item.get("location"),
                        food_cost=int(item.get("food_cost", 0) or 0),
                        water_cost=int(item.get("water_cost", 0) or 0),
                        health_risk=int(item.get("health_risk", 0) or 0),
                        trust_change=int(item.get("trust_change", 0) or 0),
                        emotional_consequence=item.get("emotional_consequence", "")[:500],
                        npc_reactions=npc_reactions[:3],
                    )
                )

    if len(choices) < 3:
        # Дополняем фолбэком
        fallback = _fallback_choices(ctx)
        for fb in fallback:
            if len(choices) >= 3:
                break
            if fb.title not in [c.title for c in choices]:
                choices.append(fb)

    return choices[:3]


def _fallback_choices(ctx: WorldContext) -> list[AIChoice]:
    """Генерирует фолбэк-выборы на основе контекста, когда AI недоступен."""

    needs = ctx.pack_needs
    hunger = needs.get("hunger", 5)
    health = needs.get("health", 10)

    choices = []

    # Выбор на основе потребностей
    if hunger > 7:
        choices.append(
            AIChoice(
                title="Голодный путь",
                description="Стая стоит перед развилкой: влево — тёмный коридор с запахом еды, вправо — светлый проход в никуда.",
                consequence="Если пойдём на запах — может быть еда, а может быть ловушка. Если в светлый — точно не еда, но безопасно.",
                tag="risk",
                characters_involved=[],
            )
        )
    else:
        choices.append(
            AIChoice(
                title="Тихий коридор",
                description="Коридор уходит вглубь. Стены холодные, но ровные. Где-то вдали капает вода.",
                consequence="Можно идти вперёд — возможно, найдём что-то полезное. Или вернуться — ничего не потеряем.",
                tag="cunning",
                characters_involved=[],
            )
        )

    if health < 7:
        choices.append(
            AIChoice(
                title="Целительный лист",
                description="На стене растёт блестящий мох. Он выглядит как лекарство — но кто знает.",
                consequence="Мох светится зелёным. Если съесть — может помочь. Если нет — будет хуже.",
                tag="care",
                characters_involved=[],
            )
        )
    else:
        choices.append(
            AIChoice(
                title="Чужой след",
                description="На полу свежие следы. Кто-то был здесь совсем недавно — и пошёл дальше в лабиринт.",
                consequence="Можно следовать за следами — может привести к людям или к опасности. Или игнорировать.",
                tag="risk",
                characters_involved=[],
            )
        )

    choices.append(
        AIChoice(
            title="Развилка теней",
            description="Три коридора расходятся. В каждом — своя тишина. Лабиринт ждёт решения.",
            consequence="Каждый путь ведёт к разным последствиям. Назад дороги нет.",
            tag="cunning",
            characters_involved=[],
            location=ctx.active_locations[0]["name"] if ctx.active_locations else None,
        )
    )

    return choices[:3]


# ── World State Management ─────────────────────────────────────────────────


async def record_choice(
    session: AsyncSession,
    day_index: int,
    choice: AIChoice,
    votes_count: int = 0,
    won: bool = False,
) -> WorldChoice:
    """Записывает выбор в БД и применяет стоимость к стае."""

    world_choice = WorldChoice(
        day_index=day_index,
        choice_text=choice.description,
        choice_tag=choice.tag,
        consequences_json=json.dumps([choice.consequence], ensure_ascii=False),
        characters_involved=json.dumps(choice.characters_involved, ensure_ascii=False),
        location=choice.location,
        votes_count=votes_count,
        won=won,
    )
    session.add(world_choice)

    # Применяем стоимость к PackState (только для выигравшего выбора)
    if won:
        from app.models import PackState
        from sqlalchemy import select as sa_select

        q = sa_select(PackState).limit(1)
        result = await session.execute(q)
        pack = result.scalar_one_or_none()

        if pack:
            # Еда: +2 за выбор, -cost
            pack.hunger = max(0, min(10, pack.hunger + 2 - choice.food_cost))
            # Вода: +2 за выбор, -cost
            pack.thirst = max(0, min(10, pack.thirst + 2 - choice.water_cost))
            # Здоровье: -risk (рандомно от 0 до health_risk)
            import random
            actual_damage = random.randint(0, choice.health_risk) if choice.health_risk > 0 else 0
            pack.health = max(0, min(10, pack.health - actual_damage))
            pack.last_updated_day = day_index

            logger.info(
                "AIWorldEngine: выбор '%s' применён: hunger=%d, thirst=%d, health=%d (урон=%d)",
                choice.title, pack.hunger, pack.thirst, pack.health, actual_damage,
            )

    await session.flush()
    return world_choice


async def record_world_event(
    session: AsyncSession,
    day_index: int,
    event_type: str,
    description: str,
    characters_involved: list[str] | None = None,
    locations_involved: list[str] | None = None,
    impact: str = "",
) -> WorldEvent:
    """Записывает событие мира."""

    event = WorldEvent(
        day_index=day_index,
        event_type=event_type,
        description=description,
        characters_involved=json.dumps(characters_involved or [], ensure_ascii=False),
        locations_involved=json.dumps(locations_involved or [], ensure_ascii=False),
        impact=impact,
    )
    session.add(event)
    await session.flush()
    return event


async def create_world_snapshot(
    session: AsyncSession,
    day_index: int,
    llm_caller,
) -> WorldSnapshot:
    """AI создаёт снимок мира в конце дня."""

    # Собираем данные дня
    choices_q = (
        select(WorldChoice)
        .where(WorldChoice.day_index == day_index)
    )
    choices_result = await session.execute(choices_q)
    day_choices = choices_result.scalars().all()

    events_q = (
        select(WorldEvent)
        .where(WorldEvent.day_index == day_index)
    )
    events_result = await session.execute(events_q)
    day_events = events_result.scalars().all()

    locs_q = select(WorldLocation).where(WorldLocation.is_active == True)
    locs_result = await session.execute(locs_q)
    active_locs = locs_result.scalars().all()

    chars_q = select(WorldCharacter).where(WorldCharacter.is_alive == True)
    chars_result = await session.execute(chars_q)
    active_chars = chars_result.scalars().all()

    # Промпт для AI
    prompt_parts = [
        "Проанализируй день в мире игры и создай краткий снимок.",
        "",
        f"День {day_index}.",
        "",
    ]

    if day_choices:
        prompt_parts.append("ВЫБОРЫ ДНЯ:")
        for c in day_choices:
            mark = " [ПОБЕДИЛ]" if c.won else ""
            prompt_parts.append(f"- {c.choice_text[:80]}{mark}")

    if day_events:
        prompt_parts.append("")
        prompt_parts.append("СОБЫТИЯ:")
        for e in day_events:
            prompt_parts.append(f"- [{e.event_type}] {e.description[:80]}")

    prompt_parts.extend([
        "",
        "СОЗДАЙ СНИМОК МИРА. Формат JSON:",
        '{',
        '  "mood": "tense|peaceful|chaotic|hopeful|grim",',
        '  "summary": "1-2 предложения о дне",',
        '  "open_threads": ["незавершённая линия"],',
        '  "world_trend": "что меняется в мире"',
        '}',
    ])

    prompt = "\n".join(prompt_parts)
    messages = [{"role": "user", "content": prompt}]

    try:
        result = await llm_caller(messages, temperature=0.7, max_tokens=500, want_json=True)
    except Exception:
        result = None

    mood = "tense"
    summary = f"День {day_index} в лабиринте."
    open_threads = []
    world_trend = ""

    if result:
        response = result[0] if isinstance(result, tuple) else result
        if isinstance(response, dict):
            mood = response.get("mood", mood)
            summary = response.get("summary", summary)
            open_threads = response.get("open_threads", open_threads)
            world_trend = response.get("world_trend", world_trend)

    snapshot = WorldSnapshot(
        day_index=day_index,
        mood=mood,
        summary=summary,
        active_locations=json.dumps([l.name for l in active_locs], ensure_ascii=False),
        active_characters=json.dumps([c.name for c in active_chars], ensure_ascii=False),
        open_threads=json.dumps(open_threads, ensure_ascii=False),
        world_trend=world_trend,
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


# ── AI Location Generation ─────────────────────────────────────────────────


@dataclass(frozen=True)
class AILocation:
    """AI-генерируемая локация."""

    name: str
    description: str
    atmosphere: str
    dangers: str
    resources: str
    scene: str  # English image prompt for cover art


def _build_location_prompt(ctx: WorldContext) -> str:
    """Строит промпт для AI-генерации новой локации."""

    parts = [
        "Сгенерируй НОВУЮ локацию для лабиринта, где бродят бездомные собаки.",
        "",
        "ТРЕБОВАНИЯ:",
        "- Локация должна быть уникальной, не повторять существующие",
        "- Имеет конкретную атмосферу, опасности и ресурсы",
        "- Подходит для дневного исследования стаей",
        "- Название на русском, 2-4 слова",
        "",
    ]

    if ctx.active_locations:
        parts.append("СУЩЕСТВУЮЩИЕ ЛОКАЦИИ (не повторять):")
        for loc in ctx.active_locations[:10]:
            parts.append(f"- {loc['name']}")
        parts.append("")

    if ctx.world_mood:
        mood_desc = {
            "tense": "напряжённая атмосфера",
            "peaceful": "спокойная атмосфера",
            "chaotic": "хаотичная атмосфера",
            "hopeful": "надежда в воздухе",
            "grim": "мрачная атмосфера",
        }.get(ctx.world_mood, "")
        if mood_desc:
            parts.append(f"ТЕКУЩЕЕ НАСТРОЕНИЕ МИРА: {mood_desc}")
            parts.append("")

    parts.extend([
        "СОЗДАЙ ЛОКАЦИЮ. Формат JSON:",
        '{',
        '  "name": "Название (2-4 слова на русском)",',
        '  "description": "Описание локации (2-3 предложения)",',
        '  "atmosphere": "Атмосфера и ощущения (1-2 предложения)",',
        '  "dangers": "Что может пойти не так (1 предложение)",',
        '  "resources": "Что можно найти полезного (1 предложение)",',
        '  "scene": "Brief English image prompt for cover art (10-15 words)"',
        '}',
    ])

    return "\n".join(parts)


def _parse_ai_location(response_text: str) -> AILocation | None:
    """Парсит ответ AI и возвращает локацию."""

    json_match = re.search(r'\{[\s\S]*"name"[\s\S]*\}', response_text)
    if not json_match:
        return None

    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError:
        return None

    if not all(k in data for k in ("name", "description", "atmosphere")):
        return None

    return AILocation(
        name=data["name"][:120],
        description=data["description"][:500],
        atmosphere=data.get("atmosphere", "")[:300],
        dangers=data.get("dangers", "")[:200],
        resources=data.get("resources", "")[:200],
        scene=data.get("scene", "dark labyrinth corridor")[:200],
    )


async def generate_ai_location(
    session: AsyncSession,
    ctx: WorldContext,
    llm_caller,
) -> AILocation | None:
    """Генерирует новую AI-локацию на основе контекста мира."""

    prompt = _build_location_prompt(ctx)
    messages = [{"role": "user", "content": prompt}]

    try:
        result = await llm_caller(messages, temperature=0.9, max_tokens=800, want_json=True)
    except Exception as e:
        logger.warning("AIWorldEngine: LLM не ответил для локации: %s", e)
        return None

    if not result:
        return None

    response = result[0] if isinstance(result, tuple) else result
    if isinstance(response, dict):
        # Уже dict — напрямую
        if all(k in response for k in ("name", "description", "atmosphere")):
            return AILocation(
                name=response["name"][:120],
                description=response["description"][:500],
                atmosphere=response.get("atmosphere", "")[:300],
                dangers=response.get("dangers", "")[:200],
                resources=response.get("resources", "")[:200],
                scene=response.get("scene", "dark labyrinth corridor")[:200],
            )
    elif isinstance(response, str):
        return _parse_ai_location(response)

    return None


async def get_or_create_location(
    session: AsyncSession,
    ctx: WorldContext,
    llm_caller,
) -> AILocation:
    """Получает существующую локацию или создаёт новую.

    Логика:
    1. Если есть активные локации — выбираем одну (с учётом давности посещения)
    2. Если нет — генерируем новую через AI
    """

    # Пробуем взять существующую локацию
    if ctx.active_locations:
        # Выбираем локацию, которая дольше не посещалась
        least_visited = min(ctx.active_locations, key=lambda x: x.get("times_visited", 0))
        return AILocation(
            name=least_visited["name"],
            description=least_visited["description"],
            atmosphere=least_visited.get("atmosphere", ""),
            dangers="",
            resources="",
            scene="dark labyrinth corridor",
        )

    # Генерируем новую
    new_loc = await generate_ai_location(session, ctx, llm_caller)
    if new_loc:
        # Сохраняем в БД
        db_loc = WorldLocation(
            name=new_loc.name,
            description=new_loc.description,
            atmosphere=new_loc.atmosphere,
            dangers=new_loc.dangers,
            resources=new_loc.resources,
            created_day=ctx.day_index,
        )
        session.add(db_loc)
        await session.flush()
        return new_loc

    # Фолбэк
    return AILocation(
        name="Безымянный коридор",
        description="Тёмный коридор уходит вглубь. Стены холодные и ровные.",
        atmosphere="Тишина и холод",
        dangers="Неизвестно",
        resources="Возможно, что-то полезное",
        scene="dark labyrinth corridor",
    )


async def update_location_visit(
    session: AsyncSession,
    location_name: str,
    day_index: int,
) -> None:
    """Обновляет статистику посещения локации."""

    q = select(WorldLocation).where(WorldLocation.name == location_name)
    result = await session.execute(q)
    loc = result.scalar_one_or_none()

    if loc:
        loc.last_visited_day = day_index
        loc.times_visited += 1
        await session.flush()


# ── AI Character Generation ────────────────────────────────────────────────


@dataclass(frozen=True)
class AICharacter:
    """AI-генерируемый персонаж."""

    name: str
    role: str  # "pack" | "npc" | "neutral" | "hostile"
    personality: str
    flaw: str
    virtue: str
    moral_alignment: str  # good | neutral | evil | complex
    mood: str  # neutral | hostile | friendly | fearful | curious
    speech_style: str  # как говорит (кратко)


def _build_character_prompt(ctx: WorldContext) -> str:
    """Строит промпт для AI-генерации нового персонажа."""

    parts = [
        "Сгенерируй НОВОГО персонажа для мира лабиринта, где бродят бездомные собаки.",
        "",
        "ТРЕБОВАНИЯ:",
        "- Имя на русском (1-2 слова), уникальное",
        "- Чёткий характер с сильными и слабыми сторонами",
        "- Моральный компас не однозначный (нет абсолютного зла/добра)",
        "- Стиль речи: как говорит, жесты, привычки",
        "- Персонаж должен иметь мотивацию и конфликт",
        "",
    ]

    if ctx.active_characters:
        parts.append("СУЩЕСТВУЮЩИЕ ПЕРСОНАЖИ (не повторять):")
        for char in ctx.active_characters[:8]:
            parts.append(f"- {char['name']} ({char['role']}): {char['personality'][:60]}")
        parts.append("")

    if ctx.world_mood:
        mood_desc = {
            "tense": "напряжённая атмосфера",
            "peaceful": "спокойная атмосфера",
            "chaotic": "хаотичная атмосфера",
            "hopeful": "надежда в воздухе",
            "grim": "мрачная атмосфера",
        }.get(ctx.world_mood, "")
        if mood_desc:
            parts.append(f"ТЕКУЩЕЕ НАСТРОЕНИЕ МИРА: {mood_desc}")
            parts.append("")

    parts.extend([
        "СОЗДАЙ ПЕРСОНАЖА. Формат JSON:",
        '{',
        '  "name": "Имя (1-2 слова на русском)",',
        '  "role": "pack|npc|neutral|hostile",',
        '  "personality": "Характер (2-3 предложения)",',
        '  "flaw": "Слабость (1 предложение)",',
        '  "virtue": "Сила (1 предложение)",',
        '  "moral_alignment": "good|neutral|evil|complex",',
        '  "mood": "neutral|hostile|friendly|fearful|curious",',
        '  "speech_style": "Как говорит (1 предложение)"',
        '}',
    ])

    return "\n".join(parts)


def _parse_ai_character(response_text: str) -> AICharacter | None:
    """Парсит ответ AI и возвращает персонажа."""

    json_match = re.search(r'\{[\s\S]*"name"[\s\S]*\}', response_text)
    if not json_match:
        return None

    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError:
        return None

    if not all(k in data for k in ("name", "personality")):
        return None

    return AICharacter(
        name=data["name"][:80],
        role=data.get("role", "npc"),
        personality=data["personality"][:300],
        flaw=data.get("flaw", "")[:150],
        virtue=data.get("virtue", "")[:150],
        moral_alignment=data.get("moral_alignment", "neutral"),
        mood=data.get("mood", "neutral"),
        speech_style=data.get("speech_style", "")[:150],
    )


async def generate_ai_character(
    session: AsyncSession,
    ctx: WorldContext,
    llm_caller,
) -> AICharacter | None:
    """Генерирует нового AI-персонажа на основе контекста мира."""

    prompt = _build_character_prompt(ctx)
    messages = [{"role": "user", "content": prompt}]

    try:
        result = await llm_caller(messages, temperature=0.9, max_tokens=800, want_json=True)
    except Exception as e:
        logger.warning("AIWorldEngine: LLM не ответил для персонажа: %s", e)
        return None

    if not result:
        return None

    response = result[0] if isinstance(result, tuple) else result
    if isinstance(response, dict):
        if all(k in response for k in ("name", "personality")):
            return AICharacter(
                name=response["name"][:80],
                role=response.get("role", "npc"),
                personality=response["personality"][:300],
                flaw=response.get("flaw", "")[:150],
                virtue=response.get("virtue", "")[:150],
                moral_alignment=response.get("moral_alignment", "neutral"),
                mood=response.get("mood", "neutral"),
                speech_style=response.get("speech_style", "")[:150],
            )
    elif isinstance(response, str):
        return _parse_ai_character(response)

    return None


async def get_or_create_character(
    session: AsyncSession,
    ctx: WorldContext,
    llm_caller,
    role: str = "npc",
) -> AICharacter:
    """Получает существующего персонажа или создаёт нового.

    Логика:
    1. Если есть активные персонажи нужной роли — выбираем случайного
    2. Если нет — генерируем нового через AI
    """

    # Фильтруем по роли
    same_role = [c for c in ctx.active_characters if c.get("role") == role]

    if same_role:
        # Выбираем случайного из существующих
        import random
        chosen = random.choice(same_role)
        return AICharacter(
            name=chosen["name"],
            role=chosen["role"],
            personality=chosen["personality"],
            flaw=chosen.get("flaw", ""),
            virtue=chosen.get("virtue", ""),
            moral_alignment=chosen.get("moral_alignment", "neutral"),
            mood=chosen.get("mood", "neutral"),
            speech_style=chosen.get("speech_style", ""),
        )

    # Генерируем нового
    new_char = await generate_ai_character(session, ctx, llm_caller)
    if new_char:
        # Сохраняем в БД
        db_char = WorldCharacter(
            name=new_char.name,
            role=new_char.role,
            personality=new_char.personality,
            flaw=new_char.flaw,
            virtue=new_char.virtue,
            moral_alignment=new_char.moral_alignment,
            mood=new_char.mood,
            created_day=ctx.day_index,
        )
        session.add(db_char)
        await session.flush()
        return new_char

    # Фолбэк
    return AICharacter(
        name="Странник",
        role=role,
        personality="Молчаливый путник, прячущий лицо в тени капюшона.",
        flaw="Не доверяет никому",
        virtue="Всегда помогает тем, кто в беде",
        moral_alignment="complex",
        mood="neutral",
        speech_style="Говорит коротко, часто молчит",
    )


async def update_character_state(
    session: AsyncSession,
    character_name: str,
    mood: str | None = None,
    trust_delta: int = 0,
    day_index: int | None = None,
) -> None:
    """Обновляет состояние персонажа: настроение, доверие."""

    q = select(WorldCharacter).where(WorldCharacter.name == character_name)
    result = await session.execute(q)
    char = result.scalar_one_or_none()

    if char:
        if mood is not None:
            char.mood = mood
        char.trust_stay = max(0, min(10, char.trust_stay + trust_delta))
        if day_index is not None:
            char.last_seen_day = day_index
        await session.flush()


# ── AI Consequence Cascading ───────────────────────────────────────────────


@dataclass(frozen=True)
class AIConsequence:
    """AI-генерируемое последствие выбора."""

    cause: str  # что вызвало
    effect: str  # что произойдёт
    affected_characters: list[str]  # кто затронут
    affected_locations: list[str]  # какие локации изменятся
    world_impact: str  # как изменится мир
    mood_shift: str  # как изменится настроение мира
    trust_changes: dict[str, int]  # {имя персонажа: изменение доверия}


@dataclass(frozen=True)
class AIConsequenceChain:
    """Цепочка последствий: одно действие → серия эффектов."""

    root_choice: str  # исходный выбор
    chain: list[AIConsequence]  # цепочка эффектов
    resolution: str  # как цепочка завершается


def _build_consequence_prompt(ctx: WorldContext, choice_text: str, choice_tag: str) -> str:
    """Строит промпт для AI-генерации последствий выбора."""

    parts = [
        "Проанализируй выбор стаи и сгенерируй цепочку последствий.",
        "",
        f"ВЫБОР: {choice_text}",
        f"ТИП: {choice_tag}",
        "",
    ]

    if ctx.active_characters:
        parts.append("ПЕРСОНАЖИ МИРА:")
        for char in ctx.active_characters[:5]:
            parts.append(f"- {char['name']} ({char['role']}): доверие {char['trust_stay']}/10")
        parts.append("")

    if ctx.active_locations:
        parts.append("ЛОКАЦИИ:")
        for loc in ctx.active_locations[:5]:
            parts.append(f"- {loc['name']}")
        parts.append("")

    if ctx.recent_choices:
        parts.append("ПОСЛЕДНИЕ ВЫБОРЫ:")
        for c in ctx.recent_choices[:3]:
            parts.append(f"- {c['text'][:60]}")
        parts.append("")

    parts.extend([
        "СГЕНЕРИРУЙ ЦЕПОЧКУ ПОСЛЕДСТВИЙ. Формат JSON:",
        '{',
        '  "chain": [',
        '    {',
        '      "cause": "описание причины",',
        '      "effect": "что произойдёт (1-2 предложения)",',
        '      "affected_characters": ["имя"],',
        '      "affected_locations": ["локация"],',
        '      "world_impact": "как изменится мир (1 предложение)",',
        '      "mood_shift": "tense|peaceful|chaotic|hopeful|grim",',
        '      "trust_changes": {"имя": число}',
        '    }',
        '  ],',
        '  "resolution": "как цепочка завершается (1-2 предложения)"',
        '}',
        "",
        "ТРЕБОВАНИЯ:",
        "- 1-3 последствия в цепочке",
        "- Каждое последствие реально влияет на мир",
        "- Последствия каскадны: одно порождает следующее",
        "- Персонажи могут реагировать и менять отношение",
    ])

    return "\n".join(parts)


def _parse_ai_consequence_chain(response_text: str) -> AIConsequenceChain | None:
    """Парсит ответ AI и возвращает цепочку последствий."""

    json_match = re.search(r'\{[\s\S]*"chain"[\s\S]*\}', response_text)
    if not json_match:
        return None

    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError:
        return None

    if "chain" not in data or not isinstance(data["chain"], list):
        return None

    chain = []
    for item in data["chain"]:
        if not all(k in item for k in ("cause", "effect")):
            continue
        chain.append(
            AIConsequence(
                cause=item["cause"][:200],
                effect=item["effect"][:500],
                affected_characters=item.get("affected_characters", []),
                affected_locations=item.get("affected_locations", []),
                world_impact=item.get("world_impact", "")[:200],
                mood_shift=item.get("mood_shift", "tense"),
                trust_changes=item.get("trust_changes", {}),
            )
        )

    if not chain:
        return None

    return AIConsequenceChain(
        root_choice=chain[0].cause if chain else "",
        chain=chain,
        resolution=data.get("resolution", "Цепочка последствий завершилась.")[:300],
    )


async def generate_consequence_chain(
    session: AsyncSession,
    ctx: WorldContext,
    llm_caller,
    choice_text: str,
    choice_tag: str,
) -> AIConsequenceChain | None:
    """Генерирует цепочку последствий для выбора стаи."""

    prompt = _build_consequence_prompt(ctx, choice_text, choice_tag)
    messages = [{"role": "user", "content": prompt}]

    try:
        result = await llm_caller(messages, temperature=0.8, max_tokens=1000, want_json=True)
    except Exception as e:
        logger.warning("AIWorldEngine: LLM не ответил для последствий: %s", e)
        return None

    if not result:
        return None

    response = result[0] if isinstance(result, tuple) else result
    if isinstance(response, dict):
        # Уже dict — напрямую
        if "chain" in response and isinstance(response["chain"], list):
            chain = []
            for item in response["chain"]:
                if all(k in item for k in ("cause", "effect")):
                    chain.append(
                        AIConsequence(
                            cause=item["cause"][:200],
                            effect=item["effect"][:500],
                            affected_characters=item.get("affected_characters", []),
                            affected_locations=item.get("affected_locations", []),
                            world_impact=item.get("world_impact", "")[:200],
                            mood_shift=item.get("mood_shift", "tense"),
                            trust_changes=item.get("trust_changes", {}),
                        )
                    )
            if chain:
                return AIConsequenceChain(
                    root_choice=chain[0].cause,
                    chain=chain,
                    resolution=response.get("resolution", "")[:300],
                )
    elif isinstance(response, str):
        return _parse_ai_consequence_chain(response)

    return None


async def apply_consequence_chain(
    session: AsyncSession,
    chain: AIConsequenceChain,
    day_index: int,
) -> None:
    """Применяет цепочку последствий к миру.

    Обновляет:
    - Настроение мира (WorldSnapshot)
    - Доверие персонажей (WorldCharacter)
    - Записывает события (WorldEvent)
    """

    for consequence in chain.chain:
        # Обновляем доверие персонажей
        for char_name, trust_delta in consequence.trust_changes.items():
            await update_character_state(
                session,
                character_name=char_name,
                trust_delta=trust_delta,
                day_index=day_index,
            )

        # Записываем событие мира
        await record_world_event(
            session,
            day_index=day_index,
            event_type="consequence",
            description=f"{consequence.cause} → {consequence.effect}",
            characters_involved=consequence.affected_characters,
            locations_involved=consequence.affected_locations,
            impact=consequence.world_impact,
        )

    logger.info(
        "AIWorldEngine: применена цепочка из %d последствий для дня %d",
        len(chain.chain),
        day_index,
    )


async def process_choice_consequences(
    session: AsyncSession,
    ctx: WorldContext,
    llm_caller,
    choice_text: str,
    choice_tag: str,
    day_index: int,
) -> AIConsequenceChain | None:
    """Полный цикл: генерация + применение последствий выбора.

    Вызывается после победы выбора в голосовании.
    """

    # 1. Генерируем цепочку
    chain = await generate_consequence_chain(session, ctx, llm_caller, choice_text, choice_tag)

    if not chain:
        return None

    # 2. Применяем к миру
    await apply_consequence_chain(session, chain, day_index)

    return chain
