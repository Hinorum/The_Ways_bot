"""AI Regeneration: замена хардкод-фолбэков на AI-данные.

Вызывается при каждом буте. Если в БД есть записи с is_ai_generated=False,
пытается перегенерировать их через LLM и обновить.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def regenerate_all(session: "AsyncSession", llm_caller, season: int = 1) -> dict[str, int]:
    """Пытается перегенерировать все хардкод-фолбэки через LLM.

    Возвращает {pool_type: count_updated}.
    """
    results = {}

    # 1. AIGeneratedPool (atmospheric, voice_examples, voice_banned, inner_thoughts, villain_events, heretic_events)
    pool_count = await _regenerate_pools(session, llm_caller, season)
    if pool_count:
        results["pools"] = pool_count

    # 2. NPCProfile
    npc_count = await _regenerate_npc_profiles(session, llm_caller)
    if npc_count:
        results["npc_profiles"] = npc_count

    # 3. PrologueBeat
    prologue_count = await _regenerate_prologue_beats(session, llm_caller, season)
    if prologue_count:
        results["prologue_beats"] = prologue_count

    # 4. SeasonArc
    arc_count = await _regenerate_season_arcs(session, llm_caller, season)
    if arc_count:
        results["season_arcs"] = arc_count

    total = sum(results.values())
    if total:
        logger.info("AI Regeneration: обновлено %d записей: %s", total, results)
    else:
        logger.debug("AI Regeneration: все данные уже AI-сгенерированы")

    return results


async def _regenerate_pools(session: "AsyncSession", llm_caller, season: int) -> int:
    """Перегенерирует AIGeneratedPool записи с is_ai_generated=False."""
    from sqlalchemy import select as sa_select
    from sqlalchemy import update as sa_update
    from app.models import AIGeneratedPool

    q = sa_select(AIGeneratedPool).where(
        AIGeneratedPool.is_ai_generated == False,  # noqa: E712
        AIGeneratedPool.season == season,
    )
    result = await session.execute(q)
    rows = result.scalars().all()

    if not rows:
        return 0

    updated = 0
    for row in rows:
        try:
            new_content = await _regenerate_pool_row(row, llm_caller)
            if new_content:
                row.content_json = json.dumps(new_content, ensure_ascii=False)
                row.is_ai_generated = True
                updated += 1
        except Exception as e:
            logger.debug("AI Regeneration: ошибка для %s/%s: %s", row.pool_type, row.phase, e)

    if updated:
        await session.commit()
    return updated


async def _regenerate_pool_row(row, llm_caller) -> list | None:
    """Перегенерирует одну запись AIGeneratedPool."""
    from app.lore import (
        _generate_atmospheric_via_llm,
        _generate_voice_examples_via_llm,
        _generate_voice_banned_via_llm,
        _generate_inner_thoughts_via_llm,
        _generate_dog_pad_via_llm,
        _generate_echo_tones_via_llm,
        _generate_weather_via_llm,
        _generate_place_via_llm,
    )
    from app.season import (
        _generate_villain_events_via_llm,
        _generate_heretic_events_via_llm,
    )

    def _parse_phase(phase_str: str):
        """Parse phase string for dog_pads (npc_key:phase) or plain phase."""
        if ":" in phase_str:
            return phase_str.split(":", 1)
        return phase_str, None

    phase_main, phase_sub = _parse_phase(row.phase)

    generators = {
        "atmospheric": lambda: _generate_atmospheric_via_llm(phase_main, llm_caller),
        "voice_examples": lambda: _generate_voice_examples_via_llm(phase_main, llm_caller),
        "voice_banned": lambda: _generate_voice_banned_via_llm(phase_main, llm_caller),
        "inner_thoughts": lambda: _generate_inner_thoughts_via_llm(phase_main, llm_caller),
        "dog_pads": lambda: _generate_dog_pad_via_llm(phase_main, phase_sub, llm_caller),
        "echo_tones": lambda: _generate_echo_tones_via_llm(phase_main, llm_caller),
        "weather_pool": lambda: _generate_weather_via_llm(llm_caller),
        "places": lambda: _generate_place_via_llm(int(phase_main), llm_caller),
        "villain_events": lambda: _generate_villain_events_via_llm(int(phase_main), llm_caller),
        "heretic_events": lambda: _generate_heretic_events_via_llm(int(phase_main), llm_caller),
    }

    min_items = {
        "atmospheric": 10,
        "voice_examples": 3,
        "voice_banned": 2,
        "inner_thoughts": 3,
        "dog_pads": 1,
        "echo_tones": 3,
        "weather_pool": 3,
        "places": 1,
        "villain_events": 3,
        "heretic_events": 3,
    }

    gen = generators.get(row.pool_type)
    if not gen:
        return None

    new_content = await gen()
    if new_content and len(new_content) >= min_items.get(row.pool_type, 3):
        return new_content
    return None


async def _regenerate_npc_profiles(session: "AsyncSession", llm_caller) -> int:
    """Перегенерирует NPCProfile записи с is_ai_generated=False."""
    from sqlalchemy import select as sa_select
    from app.models import NPCProfile
    from app.npc_cog import _generate_npc_profile_via_llm

    q = sa_select(NPCProfile).where(NPCProfile.is_ai_generated == False)  # noqa: E712
    result = await session.execute(q)
    rows = result.scalars().all()

    if not rows:
        return 0

    updated = 0
    for row in rows:
        try:
            ai_profile = await _generate_npc_profile_via_llm(row.npc_key, llm_caller)
            if ai_profile:
                row.name = ai_profile["name"]
                row.personality = ai_profile["personality"]
                row.speech_style = ai_profile.get("speech_style", "")
                row.appearance = ai_profile.get("appearance", "")
                row.default_mood = ai_profile.get("default_mood", "neutral")
                row.is_ai_generated = True
                updated += 1
        except Exception as e:
            logger.debug("AI Regeneration: NPC %s ошибка: %s", row.npc_key, e)

    if updated:
        await session.commit()
    return updated


async def _regenerate_prologue_beats(session: "AsyncSession", llm_caller, season: int) -> int:
    """Перегенерирует PrologueBeat записи с is_ai_generated=False."""
    from sqlalchemy import select as sa_select
    from app.models import PrologueBeat
    from app.prologue import _generate_prologue_beat_via_llm

    q = sa_select(PrologueBeat).where(
        PrologueBeat.is_ai_generated == False,  # noqa: E712
        PrologueBeat.season == season,
    )
    result = await session.execute(q)
    rows = result.scalars().all()

    if not rows:
        return 0

    updated = 0
    for row in rows:
        try:
            ai_beat = await _generate_prologue_beat_via_llm(row.day_index, season, llm_caller)
            if ai_beat:
                row.title = ai_beat["title"]
                row.block = ai_beat["block"]
                row.is_ai_generated = True
                updated += 1
        except Exception as e:
            logger.debug("AI Regeneration: prologue day %d ошибка: %s", row.day_index, e)

    if updated:
        await session.commit()
    return updated


async def _regenerate_season_arcs(session: "AsyncSession", llm_caller, season: int) -> int:
    """Перегенерирует SeasonArc записи с is_ai_generated=False."""
    from sqlalchemy import select as sa_select
    from app.models import SeasonArc
    from app.story_arc import _generate_season_stage_via_llm

    q = sa_select(SeasonArc).where(
        SeasonArc.is_ai_generated == False,  # noqa: E712
        SeasonArc.season == season,
    )
    result = await session.execute(q)
    rows = result.scalars().all()

    if not rows:
        return 0

    updated = 0
    for row in rows:
        try:
            ai_stage = await _generate_season_stage_via_llm(row.stage_index, season, llm_caller)
            if ai_stage:
                row.name = ai_stage["name"]
                row.purpose = ai_stage["purpose"]
                row.tone = ai_stage.get("tone", "")
                row.missions_json = json.dumps(ai_stage.get("missions", []), ensure_ascii=False)
                row.whisper_json = json.dumps(ai_stage.get("whisper", []), ensure_ascii=False)
                row.teaser_json = json.dumps(ai_stage.get("teaser", []), ensure_ascii=False)
                row.guest = ai_stage.get("guest", "")
                row.is_ai_generated = True
                updated += 1
        except Exception as e:
            logger.debug("AI Regeneration: season arc stage %d ошибка: %s", row.stage_index, e)

    if updated:
        await session.commit()
    return updated
