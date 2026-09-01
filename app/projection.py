"""DayProjection — единый объект-факт, потребляемый всеми системами после талли.

Архитектурный инсайт: сегодня каждый модуль (echoes, relations, trail,
callings, broadcast) независимо читает БД и вычисляет своё состояние.
DayProjection собирает все факты одного дня в один immutable dataclass,
который передаётся по цепочке plugin → story → broadcast.

Аналог Covel WorldIR, адаптированный под ежедневный цикл голосования.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.models import Card, Round, Stake, Vote

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoteDistribution:
    """Агрегированное распределение голосов по путям."""

    counts: dict[int, int]  # card_position -> vote_count
    total: int
    winner_card: int | None
    win_rule: str | None  # WinRule.value
    tie_note: str | None

    @property
    def winner_count(self) -> int:
        if self.winner_card is None:
            return 0
        return self.counts.get(self.winner_card, 0)

    @property
    def runner_up_count(self) -> int:
        """Голосы «второго места» — для flip margin."""
        if self.winner_card is None:
            return 0
        others = [c for pos, c in self.counts.items() if pos != self.winner_card]
        return max(others) if others else 0

    @property
    def is_unanimous(self) -> bool:
        """Один путь забрал все голоса."""
        return self.total > 0 and self.winner_count == self.total


@dataclass(frozen=True)
class StakeDistribution:
    """Финансовое распределение ставок по путям."""

    path_stakes: dict[int, int]  # card_position -> nanotons
    total: int
    winner_stake: int
    winner_share_pct: float | None
    multiplier: float | None

    @property
    def has_action(self) -> bool:
        return self.total > 0


@dataclass(frozen=True)
class NPCDelta:
    """Один шаг отношения NPC к стае."""

    name: str  # liner / archivist / master / heretic
    old_sentiment: int
    new_sentiment: int
    shift: int
    tone_before: str
    tone_after: str

    @property
    def changed(self) -> bool:
        return self.shift != 0


@dataclass(frozen=True)
class AlignmentDrift:
    """Дрейф нрава стаи после дня."""

    order_before: int
    order_after: int
    moral_before: int
    moral_after: int
    tag: str  # winning card tag

    @property
    def order_changed(self) -> bool:
        return self.order_before != self.order_after

    @property
    def moral_changed(self) -> bool:
        return self.moral_before != self.moral_after


@dataclass(frozen=True)
class FlipMarginResult:
    """«Канон на волоске»: результат анализа переворота."""

    distance: int | None  # минимальное число переключений
    alternative_winner: int | None

    @property
    def is_close(self) -> bool:
        return self.distance is not None and self.distance <= 3


@dataclass(frozen=True)
class CardInfo:
    """Информация о карте дня (без SQL-модели)."""

    position: int
    title: str
    description: str
    consequence: str
    tag: str  # risk / care / cunning


@dataclass
class DayProjection:
    """Единый объект-факт завершённого дня.

    Immutable после создания. Все downstream-системы (echoes, broadcast,
    trail, prompt assembly) читают из этого объекта вместо повторных запросов
    к БД.

    Пример использования::

        projection = await build_projection(session, round_row)
        # Все системы читают projection:
        trigger_echoes(projection)
        update_npc_relations(projection)
        broadcast_results(projection)
        inject_into_prompt(projection)
    """

    # ── Идентификация ──
    round_id: int
    day_index: int
    season: str | None
    opened_at: datetime
    closed_at: datetime

    # ── Голосование ──
    votes: VoteDistribution

    # ── Финансы ──
    stakes: StakeDistribution
    pot_nanotons: int

    # ── Карты ──
    cards: list[CardInfo] = field(default_factory=list)
    winning_card: CardInfo | None = None

    # ── NPC ──
    npc_deltas: list[NPCDelta] = field(default_factory=list)

    # ── Нрав стаи ──
    alignment: AlignmentDrift | None = None

    # ── Эхо ──
    flip_margin: FlipMarginResult | None = None
    echoed_echoes: list[int] = field(default_factory=list)  # id всплывших эхо

    # ── Контекст ──
    is_sealed: bool = False
    money_mode: bool = True
    run_day: int = 0
    total_days: int = 0
    act_stage: int = 0  # 1-4

    # ── Производные ──
    _winner_index: int | None = field(default=None, repr=False)

    @property
    def winner_title(self) -> str | None:
        return self.winning_card.title if self.winning_card else None

    @property
    def winner_consequence(self) -> str | None:
        return self.winning_card.consequence if self.winning_card else None

    @property
    def winner_tag(self) -> str | None:
        return self.winning_card.tag if self.winning_card else None

    @property
    def voter_count(self) -> int:
        return self.votes.total

    @property
    def has_stakes(self) -> bool:
        return self.stakes.has_action

    def card_by_position(self, position: int) -> CardInfo | None:
        for c in self.cards:
            if c.position == position:
                return c
        return None

    def to_dict(self) -> dict:
        """Сериализация для логирования и передачи между модулями."""
        return {
            "round_id": self.round_id,
            "day_index": self.day_index,
            "season": self.season,
            "winner_card": self.winning_card.position if self.winning_card else None,
            "winner_title": self.winner_title,
            "winner_tag": self.winner_tag,
            "votes": self.votes.counts,
            "total_votes": self.votes.total,
            "pot": self.pot_nanotons,
            "stakes_total": self.stakes.total,
            "flip_distance": self.flip_margin.distance if self.flip_margin else None,
            "npc_shifts": [
                {"name": d.name, "shift": d.shift} for d in self.npc_deltas if d.changed
            ],
            "sealed": self.is_sealed,
        }


# ────────────────────────────────────────────────────────────
# Factory: сборка проекции из БД
# ────────────────────────────────────────────────────────────


async def build_projection(session: AsyncSession, round_row: Round) -> DayProjection:
    """Собирает DayProjection из завершённого раунда.

    Вызывается ОДИН раз в finish_tally() после атомарного закрытия.
    Все данные читаются за один проход (или минимальное число запросов).
    """
    from app.models import Card, Stake, Vote, WinRule
    from app.rounds import pick_winner, tied_positions
    from app.stakes import current_network
    from app.tally import flip_margin
    from app.ton_utils import from_nano

    # ── Карты ──
    cards_result = await session.execute(
        select(Card).where(Card.round_id == round_row.id).order_by(Card.position)
    )
    card_rows = cards_result.scalars().all()
    cards = [
        CardInfo(
            position=c.position,
            title=c.title,
            description=c.description,
            consequence=c.consequence,
            tag=c.tag or "care",
        )
        for c in card_rows
    ]
    winning = None
    if round_row.winner_card is not None:
        for c in cards:
            if c.position == round_row.winner_card:
                winning = c
                break

    # ── Голоса ──
    raw_counts = json.loads(round_row.vote_counts_json or "{}")
    counts = {int(k): int(v) for k, v in raw_counts.items()}
    total_votes = sum(counts.values())
    votes = VoteDistribution(
        counts=counts,
        total=total_votes,
        winner_card=round_row.winner_card,
        win_rule=round_row.win_rule.value if round_row.win_rule else None,
        tie_note=round_row.tie_note,
    )

    # ── Ставки ──
    network = current_network()
    path_stakes_result = await session.execute(
        select(Vote.card_position, func.coalesce(func.sum(Stake.amount_nanotons), 0))
        .join(Stake, Stake.player_id == Vote.player_id)
        .where(
            Vote.round_id == round_row.id,
            Stake.round_id == round_row.id,
            Stake.status == "confirmed",
            Stake.network == network,
        )
        .group_by(Vote.card_position)
    )
    path_stakes = {int(p): int(v) for p, v in path_stakes_result.all()}
    winner_stake = path_stakes.get(round_row.winner_card, 0) if round_row.winner_card is not None else 0
    pot = round_row.pot_nanotons or 0
    winner_share_pct = round(winner_stake * 100 / pot) if pot > 0 else None

    # Множитель
    multiplier = None
    if winner_stake > 0:
        from sqlalchemy import select as _sel
        from app.models import Payout

        prize_row = await session.execute(
            select(func.coalesce(func.sum(Payout.amount_nanotons), 0)).where(
                Payout.round_id == round_row.id, Payout.kind == "prize"
            )
        )
        prize_sum = int(prize_row.scalar_one())
        if prize_sum > 0:
            multiplier = prize_sum / winner_stake

    stakes = StakeDistribution(
        path_stakes=path_stakes,
        total=sum(path_stakes.values()),
        winner_stake=winner_stake,
        winner_share_pct=winner_share_pct,
        multiplier=multiplier,
    )

    # ── Flip margin ──
    fm = flip_margin(counts, round_row.win_rule, round_row.winner_card)
    flip = FlipMarginResult(
        distance=fm[0] if fm else None,
        alternative_winner=fm[1] if fm else None,
    )

    # ── NPC deltas ──
    from app.relations import load_relations, _SHIFTS, _TONES

    npc_deltas = []
    try:
        current_rels = await load_relations(session)
        tag = winning.tag if winning else "care"
        shift_map = _SHIFTS.get(tag, {})
        for npc_name in ("liner", "archivist", "master", "heretic"):
            new_val = current_rels.get(npc_name, 0)
            shift = shift_map.get(npc_name, 0)
            old_val = max(-3, min(3, new_val - shift))
            npc_deltas.append(
                NPCDelta(
                    name=npc_name,
                    old_sentiment=old_val,
                    new_sentiment=new_val,
                    shift=shift,
                    tone_before=_TONES.get(old_val, ("neutral",))[0],
                    tone_after=_TONES.get(new_val, ("neutral",))[0],
                )
            )
    except Exception:
        logger.debug("NPC deltas не собраны для дня %s", round_row.day_index)

    # ── Alignment drift ──
    alignment = None
    try:
        from app.models import WatcherState as WS
        from app.season import RUN_START_KEY, get_run_anchor, anchor_axes, _ALIGNMENT_DRIFT, _clamp_axis, _rng

        anchor = await get_run_anchor(session)
        tag = winning.tag if winning else "care"
        # Вычисляем дельту (та же логика, что и в apply_alignment_drift),
        # но НЕ мутируем якорь — он уже сдвинут finish_tally.
        moved = _ALIGNMENT_DRIFT.get(tag)
        if moved:
            new_order, new_moral = anchor_axes(anchor)
            # Реверс: вычисляем pre-drift значения
            old_order, old_moral = new_order, new_moral
            rng = _rng(f"drift:{tag}:{round_row.day_index}")
            for key, delta in moved.items():
                if callable(delta):
                    delta = delta(rng)
                current = _clamp_axis(anchor.get(key, 0))
                old_val = _clamp_axis(current - delta)
                if key == "order":
                    old_order = old_val
                elif key == "moral":
                    old_moral = old_val
            if old_order != new_order or old_moral != new_moral:
                alignment = AlignmentDrift(
                    order_before=old_order,
                    order_after=new_order,
                    moral_before=old_moral,
                    moral_after=new_moral,
                    tag=tag,
                )
    except Exception:
        logger.debug("Alignment drift не собран для дня %s", round_row.day_index)

    # ── Временны́е рамки ──
    from app.season import run_position, get_run_anchor

    try:
        anchor = await get_run_anchor(session)
        run_day, total_days = run_position(anchor, round_row.opens_at or datetime.now(timezone.utc))
    except Exception:
        run_day, total_days = 0, 0

    from app.season import act_index as _act_index

    try:
        act_stage = _act_index(run_day, total_days)
    except Exception:
        act_stage = 1

    return DayProjection(
        round_id=round_row.id,
        day_index=round_row.day_index,
        season=round_row.season,
        opened_at=round_row.opens_at or datetime.now(timezone.utc),
        closed_at=datetime.now(timezone.utc),
        votes=votes,
        stakes=stakes,
        pot_nanotons=pot,
        cards=cards,
        winning_card=winning,
        npc_deltas=npc_deltas,
        alignment=alignment,
        flip_margin=flip,
        is_sealed=getattr(round_row, "sealed", False),
        money_mode=getattr(round_row, "money_mode", True),
        run_day=run_day,
        total_days=total_days,
        act_stage=act_stage,
    )
