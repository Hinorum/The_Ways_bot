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

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WeeklyVote, MonthlyOath


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
    tag_bonus: dict[str, int] = field(default_factory=dict)  # tag → modifier


@dataclass
class Oath:
    """Клятва для месячного голосования."""
    key: str
    name: str
    description: str
    requirement: str  # Требование для выполнения
    reward: str  # Награда за выполнение
    penalty: str  # Штраф за невыполнение
    check_stats: dict[str, int] = field(default_factory=dict)  # stat → required


# NPC-партнёры для недельного голосования
NPC_PARTNERS: dict[str, NPCPartner] = {
    "wanderer": NPCPartner(
        key="wanderer",
        name="Странник",
        description="Тот, кто ходит между стаями и знает дороги",
        passive_bonus="+1 к cunning-картам каждый день",
        active_ability="Разведка: показать 1 скрытую карту",
        tag_bonus={"cunning": 1},
    ),
    "guardian": NPCPartner(
        key="guardian",
        name="Страж",
        description="Тот, кто защищает и оберегает",
        passive_bonus="+1 к care-картам каждый день",
        active_ability="Щит: отменить 1 негативное последствие",
        tag_bonus={"care": 1},
    ),
    "trickster": NPCPartner(
        key="trickster",
        name="Обманщик",
        description="Тот, кто меняет правила игры",
        passive_bonus="+1 к risk-картам каждый день",
        active_ability="Иллюзия: изменить 1 карту перед голосованием",
        tag_bonus={"risk": 1},
    ),
    "healer": NPCPartner(
        key="healer",
        name="Целитель",
        description="Тот, кто лечит раны и души",
        passive_bonus="Восстановление 1 HP каждый день",
        active_ability="Восстановление: снять 1 шрам мира",
        tag_bonus={"health": 1},
    ),
    "sage": NPCPartner(
        key="sage",
        name="Мудрец",
        description="Тот, кто помнит прошлое и видит будущее",
        passive_bonus="+1 к hope каждый день",
        active_ability="Видение: показать 1 будущий шрам",
        tag_bonus={"hope": 1},
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
        check_stats={"care_streak": 7},
    ),
    "seek_truth": Oath(
        key="seek_truth",
        name="Клятва истины",
        description="Искать правду, даже если она болезненна",
        requirement="5 дней подряд выбирать cunning-карты",
        reward="+3 cunning, разблокировка архива",
        penalty="-2 cunning, paranoia +2",
        check_stats={"cunning_streak": 5},
    ),
    "face_danger": Oath(
        key="face_danger",
        name="Клятва храбрости",
        description="Смело идти навстречу опасности",
        requirement="6 дней подряд выбирать risk-карты",
        reward="+3 risk, разблокировка тайников",
        penalty="-2 risk, fatigue +3",
        check_stats={"risk_streak": 6},
    ),
    "balance_all": Oath(
        key="balance_all",
        name="Клятва равновесия",
        description="Держать баланс между всеми путями",
        requirement="Не более 3 побед одного типа за месяц",
        reward="+2 ко всем характеристикам",
        penalty="-1 ко всем характеристикам",
        check_stats={"max_tag_count": 3},
    ),
    "endure_all": Oath(
        key="endure_all",
        name="Клятва стойкости",
        description="Преодолеть любые трудности",
        requirement="Пережить 3 шрама мира за месяц",
        reward="+5 fatigue resistance, иммунитет к exhaustion",
        penalty="fatigue +5, полное истощение",
        check_stats={"scars_endured": 3},
    ),
}


async def get_week_number(session: AsyncSession) -> int:
    """Возвращает номер текущей недели (начиная с 1)."""
    result = await session.execute(
        select(func.max(WeeklyVote.week_number))
    )
    max_week = result.scalar() or 0
    return max_week + 1


async def get_month_number(session: AsyncSession) -> int:
    """Возвращает номер текущего месяца (начиная с 1)."""
    result = await session.execute(
        select(func.max(MonthlyOath.month_number))
    )
    max_month = result.scalar() or 0
    return max_month + 1


async def cast_weekly_vote(
    session: AsyncSession,
    week_number: int,
    player_id: int,
    partner_key: str,
) -> str:
    """Голосует за NPC-партнёра на неделю."""
    if partner_key not in NPC_PARTNERS:
        return "Неизвестный NPC-партнёр"

    # Проверяем, не голосовал ли уже игрок на этой неделе
    existing = await session.execute(
        select(WeeklyVote).where(
            WeeklyVote.week_number == week_number,
            WeeklyVote.player_id == player_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return "Вы уже проголосовали на этой неделе"

    vote = WeeklyVote(
        week_number=week_number,
        player_id=player_id,
        partner_key=partner_key,
    )
    session.add(vote)
    await session.flush()
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

    existing = await session.execute(
        select(MonthlyOath).where(
            MonthlyOath.month_number == month_number,
            MonthlyOath.player_id == player_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return "Вы уже проголосовали за клятву в этом месяце"

    vote = MonthlyOath(
        month_number=month_number,
        player_id=player_id,
        oath_key=oath_key,
    )
    session.add(vote)
    await session.flush()
    return f"Голос за {MONTHLY_OATHS[oath_key].name} принят"


async def get_weekly_winner(
    session: AsyncSession,
    week_number: int,
) -> str | None:
    """Определяет победителя недели по числу голосов."""
    result = await session.execute(
        select(WeeklyVote.partner_key, func.count(WeeklyVote.id))
        .where(WeeklyVote.week_number == week_number)
        .group_by(WeeklyVote.partner_key)
        .order_by(func.count(WeeklyVote.id).desc())
        .limit(1)
    )
    row = result.first()
    return row[0] if row else None


async def get_monthly_winner(
    session: AsyncSession,
    month_number: int,
) -> str | None:
    """Определяет победителя месяца по числу голосов."""
    result = await session.execute(
        select(MonthlyOath.oath_key, func.count(MonthlyOath.id))
        .where(MonthlyOath.month_number == month_number)
        .group_by(MonthlyOath.oath_key)
        .order_by(func.count(MonthlyOath.id).desc())
        .limit(1)
    )
    row = result.first()
    return row[0] if row else None


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
    return NPC_PARTNERS[partner_key].tag_bonus.copy()


def format_partner_block(partner_key: str | None) -> str:
    """Форматирует блок активного NPC-партнёра для промпта."""
    if partner_key is None or partner_key not in NPC_PARTNERS:
        return ""

    partner = NPC_PARTNERS[partner_key]
    return (
        f"NPC-ПАРТНЁР НА ЭТУ НЕДЕЛЮ: {partner.name}\n"
        f"{partner.description}\n"
        f"Бонус: {partner.passive_bonus}\n"
        f"Способность: {partner.active_ability}"
    )


def format_oath_block(oath_key: str | None) -> str:
    """Форматирует блок активной клятвы для промпта."""
    if oath_key is None or oath_key not in MONTHLY_OATHS:
        return ""

    oath = MONTHLY_OATHS[oath_key]
    return (
        f"КЛЯТВА НА ЭТОТ МЕСЯЦ: {oath.name}\n"
        f"{oath.description}\n"
        f"Требование: {oath.requirement}\n"
        f"Награда: {oath.reward}\n"
        f"Штраф: {oath.penalty}"
    )
