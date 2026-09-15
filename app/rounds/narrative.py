from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Round, StoryBeat

logger = logging.getLogger(__name__)


async def previous_beats(session: AsyncSession, limit: int = 12) -> list[str]:
    """Краткая сводка последних дней для анонсов и статистики.

    Формат: «День N — титул / краткий итог». Шаблонный режим не генерирует
    историю, поэтому итоги берутся из story_beats (ликвидная краткая запись).
    """
    result = await session.execute(
        select(StoryBeat).order_by(StoryBeat.day_index.desc()).limit(limit)
    )
    beats = list(result.scalars())
    beats.reverse()
    return [
        f"День {beat.day_index} — {beat.winning_title}: {beat.winning_text[:120]}"
        for beat in beats
    ]


async def write_epilogue(session: AsyncSession, round_row: Round) -> str:
    """Эпилог дня: в шаблонном режиме нейросеть не задействуется.

    Поле Round.epilogue_text остаётся пустым — сухие итоги в broadcast.
    Функция возвращает "" (идемпотентна: вызывается из heal / finalize).
    """
    return round_row.epilogue_text or ""