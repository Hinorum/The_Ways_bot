"""След игрока — эмерджентный «alignment» Правил Стаи.

Никаких анкет: две оси вычисляются из истории голосов по закрытым дням.

- Ось Хора (законопослушность): доля голосов, совпавших с итоговым
  победителем дня. Хоровая верность тянёт к «уставу», систематический бунт —
  в одиночки.
- Ось Сердца (мораль): баланс заботливых и хитрых троп среди выбранных
  путей; риск нейтрален и весит только объёмом.

Сетка 3×3 показывает титул клетки. Данные — только на показ: След не влияет
ни на деньги, ни на вес голоса. При малой выборке (< MIN_VOTES) Следа нет —
мир ещё не успел узнать собаку.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Card, Round, RoundStatus, Vote

MIN_VOTES = 10

# Клетки сетки: индекс [y][x], где x — ось Хора (-1 бунт, +1 хор),
# y — ось Сердца (+1 сердце, -1 клык).
TRAIL_CELLS: dict[tuple[int, int], str] = {
    (1, 1): "Пастух",
    (0, 1): "Компас",
    (-1, 1): "Дворняга-сердце",
    (1, 0): "Овчарка устава",
    (0, 0): "Стая сама по себе",
    (-1, 0): "Одиночка",
    (1, -1): "Цепной",
    (0, -1): "Тень двора",
    (-1, -1): "Грызло",
}


def trail_cell(order: float, moral: float) -> tuple[int, int]:
    """Точка сетки из значений осей -1..1: крайние трети дают ±1."""

    def axis(value: float) -> int:
        if value > 1 / 3:
            return 1
        if value < -1 / 3:
            return -1
        return 0

    return axis(order), axis(moral)


def trail_name(order: float, moral: float) -> str | None:
    if not TRAIL_CELLS:
        return None
    return TRAIL_CELLS[trail_cell(order, moral)]


async def trail_stats(session: AsyncSession, player_id: int) -> dict | None:
    """Оси Следа по закрытым дням либо None, если голосов ещё мало."""
    rows = (
        await session.execute(
            select(Vote.card_position, Card.tag, Round.winner_card)
            .join(Round, Round.id == Vote.round_id)
            .join(Card, (Card.round_id == Vote.round_id) & (Card.position == Vote.card_position))
            .where(
                Vote.player_id == player_id,
                Round.status == RoundStatus.CLOSED,
                Round.winner_card.is_not(None),
            )
        )
    ).all()
    total = len(rows)
    if total < MIN_VOTES:
        return None
    with_winner = sum(1 for position, _tag, winner in rows if position == winner)
    care = sum(1 for _position, tag, _winner in rows if tag == "care")
    cunning = sum(1 for _position, tag, _winner in rows if tag == "cunning")
    order = (with_winner / total) * 2 - 1
    denom = max(1, care + cunning)
    moral = (care - cunning) / denom
    return {
        "order": order,
        "moral": moral,
        "total": total,
        "conformity": with_winner / total,
        "heart_share": care / total,
        "fang_share": cunning / total,
    }


def trail_line(stats: dict) -> str:
    """Строка для /score: клетка, проценты, объём выборки."""
    name = trail_name(stats["order"], stats["moral"]) or "Стая сама по себе"
    hor = round(stats["conformity"] * 100)
    heart = round((stats["heart_share"] + 1 - stats["fang_share"]) * 50)
    return (
        f"🐾 Твой След: «{name}» — хор {hor}%, сердце {heart}% "
        f"(по {stats['total']} голосам)."
    )
