from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Stake


async def round_pot(session: AsyncSession, round_id: int) -> tuple[int, int]:
    """Банк дня и число ставок прямо из БД: всегда актуальные данные.

    Раньше здесь жил in-memory кэш (обновлялся каждым тиком). Оказался
    избыточным: /panel и анонс дня и так имеют живую сессию, а на нескольких
    процессах/перезапусках кэш у каждого был свой и врал до минуты. Теперь
    синхронный статус дня читает БД — (сумма подтверждённых ставок, число
    всех ставок дня), как требует пакет дней.
    """
    rows = (
        await session.execute(
            select(Stake.amount_nanotons, Stake.status).where(Stake.round_id == round_id)
        )
    ).all()
    nano = sum(int(amount) for amount, status in rows if status == "confirmed")
    return nano, len(rows)