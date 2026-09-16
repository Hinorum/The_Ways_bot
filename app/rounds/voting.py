from __future__ import annotations

import logging
import random

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Round, Stake, Vote, WinRule

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
    "Кость архива стукнула о дно урны: путь {chosen}.",
    "Жребий дня лёг на {paths} — и указал {chosen}.",
    "Дневник перевернул страницу дважды; выпало {chosen}.",
)


async def count_votes_for_tally(session: AsyncSession, round_id: int) -> dict[int, int]:
    """Allowed only from the tally job. One GROUP BY, O(n) scan of the day partition."""
    result = await session.execute(
        select(Vote.card_position, func.count())
        .where(Vote.round_id == round_id)
        .group_by(Vote.card_position)
    )
    counts = {0: 0, 1: 0, 2: 0}
    for position, total in result.all():
        counts[int(position)] = int(total)
    bonus = getattr(settings, "stake_vote_bonus_weight", 0) or 0
    if bonus > 0:
        # «Кожа в игре»: путь, за который хотя бы один игрок держит
        # подтверждённую ставку TON, получает плоский перевес. Ставка
        # привязывается к пути через голос самого игрока (Stake не хранит
        # позицию отдельно). Только confirmed — деньги реально заблокированы.
        stake_rows = await session.execute(
            select(Vote.card_position, func.count(distinct(Vote.player_id)))
            .join(
                Stake,
                (Stake.round_id == Vote.round_id)
                & (Stake.player_id == Vote.player_id),
            )
            .where(Vote.round_id == round_id, Stake.status == "confirmed")
            .group_by(Vote.card_position)
        )
        for position, holders in stake_rows.all():
            if holders > 0:
                counts[int(position)] += bonus
    return counts


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


async def _staked_paths(session: AsyncSession, round_id: int) -> set[int]:
    """Пути дня, за которые есть хотя бы один подтверждённый ставщик."""
    rows = await session.execute(
        select(Vote.card_position, func.count(distinct(Vote.player_id)))
        .join(
            Stake,
            (Stake.round_id == Vote.round_id) & (Stake.player_id == Vote.player_id),
        )
        .where(Vote.round_id == round_id, Stake.status == "confirmed")
        .group_by(Vote.card_position)
    )
    return {int(p) for p, holders in rows.all() if holders > 0}


def _pick_among(
    counts: dict[int, int], rule: WinRule, seed: str | None, paths: list[int]
) -> tuple[int, list[int]]:
    """(победитель, претенденты) по закону ДНЯ только внутри заданного набора
    путей. В отличие от pick_winner, «не заявленные» пути не существуют —
    отсутствующий путь не трактуется как 0 голосов и не лезет в MINORITY-минимум."""
    items = [(counts.get(p, 0), p) for p in paths]
    if not items:
        return 0, []
    if rule is WinRule.MAJORITY:
        ref = max(c for c, _ in items)
    elif rule is WinRule.MINORITY:
        ref = min(c for c, _ in items)
    else:  # MEDIAN
        ordered = sorted(items, key=lambda t: (t[0], t[1]))
        ref = ordered[len(ordered) // 2][0]
    cands = sorted(p for c, p in items if c == ref)
    if len(cands) > 1 and seed:
        winner = random.Random(f"law:{seed}").choice(cands)
    else:
        winner = cands[0]
    return winner, cands


def _prefer_staked(
    counts: dict[int, int], rule: WinRule, seed: str | None, staked: set[int]
) -> tuple[int, list[int]]:
    """(победитель, претенденты) с приоритетом ставящих.

    Если хотя бы один путь реально заблокирован ставкой TON, путь, за который
    НИКТО не держит деньги, не может победить: закон пересчитывается строго
    по ставящим путям. Это лечит MINORITY-патологию — там побеждает наименьший
    счёт, и голос против «пустого» пути мог бы случайно выиграть; отсекаем
    безденежные кандидатов до выбора. При отсутствии ставящих — исход по
    прежнему чисто-подсчётному закону целиком.
    """
    if not staked:
        return pick_winner(counts, rule, seed=seed), tied_positions(counts, rule)
    return _pick_among(counts, rule, seed, sorted(staked))


async def _winner_and_tied(
    session: AsyncSession,
    round_row: Round,
    counts: dict[int, int],
    seed: str,
) -> tuple[int, list[int]]:
    """Выбор победителя с необязательным приоритетом ставящих (win_rule_prefers_staked)."""
    if getattr(settings, "win_rule_prefers_staked", False):
        staked = await _staked_paths(session, round_row.id)
        return _prefer_staked(counts, round_row.win_rule, seed=seed, staked=staked)
    return pick_winner(counts, round_row.win_rule, seed=seed), tied_positions(
        counts, round_row.win_rule
    )
