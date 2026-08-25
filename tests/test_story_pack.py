"""Книга обещаний + фокус-день NPC + двухмесячная арка забега."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.promises import due_promises, promise_block, record_promise
from app.relations import npc_focus_line
from app.season import run_position


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# ---------- Книга обещаний ----------


async def test_promises_live_three_days_then_pruned(session: AsyncSession) -> None:
    await record_promise(session, 10, "Мир обещал тёплый свет в Нулевом Блоке.")
    await record_promise(session, 11, "Лайнер остался должен стае одну сделку.")

    live = await due_promises(session, 12)
    assert [i["text"] for i in live] == [
        "Мир обещал тёплый свет в Нулевом Блоке.",
        "Лайнер остался должен стае одну сделку.",
    ]
    assert all(not i["fulfilled_today"] for i in live)

    # День 13: запись дня 10 выходит из TTL, остаётся только вчерашняя.
    live = await due_promises(session, 13)
    assert [i["text"] for i in live] == ["Лайнер остался должен стае одну сделку."]
    # День 14 — протухло всё.
    assert await due_promises(session, 14) == []


async def test_promise_fulfilled_marks_today(session: AsyncSession) -> None:
    from app.promises import mark_fulfilled_for_sources

    await record_promise(session, 30, "Канон обещал лишнюю метку на ошейнике.")
    # Эхо родом из дня 30 всплыло на день 31 → обещание исполняется сегодня.
    changed = await mark_fulfilled_for_sources(session, {30}, today=31)
    assert changed == 1
    live = await due_promises(session, 31)
    assert len(live) == 1
    assert live[0]["fulfilled_today"] is True
    block = promise_block(live)
    assert "ИСПОЛНЕНО СЕГОДНЯ" in block


async def test_promise_block_format(session: AsyncSession) -> None:
    assert promise_block([]) is None
    await record_promise(session, 20, "Канон обещал лишнюю метку на ошейнике.")
    live = await due_promises(session, 20)
    block = promise_block(live)
    assert block is not None
    assert "ОБЕЩАНИЯ МИРА" in block and "лишнюю метку" in block
    assert "не пересказывай список" in block


# ---------- Фокус-день NPC ----------


def test_npc_focus_arcs_three_day_line() -> None:
    """Микро-линия: один NPC держит хотелку 3 дня с фазами, потом ротация."""
    l1, l2, l3 = (npc_focus_line(d) for d in (1, 2, 3))
    assert all("ФОКУС ДНЯ" in x for x in (l1, l2, l3))
    assert "завязка" in l1 and "развитие" in l2 and "ход" in l3
    # Один и тот же персонаж внутри арки:
    npc1 = [x.split("—")[1].strip().split(":")[0] for x in (l1, l2, l3)]
    assert len(set(npc1)) == 1
    # День 4 — следующий персонаж, снова завязка.
    l4 = npc_focus_line(4)
    assert "завязка" in l4 and l4.split("—")[1] != l1.split("—")[1]
    assert npc_focus_line(0) is None


# ---------- Двухмесячная арка ----------


def test_run_arc_spans_two_months(monkeypatch) -> None:
    from app.config import settings
    from app.season import is_run_finale

    monkeypatch.setattr(settings, "run_length_months", 2)
    anchor = {"dom": 24, "key": "2026-08"}
    # Август (7 дней от 24-го) + сентябрь (30) + октябрь (24 дня до 24.10):
    # длина арки = 61.
    day1, total = run_position(anchor, _utc(2026, 8, 24))
    assert (day1, total) == (1, 61)
    mid, _ = run_position(anchor, _utc(2026, 9, 23))
    assert mid == 31
    finale_day, _ = run_position(anchor, _utc(2026, 10, 23))
    assert is_run_finale(finale_day, total)
    # Цикл после финала: 24 октября — день 1 новой арки.
    wrapped, _ = run_position(anchor, _utc(2026, 10, 24))
    assert wrapped == 1


def test_one_month_mode_still_available(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "run_length_months", 1)
    anchor = {"dom": 1, "key": "2026-08"}
    _, total = run_position(anchor, _utc(2026, 8, 31))
    assert total == 31
