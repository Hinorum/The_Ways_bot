from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Round

logger = logging.getLogger(__name__)


async def write_epilogue(session: AsyncSession, round_row: Round) -> str:
    """Эпилог дня: в шаблонном режиме нейросеть не задействуется.

    Поле Round.epilogue_text остаётся пустым — сухие итоги в broadcast.
    Функция возвращает "" (идемпотентна: вызывается из heal / finalize).
    """
    return round_row.epilogue_text or ""