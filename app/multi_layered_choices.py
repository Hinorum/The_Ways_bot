"""Многослойные выборы — недельные и месячные голосования.

Помимо ежедневных выборов, стая делает:
- Недельные: голосование за NPC-партнёра на следующую неделю
- Месячные: голосование за клятву (присягу) на месяц

Это создаёт стратегическое планирование: стая решает,
на кого делать ставку и какие обязательства брать.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Player


class ChoiceType(Enum):
    """Тип выбора."""
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass
class NPCPartner:
    """NPC-партнёр для недельного голосования."""
    key: str
    name: str
    description: str
    passive_bonus: str  # Бонус за выбор этого NPC
    active_ability: str  # Активная способность (раз в неделю)


@dataclass
class Oath:
    """Клятва для месячного голосования."""
    key: str
    name: str
    description: str
    requirement: str  # Требование для выполнения
    reward: str  # Награда за выполнение
    penalty: str  # Штраф за невыполнение


# NPC-партнёры для недельного голосования
NPC_PARTNERS: dict[str, NPCPartner] = {
    "wanderer": NPCPartner(
        key="wanderer",
        name="Странник",
        description="Тот, кто ходит между стаями и знает дороги",
        passive_bonus="+1 к cunning-картам каждый день",
        active_ability="Разведка: показать 1 скрытую карту",
    ),
    "guardian": NPCPartner(
        key="guardian",
        name="Страж",
        description="Тот, кто защищает и оберегает",
        passive_bonus="+1 к care-картам каждый день",
        active_ability="Щит: отменить 1 негативное последствие",
    ),
    "trickster": NPCPartner(
        key="trickster",
        name="Обманщик",
        description="Тот, кто меняет правила игры",
        passive_bonus="+1 к risk-картам каждый день",
        active_ability="Иллюзия: изменить 1 карту перед голосованием",
    ),
    "healer": NPCPartner(
        key="healer",
        name="Целитель",
        description="Тот, кто лечит раны и души",
        passive_bonus="-1 к fatigue каждый день",
        active_ability="Восстановление: снять 1 шрам мира",
    ),
    "sage": NPCPartner(
        key="sage",
        name="Мудрец",
        description="Тот, кто помнит прошлое и видит будущее",
        passive_bonus="+1 к hope каждый день",
        active_ability="Видение: показать 1 будущий шрам",
    ),
}


# Клятвы для месячного голосования
MONTHLY_OATHS: dict[str, Oath] = {
    "protect_weak": Oath(
        key="protect_weak",
        name="Клятва защиты",
        description="Защищать слабых и бездомных",
        requirement="7 дней подряд выбирать care-карты",
        reward="+5 hope, разблокировка святилища",
        penalty="-3 hope, fatigue +2",
    ),
    "seek_truth": Oath(
        key="seek_truth",
        name="Клятва истины",
        description="Искать правду, даже если она болезненна",
        requirement="5 дней подряд выбирать cunning-карты",
        reward="+3 cunning, разблокировка архива",
        penalty="-2 cunning, paranoia +2",
    ),
    "face_danger": Oath(
        key="face_danger",
        name="Клятва храбрости",
        description="Смело идти навстречу опасности",
        requirement="6 дней подряд выбирать risk-карты",
        reward="+3 risk, разблокировка тайников",
        penalty="-2 risk, fatigue +3",
    ),
    "balance_all": Oath(
        key="balance_all",
        name="Клятва равновесия",
        description="Держать баланс между всеми путями",
        requirement="Не более 3 побед одного типа за месяц",
        reward="+2 ко всем характеристикам",
        penalty="-1 ко всем характеристикам",
    ),
    "endure_all": Oath(
        key="endure_all",
        name="Клятва стойкости",
        description="Преодолеть любые трудности",
        requirement="Пережить 3 шрама мира за месяц",
        reward="+5 fatigue resistance,免疫 к exhaustion",
        penalty="fatigue +5, полное истощение",
    ),
}


@dataclass
class WeeklyVote:
    """Недельное голосование."""
    week_number: int
    partner_key: str
    votes: dict[int, str] = field(default_factory=dict)  # player_id → partner_key


@dataclass
class MonthlyVote:
    """Месячное голосование."""
    month_number: int
    oath_key: str
    votes: dict[int, str] = field(default_factory=dict)  # player_id → oath_key


async def get_available_partners(session: AsyncSession) -> list[NPCPartner]:
    """Возвращает доступных NPC-партнёров."""
    return list(NPC_PARTNERS.values())


async def get_available_oaths(session: AsyncSession) -> list[Oath]:
    """Возвращает доступные клятвы."""
    return list(MONTHLY_OATHS.values())


async def cast_weekly_vote(
    session: AsyncSession,
    week_number: int,
    player_id: int,
    partner_key: str,
) -> str:
    """Голосует за NPC-партнёра на неделю."""
    if partner_key not in NPC_PARTNERS:
        return "Неизвестный NPC-партнёр"

    # Здесь будет логика сохранения голоса в БД
    # Пока просто подтверждаем
    return f"Голос за {NPC_PARTNERS[partner_key].name} принят"


async def cast_monthly_vote(
    session: AsyncSession,
    month_number: int,
    player_id: int,
    oath_key: str,
) -> str:
    """Голосует за клятву на месяц."""
    if oath_key not in MONTHLY_OATHS:
        return "Неизвестная клятва"

    # Здесь будет логика сохранения голоса в БД
    # Пока просто подтверждаем
    return f"Голос за {MONTHLY_OATHS[oath_key].name} принят"


def get_weekly_choice_text() -> str:
    """Форматирует текст недельного выбора."""
    lines = ["ГОЛОСОВАНИЕ НА НЕДЕЛЮ:"]
    lines.append("Выберите NPC-партнёра на следующую неделю:")
    lines.append("")

    for key, partner in NPC_PARTNERS.items():
        lines.append(f"• {partner.name}: {partner.description}")
        lines.append(f"  Пассивный бонус: {partner.passive_bonus}")
        lines.append(f"  Активная способность: {partner.active_ability}")
        lines.append("")

    return "\n".join(lines)


def get_monthly_choice_text() -> str:
    """Форматирует текст месячного выбора."""
    lines = ["ГОЛОСОВАНИЕ НА МЕСЯЦ:"]
    lines.append("Выберите клятву на месяц:")
    lines.append("")

    for key, oath in MONTHLY_OATHS.items():
        lines.append(f"• {oath.name}: {oath.description}")
        lines.append(f"  Требование: {oath.requirement}")
        lines.append(f"  Награда: {oath.reward}")
        lines.append(f"  Штраф: {oath.penalty}")
        lines.append("")

    return "\n".join(lines)


def get_active_partner_bonuses(partner_key: str) -> dict[str, int]:
    """Возвращает пассивные бонусы от выбранного NPC-партнёра."""
    if partner_key not in NPC_PARTNERS:
        return {}

    partner = NPC_PARTNERS[partner_key]
    bonuses = {}

    if "+1 к cunning" in partner.passive_bonus:
        bonuses["cunning"] = 1
    elif "+1 к care" in partner.passive_bonus:
        bonuses["care"] = 1
    elif "+1 к risk" in partner.passive_bonus:
        bonuses["risk"] = 1
    elif "-1 к fatigue" in partner.passive_bonus:
        bonuses["fatigue"] = -1
    elif "+1 к hope" in partner.passive_bonus:
        bonuses["hope"] = 1

    return bonuses


def check_oath_completion(oath_key: str, stats: dict[str, int]) -> bool:
    """Проверяет, выполнена ли клятва."""
    if oath_key not in MONTHLY_OATHS:
        return False

    oath = MONTHLY_OATHS[oath_key]

    if oath_key == "protect_weak":
        return stats.get("care_streak", 0) >= 7
    elif oath_key == "seek_truth":
        return stats.get("cunning_streak", 0) >= 5
    elif oath_key == "face_danger":
        return stats.get("risk_streak", 0) >= 6
    elif oath_key == "balance_all":
        max_count = max(stats.get("risk_count", 0), stats.get("care_count", 0), stats.get("cunning_count", 0))
        return max_count <= 3
    elif oath_key == "endure_all":
        return stats.get("scars_endured", 0) >= 3

    return False
