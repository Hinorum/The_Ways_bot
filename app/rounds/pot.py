from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Round, Stake

# round_id -> (нанотоны подтверждённых ставок, число ставок дня).
# Обновляется каждым тиком; синхронный статус дня читает без БД.
_POT_CACHE: dict[int, tuple[int, int]] = {}


def get_cached_pot(round_id: int) -> tuple[int, int]:
    return _POT_CACHE.get(round_id, (0, 0))


async def refresh_round_pot_cache(session: AsyncSession, round_row: Round) -> None:
    """Банк дня = сумма подтверждённых ставок; счётчик — все ставки дня."""
    rows = (
        await session.execute(
            select(Stake.amount_nanotons, Stake.status).where(Stake.round_id == round_row.id)
        )
    ).all()
    nano = sum(int(amount) for amount, status in rows if status == "confirmed")
    if len(_POT_CACHE) > 64:
        _POT_CACHE.clear()
    _POT_CACHE[round_row.id] = (nano, len(rows))