from __future__ import annotations

import logging
import random

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Round, Stake, Vote, WinRule
from app.stakes import current_network

logger = logging.getLogger(__name__)

# Визуальная фактура типов эхов: содержание скрыто, фактура повторяется —
# внимательный игрок учится узнавать класс следа по кадру дня.
_ECHO_ART_MOTIFS = {
    "угроза": "ominous burnt-wire glow in the fog",
    "память": "a warm amber keepsake bowl catching light",
    "обман": "a mirage-like silhouette of an unfamiliar dog",
}

# Театр жребия: реплики к честному броску при ничьей (детерминированы сидом).
_TIE_THEATER = (
    "Котёл булькнул дважды — жребий лёг на {chosen}.",
    "Жребий дня лёг на {paths} — и указал {chosen}.",
    "Часы Вокзала пробили полночь лишний раз: выпало {chosen}.",
)


async def count_votes_for_tally(session: AsyncSession, round_id: int) -> dict[int, int]:
    """Счёт голосов для подведения итога дня.

    Совместимая обёртка: единственная реализация GROUP BY по голосам —
    plain_vote_counts (хранимый счёт и fallback-исход при пустом фонде).
    Имя сохранено — по нему ходят tally-джоба и тесты
    (test_winner_by_stakes).
    """
    return await plain_vote_counts(session, round_id)


async def plain_vote_counts(session: AsyncSession, round_id: int) -> dict[int, int]:
    """Бесплатные голоса без легаси-бонуса ставок.

    Используется как хранимый счёт (vote_counts_json) и как fallback-исход,
    когда winner_by_stakes включён, но на день не поставлено ни одного грамма.
    """
    result = await session.execute(
        select(Vote.card_position, func.count())
        .where(Vote.round_id == round_id)
        .group_by(Vote.card_position)
    )
    counts = {0: 0, 1: 0, 2: 0}
    for position, total in result.all():
        counts[int(position)] = int(total)
    return counts


async def count_stakes_for_tally(session: AsyncSession, round_id: int) -> dict[int, int]:
    """Суммы подтверждённых ставок (нанотоны Gram) по путям дня.

    Путь привязан к ставке через голос игрока (Stake не хранит позицию):
    ставка игрока усиливает путь, за который он проголосовал. Только
    confirmed (деньги реально заблокированы) и активной сети (счёт должен
    совпадать с тем, по чему проходят выплаты). Пути без грамма = 0.
    """
    result = await session.execute(
        select(Vote.card_position, func.coalesce(func.sum(Stake.amount_nanotons), 0))
        .join(
            Stake,
            (Stake.round_id == Vote.round_id) & (Stake.player_id == Vote.player_id),
        )
        .where(
            Vote.round_id == round_id,
            Stake.status == "confirmed",
            Stake.network == current_network(),
        )
        .group_by(Vote.card_position)
    )
    sums = {0: 0, 1: 0, 2: 0}
    for position, total in result.all():
        sums[int(position)] = int(total)
    return sums


async def _decisive_counts(
    session: AsyncSession,
    round_row: Round,
    vote_counts: dict[int, int],
) -> tuple[dict[int, int], bool]:
    """(решающий счёт, были ли это суммы ставок) для winner_by_stakes.

    Исход определяют суммы подтверждённых ставок; день, где
    нет ни одного грамма, решается бесплатными голосами (fallback).
    """
    stakes = await count_stakes_for_tally(session, round_row.id)
    if any(value > 0 for value in stakes.values()):
        return stakes, True
    return vote_counts, False


def tied_positions(counts: dict[int, int], rule: WinRule) -> list[int]:
    """Все пути, претендующие на победу по закону дня (без учёта позиций)."""
    items = [(counts.get(i, 0), i) for i in range(3)]
    if rule is WinRule.MAJORITY:
        best = max(item[0] for item in items)
        return sorted(i for total, i in items if total == best)
    if rule is WinRule.MINORITY:
        worst = min(item[0] for item in items)
        return sorted(i for total, i in items if total == worst)
    ordered = sorted(items, key=lambda item: (item[0], item[1]))
    median = ordered[1][0]
    return sorted(i for total, i in items if total == median)


def pick_winner(counts: dict[int, int], rule: WinRule, seed: str | None = None) -> int:
    """Победитель по закону дня. Без seed — детерминированный fallback
    (меньший номер пути); с seed — честный жребий по закону дня, чтобы
    ничья не решалась «номером карты»."""
    candidates = tied_positions(counts, rule)
    if len(candidates) > 1 and seed:
        return random.Random(f"law:{seed}").choice(candidates)
    return candidates[0]


def tie_seed(round_row: Round) -> str:
    """Сидовое значение жеребьёвки дня — с честной энтропией мастерчейна.

    Формат «{day}:{law}[:{entropy}]»: при ничьей энтропия снимается ОДИН раз
    (close_voting) и сохраняется в день, поэтому пересчёт (heal, finish_tally)
    даёт тот же победитель. Без энтропии — прежний детерминированный жребий.
    """
    base = f"{round_row.day_index}:{round_row.win_rule.value}"
    entropy = getattr(round_row, "tie_entropy", None)
    if entropy:
        return f"{base}:{entropy}"
    return base


async def _winner_and_tied(
    session: AsyncSession,
    round_row: Round,
    counts: dict[int, int],
    seed: str,
) -> tuple[int, list[int]]:
    """Выбор победителя по закону дня.

    counts уже решающие (суммы ставок или голоса) — закон дня
    применяется к ним напрямую.
    """
    return pick_winner(counts, round_row.win_rule, seed=seed), tied_positions(
        counts, round_row.win_rule
    )
