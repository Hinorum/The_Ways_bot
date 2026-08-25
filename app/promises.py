"""Книга обещаний: последствия победителей живут в мире несколько дней.

Промпт говорит «завтра придётся жить с этим», но раньше мир помнил только
эхо. Теперь каждое последствие победившего пути попадает в книгу обещаний
и три дня подаётся главам как невыплаченный долг: исполняется, ломается
или откладывается — на усмотрение Ведущего. Записи старше TTL растворяются.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WatcherState

PROMISE_KEY = "promise_ledger"
TTL_DAYS = 3

logger = logging.getLogger(__name__)


def _clamp_text(text: str, limit: int = 160) -> str:
    clean = " ".join((text or "").split())
    if len(clean) > limit:
        cut = clean[: limit - 1]
        stop = max(cut.rfind("."), cut.rfind("!"), cut.rfind("…"))
        clean = (cut[: stop + 1] if stop > 0 else cut.rstrip(" ,;:-")) + "…"
    return clean


async def record_promise(session: AsyncSession, day_index: int, text: str) -> None:
    """Добавляет обещание дня (после finish_tally), вычищая протухшие."""
    entry = {"day": int(day_index), "text": _clamp_text(text)}
    row = await session.get(WatcherState, PROMISE_KEY)
    ledger: list[dict] = []
    if row is not None and row.value:
        try:
            data = json.loads(row.value)
            if isinstance(data, list):
                ledger = [e for e in data if isinstance(e, dict) and "day" in e]
        except ValueError:
            ledger = []
    ledger.append(entry)
    ledger = [e for e in ledger if day_index - int(e["day"]) < TTL_DAYS]
    payload = json.dumps(ledger[-12:], ensure_ascii=False)
    if row is None:
        session.add(WatcherState(key=PROMISE_KEY, value=payload))
    else:
        row.value = payload
    await session.commit()


async def due_promises(session: AsyncSession, current_day_index: int) -> list[str]:
    """Тексты живых обещаний (не старше TTL); протухшие вычищаются на месте."""
    row = await session.get(WatcherState, PROMISE_KEY)
    if row is None or not row.value:
        return []
    try:
        ledger = json.loads(row.value)
        assert isinstance(ledger, list)
    except Exception:
        return []
    live: list[str] = []
    stale_found = False
    for entry in ledger:
        try:
            day = int(entry["day"])
            text = _clamp_text(str(entry.get("text", "")))
        except Exception:
            stale_found = True
            continue
        if current_day_index - day >= TTL_DAYS or current_day_index < day:
            stale_found = True
            continue
        if text:
            live.append(text)
    fresh = [
        {"day": int(e["day"]), "text": e["text"]}
        for e in ledger
        if isinstance(e, dict) and current_day_index - int(e.get("day", 10**9)) < TTL_DAYS
    ]
    if stale_found or len(fresh) != len(ledger):
        row.value = json.dumps(fresh, ensure_ascii=False)
        await session.commit()
    return live


def promise_block(promises: list[str]) -> str | None:
    """Блок для промпта главы. None — обещаний нет."""
    if not promises:
        return None
    lines = ["ОБЕЩАНИЯ МИРА (невыплаченный долг прошлых выборов):"]
    lines += [f"- {text}" for text in promises]
    lines.append(
        "Вплети ОДНО из них как живую деталь сцены или последствия выбора: "
        "обещание исполняется, ломается или откладывается — но не пересказывай список."
    )
    return "\n".join(lines)
