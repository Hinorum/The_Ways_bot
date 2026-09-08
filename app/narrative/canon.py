"""Единый повествовательный канон (StoryCanon).

Один объект собирает всё, что промпты знают о прошлом, из одного места:
- строки «титул: крючок · эпилог · итог» (прежний rounds.previous_beats),
- теги и последний титул (канон для понижения повторяемости карт),
- созревшие эха прошлых дней (прежний echoes.collect_due_echoes).

Канон — точка, где «история» читается целиком, а не пятью параллельными
чтениями. Поздние слои пересборки переведут на этот же объект остальные
системы памяти (WorldChoice/WorldSnapshot/ConsequenceTree/MemoryHit/
AIGeneratedPool) — правда станет единой.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.echoes import collect_due_echoes
from app.models import LoreEcho, Round, StoryBeat

logger = logging.getLogger(__name__)


def _closing_hook(text: str, limit: int = 220) -> str:
    """Последние 1-2 предложения текста — финальный крючок дня.

    Режет по границе предложения, чтобы ни одно слово обрывающихся глав
    не попало в канон «на полуслове». Если последнее предложение короткое,
    докидывает предпоследнее.
    """
    text = " ".join((text or "").split())
    if not text:
        return ""
    text = text[: limit * 3]
    seps = [text[:limit].rfind(s) for s in ("…", "?", "!", ".")]
    first = max(seps)
    if first <= 0:
        return text[:limit].rstrip(" ,.;:")
    if first >= limit - 40:
        return text[: first + 1]
    second = max(text[:first].rfind(s) for s in ("…", "?", "!", "."))
    return text[second + 1 : first + 1] if second >= 0 else text[: first + 1]


@dataclass
class StoryCanon:
    """Канон последних дней + созревшие эха — сырьё нарративных промптов."""

    lines: list[str] = field(default_factory=list)
    day_indexes: list[int] = field(default_factory=list)
    echoes: list[LoreEcho] = field(default_factory=list)

    @property
    def titles(self) -> list[str]:
        """Титулы дней по порядку канона (старшие раньше)."""
        return [line.split(":", 1)[0] for line in self.lines if line]

    @property
    def last_title(self) -> str | None:
        return self.titles[-1] if self.titles else None

    @property
    def has_history(self) -> bool:
        return bool(self.lines)


async def load_canon(
    session: AsyncSession,
    day_index: int | None = None,
    *,
    limit: int = 12,
    echo_limit: int = 2,
) -> StoryCanon:
    """Собирает канон в одном чтении: уходящие дни плюс созревшие эха.

    day_index — день, к которому «созрели» эха. Без него (например, для
    /lore или тестов) читаются только строки канона, без побочных эффектов
    всплытия следов.
    """
    result = await session.execute(
        select(StoryBeat, Round)
        .outerjoin(Round, Round.day_index == StoryBeat.day_index)
        .order_by(StoryBeat.day_index.desc())
        .limit(limit)
    )
    rows = list(result.all())
    rows.reverse()

    lines: list[str] = []
    day_indexes: list[int] = []
    for beat, round_row in rows:
        chapter_text = round_row.chapter_text if round_row is not None else ""
        epis_text = round_row.epilogue_text if round_row is not None else ""
        hook = (beat.hook_text or "").strip() or _closing_hook(chapter_text)
        hook = hook[:220]
        epis = _closing_hook(epis_text, limit=140)
        parts = []
        if hook:
            parts.append(f"крючок: {hook}")
        if epis:
            parts.append(f"эпилог: {epis}")
        parts.append(f"итог: {beat.winning_text[:220]}")
        lines.append(f"{beat.winning_title}: {' '.join(parts)}")
        day_indexes.append(beat.day_index)

    echoes: list[LoreEcho] = []
    if day_index is not None:
        echoes = await collect_due_echoes(session, day_index, limit=echo_limit)

    return StoryCanon(lines=lines, day_indexes=day_indexes, echoes=echoes)