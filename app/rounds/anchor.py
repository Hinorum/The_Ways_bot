from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.registry import RUN_START_KEY
from app.models import WatcherState

from .time import _now


def parse_anchor(value: str | None) -> dict | None:
    """Прочитать якорь из строки. Если формат некорректен — None."""
    if not value:
        return None
    try:
        data = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def default_anchor(moment) -> dict:
    """Свежий якорь забега на момент `moment`.

    Для leaderboard-совместимости включает «key» (YYYY-MM) и «dom»
    (день месяца): это всё, что читает parse_anchor для короткой
    стартовой недели.
    """
    return {
        "key": f"{moment.year:04d}-{moment.month:02d}",
        "dom": moment.day,
        "start_iso": (
            moment.isoformat()
            if hasattr(moment, "isoformat")
            else str(moment)
        ),
    }


async def get_run_anchor(session: AsyncSession) -> dict:
    """Якорь забега из watcher_state; для новых инстансов — «сейчас».

    Используется для одноразового определения периода (месяц запуска) —
    leaderboard проверяет короткую стартовую неделю забега по «key» и «dom».
    """
    row = await session.get(WatcherState, RUN_START_KEY)
    anchor = parse_anchor(row.value if row is not None else None)
    if anchor is None:
        anchor = default_anchor(_now())
        payload = json.dumps(anchor, ensure_ascii=False)
        if row is None:
            session.add(WatcherState(key=RUN_START_KEY, value=payload))
        else:
            row.value = payload
        await session.commit()
    return anchor