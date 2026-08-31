"""Призвания Стаи — «классы» Правил Стаи.

Призвание — косметика нарратива, не механика денег: титул в /score и
церемониях, окраска личного эха, касание в главах через промпт. Разблокировка
считается из уже накопленных данных (верные пути, память сети, глухие дни),
поэтому старые игроки получают варианты сразу.

Красная линия: призвание НЕ даёт ни веса голоса, ни информации о законе дня,
ни коэффициентов. Только лицо собаки.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Card, MemoryHit, Player, Round, RoundStatus, Vote

# Хвосты личного эха: как призвание окрашивает проигранную тропу.
ECHO_TAILS: dict[str, str] = {
    "ranger": "След не остывает — ты его уже читаешь.",
    "paladin": "Стая помнит тех, кто шёл с сердцем.",
    "rogue": "Тихие тропы тоже приводят домой.",
    "cleric": "Где-то в папке архива уже лежит и этот день.",
    "bard": "Об этом ещё споют у порталов.",
    "occultist": "Слепые дни иногда видят точнее всех.",
    "warlock": "Твой путь никто не отменял — его просто не поняли.",
}


@dataclass(frozen=True)
class Calling:
    key: str
    title: str
    emoji: str
    description: str
    # Условие разблокировки: ключ → минимальное значение из calling_progress.
    requirement: tuple[str, int]


CALLINGS: tuple[Calling, ...] = (
    Calling(
        "ranger",
        "Следопыт",
        "🏹",
        "Пять верных путей: тропа узнаёт своего.",
        ("correct_picks", 5),
    ),
    Calling(
        "paladin",
        "Палладин",
        "⚔️",
        "Сердце выбирается чаще клыка — и не один раз.",
        ("heart_lead", 5),
    ),
    Calling(
        "rogue",
        "Разбойник",
        "🗡️",
        "Дважды угадал в ночь Одинокого Волка: правота отставших.",
        ("minority_correct", 2),
    ),
    Calling(
        "cleric",
        "Жрец",
        "📖",
        "Три находки памяти: папки сами раскрываются навстречу.",
        ("memory_hits", 3),
    ),
    Calling(
        "bard",
        "Бард",
        "🎵",
        "Двадцать дней у карт: постоянство — тоже песня.",
        ("votes", 20),
    ),
    Calling(
        "occultist",
        "Оккультист",
        "👁️",
        "Верный путь в Слепой Яме: повезло так повезло.",
        ("sealed_correct", 1),
    ),
    Calling(
        "warlock",
        "Варлок",
        "🌑",
        "Дважды верный в Слепой Яме: закон для тебя не указ.",
        ("sealed_correct", 2),
    ),
)

_BY_KEY = {calling.key: calling for calling in CALLINGS}


def calling_by_key(key: str | None) -> Calling | None:
    if not key:
        return None
    return _BY_KEY.get(key)


async def calling_progress(session: AsyncSession, player_id: int) -> dict[str, int]:
    """Сырые счётчики для условий разблокировки. Немного запросов — данные малы."""
    player = await session.get(Player, player_id)
    correct_picks = player.correct_picks if player else 0

    votes_count = (
        await session.execute(select(func.count()).select_from(Vote).where(Vote.player_id == player_id))
    ).scalar_one()

    tag_rows = (
        await session.execute(
            select(Card.tag, func.count())
            .join(Vote, (Vote.round_id == Card.round_id) & (Vote.card_position == Card.position))
            .where(Vote.player_id == player_id)
            .group_by(Card.tag)
        )
    ).all()
    tags = {tag: count for tag, count in tag_rows}
    care = int(tags.get("care", 0))
    cunning = int(tags.get("cunning", 0))

    minority_correct = await _correct_in_kinds(session, player_id, kinds=("minority",))
    sealed_correct = await _correct_sealed(session, player_id)
    memory_hits = (
        await session.execute(
            select(func.count()).select_from(MemoryHit).where(MemoryHit.player_id == player_id)
        )
    ).scalar_one()
    return {
        "correct_picks": int(correct_picks),
        "votes": int(votes_count),
        "care_votes": care,
        "cunning_votes": cunning,
        "heart_lead": max(0, care - cunning),
        "minority_correct": minority_correct,
        "sealed_correct": sealed_correct,
        "memory_hits": int(memory_hits),
    }


async def _correct_in_kinds(session: AsyncSession, player_id: int, kinds: tuple[str, ...]) -> int:
    rows = await session.execute(
        select(func.count())
        .select_from(Vote)
        .join(Round, Round.id == Vote.round_id)
        .where(
            Vote.player_id == player_id,
            Round.status == RoundStatus.CLOSED,
            Round.winner_card.is_not(None),
            Round.win_rule.in_(kinds),
            Vote.card_position == Round.winner_card,
        )
    )
    return int(rows.scalar_one())


async def _correct_sealed(session: AsyncSession, player_id: int) -> int:
    rows = await session.execute(
        select(func.count())
        .select_from(Vote)
        .join(Round, Round.id == Vote.round_id)
        .where(
            Vote.player_id == player_id,
            Round.status == RoundStatus.CLOSED,
            Round.winner_card.is_not(None),
            Round.sealed.is_(True),
            Vote.card_position == Round.winner_card,
        )
    )
    return int(rows.scalar_one())


def unlocked_callings(progress: dict[str, int]) -> list[Calling]:
    available: list[Calling] = []
    for calling in CALLINGS:
        field, minimum = calling.requirement
        if progress.get(field, 0) >= minimum:
            available.append(calling)
    return available


async def available_callings(session: AsyncSession, player_id: int) -> list[Calling]:
    return unlocked_callings(await calling_progress(session, player_id))


async def callings_prompt_block(session: AsyncSession) -> str | None:
    """Блок для промпта главы: какие призвания живут в стае сейчас.

    Ведущему разрешено одно касание за главу; пустой стаи блок не строит.
    """
    rows = (
        await session.execute(
            select(Player.calling, func.count())
            .where(Player.calling.is_not(None))
            .group_by(Player.calling)
        )
    ).all()
    parts = []
    for key, count in rows:
        calling = calling_by_key(key)
        if calling is not None and count:
            parts.append(f"{calling.title} — {count}")
    if not parts:
        return None
    return (
        "ПРИЗВАНИЯ СТАИ (фон, одним касанием за главу, если уместно): "
        + ", ".join(parts)
        + "."
    )


def echo_tail(key: str | None) -> str | None:
    return ECHO_TAILS.get(key or "")
