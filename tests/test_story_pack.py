"""Книга обещаний + фокус-день NPC + двухмесячная арка забега."""

from __future__ import annotations

from datetime import datetime, timezone


from app.relations import npc_focus_line
from app.season import run_position


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


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
    monkeypatch.setattr(settings, "first_season_months", 2)
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
