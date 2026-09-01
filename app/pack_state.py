"""Состояние стаи: голод, жажда, здоровье.

Параметры растут/падают каждый день. Достигнув экстремумов —
триггер смерти (пермадет) или дебаффы на голосование.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PackState


@dataclass
class PackNeeds:
    """Потребности стаи."""
    hunger: int = 5      # 0 = сытые, 10 = голодные
    thirst: int = 5      # 0 = польные, 10 = жаждущие
    health: int = 10     # 10 = здоровые, 0 = мёртвые
    alive_count: int = 5 # Сколько собак ещё живы

    def clamp(self) -> None:
        self.hunger = max(0, min(10, self.hunger))
        self.thirst = max(0, min(10, self.thirst))
        self.health = max(0, min(10, self.health))
        self.alive_count = max(0, min(5, self.alive_count))


# Сдвиги потребностей по тегу победившей карты
NEED_SHIFTS: dict[str, dict[str, int]] = {
    "risk":   {"hunger": +1, "thirst": +1, "health": -1},
    "care":   {"hunger": -2, "thirst": -1, "health": +1},
    "cunning": {"hunger": +0, "thirst": +1, "health": +0},
}


def apply_needs_shift(needs: PackNeeds, tag: str | None) -> None:
    """Применяет сдвиг потребностей по тегу карты."""
    if tag is None or tag not in NEED_SHIFTS:
        return
    shifts = NEED_SHIFTS[tag]
    needs.hunger += shifts.get("hunger", 0)
    needs.thirst += shifts.get("thirst", 0)
    needs.health += shifts.get("health", 0)
    needs.clamp()


def get_needs_phase(needs: PackNeeds) -> str:
    """Определяет фазу потребностей."""
    if needs.health <= 0:
        return "dead"
    if needs.health <= 3:
        return "dying"
    if needs.hunger >= 8 and needs.thirst >= 8:
        return "desperate"
    if needs.hunger >= 7 or needs.thirst >= 7:
        return "struggling"
    if needs.hunger >= 5 or needs.thirst >= 5:
        return "hungry"
    return "sated"


def get_needs_debuffs(needs: PackNeeds) -> dict[str, float]:
    """Возвращает дебаффы на основе потребностей."""
    debuffs = {}

    if needs.hunger >= 8:
        debuffs["risk_penalty"] = -0.2
        debuffs["care_bonus"] = 0.1
    elif needs.hunger >= 5:
        debuffs["risk_penalty"] = -0.1

    if needs.thirst >= 8:
        debuffs["cunning_penalty"] = -0.2
        debuffs["care_bonus"] = debuffs.get("care_bonus", 0) + 0.1
    elif needs.thirst >= 5:
        debuffs["cunning_penalty"] = -0.1

    if needs.health <= 3:
        debuffs["risk_penalty"] = debuffs.get("risk_penalty", 0) - 0.3
        debuffs["care_penalty"] = -0.2
    elif needs.health <= 5:
        debuffs["risk_penalty"] = debuffs.get("risk_penalty", 0) - 0.1

    return debuffs


def get_needs_block(needs: PackNeeds) -> str | None:
    """Возвращает блок для промпта главы. None — всё нормально."""
    phase = get_needs_phase(needs)

    if phase == "dead":
        return (
            "ПОТРЕБНОСТИ СТАИ: ВСЕ ПОГИБЛИ. Стая мертва. "
            "Это конец. Опиши финальную сцену."
        )

    if phase == "dying":
        return (
            f"ПОТРЕБНОСТИ СТАИ: КРИТИЧЕСКОЕ СОСТОЯНИЕ. "
            f"Здоровье: {needs.health}/10. "
            f"Собаки слабеют. Мир становится тяжелее. "
            f"Опиши, как стая борется за выживание."
        )

    if phase == "desperate":
        return (
            f"ПОТРЕБНОСТИ СТАИ: КРИТИЧЕСКАЯ НУЖДА. "
            f"Голод: {needs.hunger}/10, жажда: {needs.thirst}/10. "
            f"Собаки измотаны. Каждый шаг даётся с трудом."
        )

    if phase == "struggling":
        lines = []
        if needs.hunger >= 7:
            lines.append(f"голод: {needs.hunger}/10")
        if needs.thirst >= 7:
            lines.append(f"жажда: {needs.thirst}/10")
        if lines:
            return (
                f"ПОТРЕБНОСТИ СТАИ: НУЖДА. "
                f"{' и '.join(lines)}. "
                f"Стая чувствует тяжесть мира."
            )

    if phase == "hungry":
        lines = []
        if needs.hunger >= 5:
            lines.append(f"голод: {needs.hunger}/10")
        if needs.thirst >= 5:
            lines.append(f"жажда: {needs.thirst}/10")
        if lines:
            return (
                f"ПОТРЕБНОСТИ СТАИ: ЛЁГКИЙ НАПОР. "
                f"{' и '.join(lines)}. "
                f"Собаки чувствуют потребности."
            )

    return None


async def load_pack_state(session: AsyncSession) -> PackNeeds:
    """Загружает состояние стаи из БД."""
    result = await session.execute(select(PackState).limit(1))
    row = result.scalar_one_or_none()
    if row is None:
        return PackNeeds()
    return PackNeeds(
        hunger=row.hunger,
        thirst=row.thirst,
        health=row.health,
        alive_count=row.alive_count,
    )


async def save_pack_state(
    session: AsyncSession,
    needs: PackNeeds,
    current_day: int,
) -> None:
    """Сохраняет состояние стаи в БД."""
    result = await session.execute(select(PackState).limit(1))
    row = result.scalar_one_or_none()
    if row is None:
        row = PackState(
            hunger=needs.hunger,
            thirst=needs.thirst,
            health=needs.health,
            alive_count=needs.alive_count,
            last_updated_day=current_day,
        )
        session.add(row)
    else:
        row.hunger = needs.hunger
        row.thirst = needs.thirst
        row.health = needs.health
        row.alive_count = needs.alive_count
        row.last_updated_day = current_day
    await session.flush()


async def process_round_needs(
    session: AsyncSession,
    winner_tag: str | None,
    current_day: int,
) -> PackNeeds:
    """Обновляет потребности после раунда. Возвращает текущее состояние."""
    needs = await load_pack_state(session)
    apply_needs_shift(needs, winner_tag)
    await save_pack_state(session, needs, current_day)
    return needs


def check_death(needs: PackNeeds) -> bool:
    """Проверяет, погибла ли стая."""
    return needs.health <= 0
