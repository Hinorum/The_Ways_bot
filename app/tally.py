from __future__ import annotations

import json

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LeaderboardPot, Player, Payout, Round, Stake, Vote, RULE_PHRASES
from app.ton_utils import from_nano


async def award_points(session: AsyncSession, round_row: Round) -> int:
    if round_row.winner_card is None:
        return 0
    voters = await session.execute(select(Vote.player_id).where(Vote.round_id == round_row.id))
    voter_ids = [row[0] for row in voters.all()]
    if voter_ids:
        await session.execute(
            update(Player).where(Player.id.in_(voter_ids)).values(score=Player.score + 1)
        )
    winners = await session.execute(
        select(Vote.player_id).where(
            Vote.round_id == round_row.id,
            Vote.card_position == round_row.winner_card,
        )
    )
    winner_ids = [row[0] for row in winners.all()]
    if winner_ids:
        await session.execute(
            update(Player)
            .where(Player.id.in_(winner_ids))
            .values(score=Player.score + 10, correct_picks=Player.correct_picks + 1)
        )
    await session.commit()
    return len(winner_ids)


def format_results(round_row: Round) -> str:
    raw = json.loads(round_row.vote_counts_json or "{}")
    counts = {int(key): int(value) for key, value in raw.items()}
    names = {card.position: card.title for card in round_row.cards}
    lines = [
        f"Итог дня {round_row.day_index}",
        f"Закон дня: {RULE_PHRASES[round_row.win_rule]}",
        "",
    ]
    for position in range(3):
        mark = " ← След" if position == round_row.winner_card else ""
        lines.append(f"{names[position]}: {counts.get(position, 0)}{mark}")
    winner = names[round_row.winner_card or 0]
    consequence = next(
        card.consequence for card in round_row.cards if card.position == round_row.winner_card
    )
    lines += ["", f"Канон: {winner}", consequence]
    return "\n".join(lines)


async def day_economics(session: AsyncSession, round_row: Round) -> dict:
    """Цифры дня для поста итогов: банк, проценты, коэффициент, копилка.

    Читает уже созданные финализацией выплаты, поэтому вызывать стоит после
    finalize_day_payouts; при пустом банке деньги просто не попадут в пост.
    """
    raw = json.loads(round_row.vote_counts_json or "{}")
    counts = {int(key): int(value) for key, value in raw.items()}
    players = sum(counts.values())
    stats: dict = {
        "players": players,
        "counts": counts,
        "pot": round_row.pot_nanotons or 0,
        "multiplier": None,
        "bonus": 0,
        "board_today": 0,
        "bank_total": 0,
        "refunded": False,
    }
    if not players and not stats["pot"]:
        return stats

    async def kind_sum(kind: str) -> int:
        row = await session.execute(
            select(func.coalesce(func.sum(Payout.amount_nanotons), 0)).where(
                Payout.round_id == round_row.id,
                Payout.kind == kind,
            )
        )
        return int(row.scalar_one())

    stats["bonus"] = await kind_sum("bonus")
    board_today = await kind_sum("leaderboard")
    prize_sum = await kind_sum("prize")
    bank_row = await session.execute(select(func.coalesce(func.sum(LeaderboardPot.nanotons), 0)))
    stats["bank_total"] = int(bank_row.scalar_one())
    if board_today or stats["bank_total"]:
        stats["board_today"] = board_today

    if round_row.winner_card is not None:
        from app.stakes import current_network

        winners_subq = select(Vote.player_id).where(
            Vote.round_id == round_row.id,
            Vote.card_position == round_row.winner_card,
        )
        staked_winners = await session.execute(
            select(func.coalesce(func.sum(Stake.amount_nanotons), 0)).where(
                Stake.round_id == round_row.id,
                Stake.network == current_network(),
                Stake.status == "confirmed",
                Stake.player_id.in_(winners_subq),
            )
        )
        winning_total = int(staked_winners.scalar_one())
        if winning_total > 0 and prize_sum > 0:
            stats["multiplier"] = prize_sum / winning_total
        elif stats["pot"] > 0 and prize_sum == 0:
            stats["refunded"] = True
    return stats


def format_economics(stats: dict) -> str:
    """Текстовый блок экономики дня; без банка показывает только явку."""
    lines: list[str] = []
    if stats["players"]:
        percents = {
            pos: round(count * 100 / stats["players"])
            for pos, count in stats["counts"].items()
        }
        spread = " · ".join(
            f"{('I', 'II', 'III')[pos]} {percents.get(pos, 0)}%" for pos in range(3)
        )
        lines.append(f"📊 Пути: {spread}")
    if stats["pot"] <= 0:
        return "\n".join(lines)
    ton = from_nano
    lines.insert(0, f"💰 Банк дня: {ton(stats['pot']):.2f} TON · Играло: {stats['players']}")
    if stats["refunded"]:
        lines.append("🎯 Коэффициент недоступен: все ставки возвращены игрокам")
    elif stats["multiplier"] is not None:
        lines.append(f"🎯 Коэффициент верного пути: ×{stats['multiplier']:.2f}")
    if stats["bonus"] > 0:
        lines.append(f"🎁 Угадавшим без ставки роздано: {ton(stats['bonus']):.2f} TON")
    if stats["board_today"] > 0 or stats["bank_total"] > 0:
        lines.append(
            f"🏆 В копилку месяца ушло: {ton(stats['board_today']):.2f} TON"
            f" · всего в банке месяца: {ton(stats['bank_total']):.2f} TON"
        )
    return "\n".join(lines)
