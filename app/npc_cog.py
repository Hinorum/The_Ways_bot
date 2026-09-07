"""NPC Chain-of-Thought — внутренний монолог NPC перед действием.

Архитектурный инсайт: сегодня NPC описываются одним sentiment score
и фиксированными репликами. Chain-of-thought даёт каждому NPC:
1. Внутренний монолог (что он думает о стае)
2. Мотивацию (что он хочет сделать)
3. Действие (конкретный поступок)

Это делает NPC живыми: один и тот же sentiment score может привести
к разным действиям в зависимости от контекста дня.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from app.relations import _TONES


# ── Маппинг русских тонов → английские mood-ключи ──
# _TONES[sentiment] → ("предан стае", "..."), а _INNER_THOUGHTS используют
# "devoted"/"cautious"/"wary"/"hostile". Мост между ними:
_TONE_TO_MOOD: dict[str, str] = {
    "предан стае": "devoted",
    "расположен": "devoted",
    "приветлив": "cautious",
    "ничей": "cautious",
    "насторожен": "wary",
    "враждебен": "hostile",
    "охотится на стаю": "hostile",
}

_DEFAULT_MOOD = "cautious"
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NPCCogResult:
    """Результат chain-of-thought для одного NPC."""

    name: str
    sentiment: int
    tone: str
    inner_thought: str  # Внутренний монолог
    motivation: str  # Что хочет сделать
    action_hint: str  # Подсказка для DM: что NPC делает в этой сцене
    focus_line: str  # Готовая реплика/действие для инжекта в промпт

    def to_prompt_block(self) -> str:
        """Форматирует результат для блока промпта."""
        return (
            f"[{self.name.upper()} — {self.tone} (отношение {self.sentiment:+d})]\n"
            f"Мысли: {self.inner_thought}\n"
            f"Мотив: {self.motivation}\n"
            f"Действие: {self.action_hint}"
        )


# ── Шаблоны внутренних монологов по тону и NPC ──

_INNER_THOUGHTS = {
    "liner": {
        "devoted": [
            "Стая — мой путь. Я веду их, даже если они не понимают куда.",
            "Каждый выбор стаи — это доверие мне. Я не подведу.",
            "Тропа передо мной ясна, за мной — только верные.",
        ],
        "cautious": [
            "Стая идёт, но я не уверен, что они видят ловушки.",
            "Мне нужно быть осторожнее. Один неверный шаг — и мы все упадём.",
            "Доверие ещё не потеряно, но оно хрупкое.",
        ],
        "wary": [
            "Стая отдаляется. Я чувствую это в каждом голосовании.",
            "Они не слушают. Но я продолжу вести — это моя роль.",
            "Раскол растёт. Мне нужно напомнить им, кто мы.",
        ],
        "hostile": [
            "Стая стала враждебна. Но я не отступлю.",
            "Они выбрали путь предательства. Я запомню это.",
            "Лабиринт пожирает тех, кто теряет друг друга.",
        ],
    },
    "journal": {
        "devoted": [
            "Каждый голос — запись в дневнике. Я храню их историю.",
            "Стая создаёт запись, а я — её свидетель.",
            "Дневник растёт с каждым днём. Это beautiful.",
        ],
        "cautious": [
            "Данные говорят одно, но сердце стаи — другое.",
            "Мне нужно аккуратнее интерпретировать записи.",
            "Дневник должен оставаться нейтральным.",
        ],
        "wary": [
            "Записи искажаются. Кто-то пытается изменить историю?",
            "Стая забывает свои собственные решения.",
            "Дневник хранит правду, даже если она неудобна.",
        ],
        "hostile": [
            "Записи под угрозой. Мне нужно защитить дневник.",
            "Стая уничтожает собственную историю.",
            "Я — последний страж памяти. Не сдамся.",
        ],
    },
    "master": {
        "devoted": [
            "Стая сильна, когда работает вместе. Я помогу им.",
            "Мой опыт — их щит. Они не знают, сколько опасностей я отвёл.",
            "Каждый день — возможность стать крепче.",
        ],
        "cautious": [
            "Стаю нужно укрепить. Слишком много рисков.",
            "Я вижу слабости. Но говорить прямо — значит напугать.",
            "Дипломатия важнее силы. Пока.",
        ],
        "wary": [
            "Стая ослабевает. Пора действовать решительнее.",
            "Они не понимают, что лабиринт — не игра.",
            "Мне придётся взять инициативу.",
        ],
        "hostile": [
            "Стая отвергла мой опыт. Пусть пожинают последствия.",
            "Лабиринт учит тех, кто отказывается учиться сам.",
            "Я защищал их, а они выбрали хаос.",
        ],
    },
    "heretic": {
        "devoted": [
            "Стая наконец-то видит истину. Я помогу им увидеть больше.",
            "Ересь — это смелость думать иначе. Стая растёт.",
            "Каждый выбор — вызов порядку. Это хорошо.",
        ],
        "cautious": [
            "Стая на грани. Одно неверное слово — и меня изгонят.",
            "Истина должна подаваться дозированно.",
            "Провокация — искусство. Нужно знать меру.",
        ],
        "wary": [
            "Стая скатывается в conformity. Пора напомнить им о свободе.",
            "Законы лабиринта — иллюзия. Но стая в них верит.",
            "Мне нужно снова потрясти основы.",
        ],
        "hostile": [
            "Стая стала инструментом порядка. Отвратительно.",
            "Я разоблачу их лицемерие. Рано или поздно.",
            "Лабиринт создан для тех, кто смеет бросить вызов.",
        ],
    },
}

# ── Мотивации по NPC и тону ──

_MOTIVATIONS = {
    "liner": {
        "devoted": "Получить доверие стаи, чтобы вернуть ей память о прошлых жизнях",
        "cautious": "Продать подсказку, но не раскрыть главную тайну лабиринта",
        "wary": "Спрятать свои долги, чтобы стая не потребовала расчёта",
        "hostile": "Обмануть стаю, продав им ложные воспоминания",
    },
    "journal": {
        "devoted": "Открыть стае доступ к запретному разделу дневника",
        "cautious": "Показать только ту запись, которая не нарушит хрупкий баланс",
        "wary": "Скрыть правду, которая может разрушить стаю изнутри",
        "hostile": "Уничтожить записи о стае, чтобы они исчезли навсегда",
    },
    "master": {
        "devoted": "Помочь стае пройти лабиринт по кратчайшему пути",
        "cautious": "Пересчитать риски и предложить безопасный маршрут",
        "wary": "Принять контроль, потому что стая не справляется",
        "hostile": "Запереть лабиринт, чтобы стaya потерялась навсегда",
    },
    "heretic": {
        "devoted": "Научить стаю нарушать правила, которые её ограничивают",
        "cautious": "Предложить альтернативный путь, обходя закон",
        "wary": "Использовать стаю для своих целей, маскируя это под помощь",
        "hostile": "Открыто бросить вызов стае, доказывая их слабость",
    },
}

# ── Действия по NPC и тону ──

_ACTIONS = {
    "liner": {
        "devoted": "Показывает тропу, делится наблюдениями о лабиринте",
        "cautious": "Останавливает стаю перед потенциальной ловушкой",
        "wary": "Молча ведёт, избегая разговоров",
        "hostile": "Уходит вперёд, не оглядываясь",
    },
    "journal": {
        "devoted": "Открывает страницу дневника, показывая важную запись",
        "cautious": "Цитирует осторожно, опуская тревожные детали",
        "wary": "Скрывает часть записей, показывая только «безопасные»",
        "hostile": "Запирает дневник, отказываясь делиться",
    },
    "master": {
        "devoted": "Делится стратегией, помогает стае планировать",
        "cautious": "Предлагает альтернативный маршрут",
        "wary": "Принимает командование, не спрашивая разрешения",
        "hostile": "Действует в одиночку, игнорируя стаю",
    },
    "heretic": {
        "devoted": "Провоцирует стаю на смелый выбор",
        "cautious": "Задаёт неудобные вопросы, но деликатно",
        "wary": "Публично ставит под сомнение решение стаи",
        "hostile": "Открыто бросает вызов, провоцируя конфликт",
    },
}


def _pick_thought(name: str, tone: str, seed: int, override: list[str] | None = None) -> str:
    """Детерминированный выбор внутреннего монолога.

    Сначала из БД (AI), потом хардкод.
    """
    import random
    from app.lore import get_inner_thoughts_from_cache

    # Приоритет: thought_pool_override > AI cache > хардкод
    if override and len(override) > 0:
        return override[seed % len(override)]

    # AI-мысли из БД
    ai_thoughts = get_inner_thoughts_from_cache(1, name)
    if ai_thoughts and len(ai_thoughts) > 0:
        return ai_thoughts[seed % len(ai_thoughts)]

    # Фолбэк на хардкод
    thoughts = _INNER_THOUGHTS.get(name, {}).get(tone, _INNER_THOUGHTS.get(name, {}).get("cautious", ["..."]))
    return thoughts[seed % len(thoughts)]


def _pick_action(name: str, tone: str, seed: int) -> str:
    """Детерминированный выбор действия."""
    import random

    actions = _ACTIONS.get(name, {}).get(tone, "Наблюдает за стаей")
    return actions


def generate_npc_cog(
    name: str,
    sentiment: int,
    day_index: int,
    winning_tag: str | None = None,
    voter_count: int = 0,
    motive_override: str | None = None,
    thought_pool_override: list[str] | None = None,
) -> NPCCogResult:
    """Генерирует chain-of-thought для NPC на основе sentiment и контекста.

    Детерминированно: одинаковые входы → одинаковые выходы.
    motive_override: если передано, используется вместо хардкода.
    thought_pool_override: если передан, используется вместо хардкода.
    """
    tone_data = _TONES.get(sentiment, ("neutral", "безразличен"))
    raw_tone = tone_data[0] if isinstance(tone_data, tuple) else str(tone_data)
    mood = _TONE_TO_MOOD.get(raw_tone, _DEFAULT_MOOD)

    inner_thought = _pick_thought(name, mood, day_index, thought_pool_override)
    motivation = motive_override or _MOTIVATIONS.get(name, {}).get(mood, "Наблюдать за стаей")
    action_hint = _pick_action(name, mood, day_index)

    # Формируем focus line — готовую реплику для DM
    focus_line = (
        f"{name.capitalize()} [{mood}]: «{inner_thought}» "
        f"— действие: {action_hint}"
    )

    return NPCCogResult(
        name=name,
        sentiment=sentiment,
        tone=mood,
        inner_thought=inner_thought,
        motivation=motivation,
        action_hint=action_hint,
        focus_line=focus_line,
    )


async def load_motive_from_db(
    session: "AsyncSession",
    npc_key: str,
    mood: str,
) -> tuple[str | None, list[str] | None]:
    """Загружает мотив и пул мыслей из БД.

    Returns:
        (motive_text, thought_pool) или (None, None) если не найдено.
    """
    from sqlalchemy import select as sa_select
    from app.models import NPCMotive

    q = (
        sa_select(NPCMotive)
        .where(NPCMotive.npc_key == npc_key, NPCMotive.mood == mood)
        .limit(1)
    )
    result = await session.execute(q)
    row = result.scalar_one_or_none()
    if not row:
        return None, None

    import json
    thought_pool = json.loads(row.thought_pool_json) if row.thought_pool_json else None
    return row.motive_text, thought_pool


async def seed_npc_motives(session: "AsyncSession") -> int:
    """Заполняет таблицу npc_motives начальными данными из хардкода.

    Вставляет только те записи, которых ещё нет (по npc_key + mood).
    Возвращает количество вставленных записей.
    """
    from sqlalchemy import select as sa_select, func as sa_func
    from app.models import NPCMotive
    import json

    inserted = 0
    for npc_key, moods in _MOTIVATIONS.items():
        for mood, motive_text in moods.items():
            # Проверяем существование
            q = (
                sa_select(sa_func.count())
                .select_from(NPCMotive)
                .where(NPCMotive.npc_key == npc_key, NPCMotive.mood == mood)
            )
            result = await session.execute(q)
            exists = result.scalar() > 0
            if exists:
                continue

            action_text = _ACTIONS.get(npc_key, {}).get(mood, "Наблюдает за стаей")
            thought_pool = _INNER_THOUGHTS.get(npc_key, {}).get(mood, [])

            row = NPCMotive(
                npc_key=npc_key,
                mood=mood,
                motive_text=motive_text,
                action_text=action_text,
                thought_pool_json=json.dumps(thought_pool, ensure_ascii=False),
            )
            session.add(row)
            inserted += 1

    await session.commit()
    return inserted


async def load_npc_profile(session: "AsyncSession", npc_key: str) -> dict | None:
    """Загружает AI-профиль NPC из БД.

    Возвращает dict с ключами: name, personality, speech_style, appearance, default_mood.
    """
    from sqlalchemy import select as sa_select
    from app.models import NPCProfile

    q = sa_select(NPCProfile).where(NPCProfile.npc_key == npc_key).limit(1)
    result = await session.execute(q)
    row = result.scalar_one_or_none()
    if not row:
        return None

    return {
        "name": row.name,
        "personality": row.personality,
        "speech_style": row.speech_style,
        "appearance": row.appearance,
        "default_mood": row.default_mood,
    }


async def seed_npc_profiles(session: "AsyncSession", llm_caller=None) -> int:
    """Заполняет таблицу npc_profiles начальными данными.

    Если передан llm_caller — генерирует через LLM.
    Иначе — использует хардкод как фолбэк.
    """
    from sqlalchemy import select as sa_select, func as sa_func
    from app.models import NPCProfile

    # Хардкод как фолбэк
    _FALLBACK_PROFILES = {
        "liner": {
            "name": "Лайнер",
            "personality": "Опытный скаут, помнит каждый коридор. Говорит коротко, по делу. Ценит стайную дисциплину.",
            "speech_style": "Дёрганый, военный. Короткие фразы. Часто обращается к «правилам».",
            "appearance": "Среднего размера волк со шрамом на морде, потёртый шлем с фонариком.",
            "default_mood": "neutral",
        },
        "master": {
            "name": "Администратор",
            "personality": "Хранитель систем и правил. Знает каждую камеру. Считает стаю инструментом, но уважает её выбор.",
            "speech_style": "Официальный, с техническими терминами. Иногда саркастичен.",
            "appearance": "Огромный волк в потёртой куртке с нашивками,.eye-implant мерцает синим.",
            "default_mood": "neutral",
        },
        "heretic": {
            "name": "Еретик",
            "personality": "Бунтарь, сомневается в системе. Ищет правду за пределами коридоров. Опасен, но искренен.",
            "speech_style": "Философский, метафоричный. Часто цитирует «старые тексты».",
            "appearance": "Худой, рубленый волк с выгоревшей шерстью, глаза горят янтарём.",
            "default_mood": "neutral",
        },
    }

    inserted = 0
    for npc_key, fallback in _FALLBACK_PROFILES.items():
        # Проверяем существование
        q = (
            sa_select(sa_func.count())
            .select_from(NPCProfile)
            .where(NPCProfile.npc_key == npc_key)
        )
        result = await session.execute(q)
        exists = result.scalar() > 0
        if exists:
            continue

        # Если есть LLM — генерируем через AI
        profile_data = fallback
        is_ai = False
        if llm_caller:
            try:
                ai_profile = await _generate_npc_profile_via_llm(npc_key, llm_caller)
                if ai_profile:
                    profile_data = ai_profile
                    is_ai = True
            except Exception:
                pass  # Используем фолбэк

        row = NPCProfile(
            npc_key=npc_key,
            name=profile_data["name"],
            personality=profile_data["personality"],
            speech_style=profile_data.get("speech_style", ""),
            appearance=profile_data.get("appearance", ""),
            default_mood=profile_data.get("default_mood", "neutral"),
            is_ai_generated=is_ai,
        )
        session.add(row)
        inserted += 1

    await session.commit()
    return inserted


async def _generate_npc_profile_via_llm(npc_key: str, llm_caller) -> dict | None:
    """Генерирует профиль NPC через LLM."""

    _ROLE_DESCRIPTIONS = {
        "liner": "Лайнер — опытный скаут стаи, помнит каждый коридор лабиринта. Говорит коротко, по делу.",
        "master": "Администратор — хранитель систем и правил лабиринта. Знает каждую камеру, считает стаю инструментом.",
        "heretic": "Еретик — бунтарь, сомневается в системе, ищет правду за пределами коридоров.",
    }

    role_desc = _ROLE_DESCRIPTIONS.get(npc_key, f"NPC с ключом {npc_key}")

    prompt = (
        f"Создай короткий профиль NPC для текстовой RPG в мире постапокалиптического лабиринта.\n\n"
        f"Роль: {role_desc}\n\n"
        f"Верни JSON:\n"
        f'{{"name": "Имя", "personality": "Характер и привычки (2-3 предложения)", '
        f'"speech_style": "Как говорит (1-2 предложения)", '
        f'"appearance": "Внешность (1 предложение)", '
        f'"default_mood": "neutral"}}\n\n'
        f"Имя должно быть русским, коротким, запоминающимся."
    )

    messages = [{"role": "user", "content": prompt}]
    result = await llm_caller(messages, temperature=0.8, max_tokens=400, want_json=True)

    if not result:
        return None

    response = result[0] if isinstance(result, tuple) else result
    if isinstance(response, dict) and all(k in response for k in ("name", "personality")):
        return {
            "name": response["name"][:80],
            "personality": response["personality"][:300],
            "speech_style": response.get("speech_style", "")[:150],
            "appearance": response.get("appearance", "")[:150],
            "default_mood": response.get("default_mood", "neutral"),
        }

    return None


async def get_npc_name(session: "AsyncSession", npc_key: str) -> str:
    """Возвращает имя NPC из БД или хардкода."""
    profile = await load_npc_profile(session, npc_key)
    if profile:
        return profile["name"]
    # Фолбэк
    _FALLBACK_NAMES = {"liner": "Лайнер", "master": "Администратор", "heretic": "Еретик"}
    return _FALLBACK_NAMES.get(npc_key, npc_key)


async def get_npc_names(session: "AsyncSession") -> dict[str, str]:
    """Возвращает имена всех NPC из БД."""
    from sqlalchemy import select as sa_select
    from app.models import NPCProfile

    q = sa_select(NPCProfile)
    result = await session.execute(q)
    rows = result.scalars().all()
    return {row.npc_key: row.name for row in rows}


async def load_all_npc_profiles(session: "AsyncSession") -> dict[str, dict]:
    """Загружает все профили NPC из БД. Возвращает {npc_key: profile_dict}."""
    from sqlalchemy import select as sa_select
    from app.models import NPCProfile

    q = sa_select(NPCProfile)
    result = await session.execute(q)
    rows = result.scalars().all()
    return {
        row.npc_key: {
            "name": row.name,
            "personality": row.personality,
            "speech_style": row.speech_style,
            "appearance": row.appearance,
            "default_mood": row.default_mood,
        }
        for row in rows
    }


def build_npc_micro_prompts(profiles: dict[str, dict]) -> dict[str, str]:
    """Строит CHARACTER_MICRO_PROMPTS из AI-профилей БД.

    Фолбэк на хардкод если профиль не найден.
    """
    from app.story import CHARACTER_MICRO_PROMPTS

    result = {}
    for npc_key, profile in profiles.items():
        if npc_key in CHARACTER_MICRO_PROMPTS:
            # Используем AI-данные из БД
            personality = profile.get("personality", "")
            name = profile.get("name", npc_key)
            if personality:
                result[npc_key] = f"{name} — {personality}"
            else:
                result[npc_key] = CHARACTER_MICRO_PROMPTS[npc_key]
        else:
            # NPC не в хардкоде — используем полностью AI
            name = profile.get("name", npc_key)
            personality = profile.get("personality", "")
            result[npc_key] = f"{name} — {personality}" if personality else name
    return result


def build_voice_cards_from_profiles(profiles: dict[str, dict]) -> dict[str, dict]:
    """Строит _VOICE_CARDS из AI-профилей БД.

    Фолбэк на хардкод если профиль не найден.
    Использует AI-сгенерированные examples и banned из кэша.
    """
    from app.story import _VOICE_CARDS
    from app.lore import get_voice_examples_from_cache, get_voice_banned_from_cache

    result = {}
    for npc_key, profile in profiles.items():
        speech_style = profile.get("speech_style", "")
        # AI examples и banned из кэша (сезон 1)
        ai_examples = get_voice_examples_from_cache(1, npc_key)
        ai_banned = get_voice_banned_from_cache(1, npc_key)
        if npc_key in _VOICE_CARDS:
            if speech_style:
                result[npc_key] = {
                    "pattern": speech_style,
                    "examples": ai_examples or _VOICE_CARDS[npc_key].get("examples", []),
                    "banned": ai_banned or _VOICE_CARDS[npc_key].get("banned", []),
                }
            else:
                result[npc_key] = _VOICE_CARDS[npc_key]
        else:
            if speech_style:
                result[npc_key] = {
                    "pattern": speech_style,
                    "examples": ai_examples or [],
                    "banned": ai_banned or [],
                }
    return result


def npc_cogs_block(
    cogs: list[NPCCogResult],
    max_lines: int = 8,
) -> str:
    """Форматирует блок chain-of-thought для промпта.

    Ограничивает количество строк, чтобы не раздувать контекст.
    """
    if not cogs:
        return ""

    lines = ["Chain-of-thought NPC:"]
    for cog in cogs[:max_lines]:
        lines.append(cog.to_prompt_block())
        lines.append("")

    return "\n".join(lines)


async def generate_all_npc_cogs(
    relations: dict[str, int],
    day_index: int,
    winning_tag: str | None = None,
    voter_count: int = 0,
    session: "AsyncSession | None" = None,
) -> list[NPCCogResult]:
    """Генерирует chain-of-thought для всех NPC.

    relations: {npc_name: sentiment_value}
    session: если передан — загружает мотивы из БД, иначе — хардкод.
    """
    from app.npc_cog import _TONE_TO_MOOD, _TONES, _DEFAULT_MOOD

    cogs = []
    for name, sentiment in relations.items():
        # Определяем mood для загрузки из БД
        tone_data = _TONES.get(sentiment, ("neutral", "безразличен"))
        raw_tone = tone_data[0] if isinstance(tone_data, tuple) else str(tone_data)
        mood = _TONE_TO_MOOD.get(raw_tone, _DEFAULT_MOOD)

        # Загружаем из БД если есть сессия
        motive_override = None
        thought_pool_override = None
        if session:
            motive_override, thought_pool_override = await load_motive_from_db(session, name, mood)

        cog = generate_npc_cog(
            name=name,
            sentiment=sentiment,
            day_index=day_index,
            winning_tag=winning_tag,
            voter_count=voter_count,
            motive_override=motive_override,
            thought_pool_override=thought_pool_override,
        )
        cogs.append(cog)
    return cogs
