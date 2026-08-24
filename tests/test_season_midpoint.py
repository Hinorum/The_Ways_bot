"""Серединный поворот: ровно первый день ступени 2 плана злодея."""

from datetime import datetime, timezone

from app.season import midpoint_day, season_block, villain_stage

ANCHOR = {"dom": 1, "key": "2026-08"}  # август: total = 31, середина = 15


def _moment(day: int) -> datetime:
    return datetime(2026, 8, day, 11, 0, tzinfo=timezone.utc)


def test_midpoint_is_first_day_of_stage2() -> None:
    assert villain_stage(15, 31) == 2
    assert midpoint_day(15, 31) is True
    for day in (13, 14, 16, 17):
        assert midpoint_day(day, 31) is False
    # Короткий месяц (февраль-28): max(8, 14) = 14.
    assert midpoint_day(14, 28) is True
    assert midpoint_day(13, 28) is False and midpoint_day(15, 28) is False


def test_season_block_marks_midpoint_once() -> None:
    block = season_block(anchor=ANCHOR, moment=_moment(15))
    assert "ПОВОРОТ СЕРЕДИНЫ" in block
    # Соседние дни и финал — без поворота.
    for day in (14, 16, 24, 31):
        other = season_block(anchor=ANCHOR, moment=_moment(day))
        assert "ПОВОРОТ СЕРЕДИНЫ" not in other
