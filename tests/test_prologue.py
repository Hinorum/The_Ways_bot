"""Пролог забега: дни 1-7 знакомят со стаей, финал и поздние дни — без него."""

from datetime import datetime, timezone

from app.prologue import PROLOGUE_BEATS, PROLOGUE_LAST_DAY, prologue_block, prologue_title
from app.season import season_block

ANCHOR = {"dom": 1, "key": "2026-08"}


def _moment(day: int) -> datetime:
    return datetime(2026, 8, day, 11, 0, tzinfo=timezone.utc)


def test_prologue_covers_first_seven_days() -> None:
    assert PROLOGUE_LAST_DAY == 7
    for day in range(1, 8):
        block = prologue_block(day)
        assert block is not None and "ПРОЛОГ" in block
        title = prologue_title(day)
        assert title is not None and title


def test_no_prologue_after_seven() -> None:
    for day in (8, 15, 27):
        assert prologue_block(day) is None
        assert prologue_title(day) is None


def test_season_block_injects_prologue_by_run_day() -> None:
    for day, fragment in (
        (1, "Приход"),
        (2, "Архивариус"),
        (3, "Лайнер"),
        (4, "Баркод"),
        (5, "Чужой счёт"),
        (6, "Первый Лай"),
        (7, "Зачем мы здесь"),
    ):
        block = season_block(anchor=ANCHOR, moment=_moment(day))
        assert fragment in block, f"день {day}: нет «{fragment}»"
        assert "акт" in block.lower()


def test_day8_free_of_prologue_and_finale_untouched(monkeypatch) -> None:
    block = season_block(anchor=ANCHOR, moment=_moment(8))
    assert "Пролог" not in block
    # Финал арки длиной в месяц: патчим длину, 31-е число — День Лая.
    from app.config import settings

    monkeypatch.setattr(settings, "run_length_months", 1)
    finale = season_block(anchor=ANCHOR, moment=_moment(31), balance={"care": 5})
    assert "ДЕНЬ ПЕРВОГО ЛАЯ" in finale
    assert "ПРОЛОГ" not in finale


def test_beats_unique_titles() -> None:
    titles = [beat["title"] for beat in PROLOGUE_BEATS.values()]
    assert len(titles) == len(set(titles))
