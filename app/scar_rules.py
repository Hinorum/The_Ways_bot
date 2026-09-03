"""Правила создания и применения шрамов мира.

Шрамы — это следы от значимых выборов стаи, меняющие лабиринт:
- Блокируют локации (сожжённый мост исчезает из пула)
- Разблокируют новые (тёплый очаг открывает укрытие)
- Меняют тон атмосферы (дым, тревога, тепло)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WorldScar


@dataclass
class ScarRule:
    """Правило создания шрама: условие → эффект."""
    trigger_tag: str
    min_streak: int = 1
    scar_key: str = ""
    effect_type: str = ""  # "block_place", "unlock_place", "modify_tone"
    duration_days: int | None = 5  # None = бессрочно
    strength_gain: int = 1
    description: str = ""


SCAR_RULES: list[ScarRule] = [
    # RISK шрамы
    ScarRule(
        trigger_tag="risk", min_streak=2, scar_key="burned_path",
        effect_type="block_place", duration_days=7, strength_gain=1,
        description="Сожжённый путь: стая сжигает мосты, и мир запоминает",
    ),
    ScarRule(
        trigger_tag="risk", min_streak=5, scar_key="scorched_earth",
        effect_type="block_place", duration_days=14, strength_gain=2,
        description="Выжженная земля: слишком много мостов сожжено",
    ),
    ScarRule(
        trigger_tag="risk", min_streak=1, scar_key="fresh_wound",
        effect_type="modify_tone", duration_days=3, strength_gain=1,
        description="Свежая рана: мир помнит боль",
    ),

    # CARE шрамы
    ScarRule(
        trigger_tag="care", min_streak=3, scar_key="warm_hearth",
        effect_type="unlock_place", duration_days=None, strength_gain=1,
        description="Тёплый очаг: стая создала место, куда хочется возвращаться",
    ),
    ScarRule(
        trigger_tag="care", min_streak=5, scar_key="sanctuary",
        effect_type="unlock_place", duration_days=None, strength_gain=2,
        description="Святилище: стая стала домом для других",
    ),
    ScarRule(
        trigger_tag="care", min_streak=1, scar_key="gentle_breath",
        effect_type="modify_tone", duration_days=3, strength_gain=1,
        description="Мягкое дыхание: мир стал теплее",
    ),

    # CUNNING шрамы
    ScarRule(
        trigger_tag="cunning", min_streak=2, scar_key="labyrinth_doubt",
        effect_type="modify_tone", duration_days=7, strength_gain=1,
        description="Сомнение лабиринта: коридоры начинают дублироваться",
    ),
    ScarRule(
        trigger_tag="cunning", min_streak=4, scar_key="false_trails",
        effect_type="unlock_place", duration_days=10, strength_gain=2,
        description="Ложные тропы: хитрость открыла новые коридоры",
    ),
    ScarRule(
        trigger_tag="cunning", min_streak=1, scar_key="whisper_of_trick",
        effect_type="modify_tone", duration_days=3, strength_gain=1,
        description="Шёпот обмана: кто-то считает дни иначе",
    ),
]


def check_streak_for_scar(
    history_tags: list[str], rule: ScarRule, current_day: int
) -> ScarRule | None:
    """Проверяет, сработало ли правило шрама по стрику тегов."""
    if len(history_tags) < rule.min_streak:
        return None
    recent = history_tags[-rule.min_streak:]
    if all(tag == rule.trigger_tag for tag in recent):
        return rule
    return None


def get_active_scars_for_day(
    scars: list[WorldScar], day_index: int
) -> list[WorldScar]:
    """Возвращает активные шрамы на указанный день."""
    active = []
    for scar in scars:
        if scar.expires_day is not None and day_index > scar.expires_day:
            continue
        active.append(scar)
    return active


def get_blocked_places(scars: list[WorldScar]) -> set[str]:
    """Возвращает множество локаций, заблокированных шрамами."""
    blocked = set()
    for scar in scars:
        if scar.scar_key in ("burned_path", "scorched_earth"):
            blocked.add(scar.metadata_json or "мост")
    return blocked


def get_unlocked_places(scars: list[WorldScar]) -> set[str]:
    """Возвращает множество локаций, разблокированных шрамами."""
    unlocked = set()
    for scar in scars:
        if scar.scar_key in ("warm_hearth", "sanctuary", "false_trails"):
            if scar.metadata_json:
                unlocked.add(scar.metadata_json)
    return unlocked


def get_tone_modifiers(scars: list[WorldScar]) -> list[str]:
    """Возвращает модификаторы тона от активных шрамов."""
    modifiers = []
    for scar in scars:
        if scar.scar_key == "fresh_wound":
            modifiers.append("wound")
        elif scar.scar_key == "gentle_breath":
            modifiers.append("warmth")
        elif scar.scar_key == "labyrinth_doubt":
            modifiers.append("doubt")
        elif scar.scar_key == "whisper_of_trick":
            modifiers.append("trick")
    return modifiers


async def create_scar(
    session: AsyncSession,
    rule: ScarRule,
    current_day: int,
    metadata: str | None = None,
) -> WorldScar:
    """Создаёт новый шрам в базе данных."""
    expires = current_day + rule.duration_days if rule.duration_days else None
    scar = WorldScar(
        scar_key=rule.scar_key,
        created_day=current_day,
        expires_day=expires,
        strength=rule.strength_gain,
        metadata_json=metadata,
    )
    session.add(scar)
    await session.flush()
    return scar


async def load_active_scars(session: AsyncSession, current_day: int) -> list[WorldScar]:
    """Загружает все активные шрамы на указанный день."""
    result = await session.execute(select(WorldScar))
    all_scars = list(result.scalars().all())
    return get_active_scars_for_day(all_scars, current_day)


async def process_round_scars(
    session: AsyncSession,
    winner_tag: str | None,
    history_tags: list[str],
    current_day: int,
) -> list[WorldScar]:
    """Обрабатывает шрамы после завершения раунда. Возвращает новые шрамы."""
    if winner_tag is None:
        return []

    new_scars = []
    for rule in SCAR_RULES:
        triggered = check_streak_for_scar(history_tags, rule, current_day)
        if triggered is None:
            continue
        
        # Проверяем, нет ли уже такого шрама за последние 3 дня
        # Используем limit(1) и scalar() вместо scalar_one_or_none()
        # чтобы избежать ошибки MultipleResultsFound
        existing = await session.execute(
            select(WorldScar.id)
            .where(
                WorldScar.scar_key == rule.scar_key,
                WorldScar.created_day >= current_day - 3,
            )
            .order_by(WorldScar.created_day.desc())
            .limit(1)
        )
        
        # scalar() вернёт первый id или None, если записей нет
        existing_scar_id = existing.scalar()
        
        if existing_scar_id is not None:
            # Такой шрам уже есть — пропускаем
            continue
        
        # Создаём новый шрам
        scar = await create_scar(session, rule, current_day)
        new_scars.append(scar)

    return new_scars
