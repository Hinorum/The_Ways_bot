"""Бестиарий: идемпотентные встречи за сезон, скрытие невстреченных."""

from datetime import datetime, timezone

from sqlalchemy import delete, select

from app.bestiary import BEASTIES, bestiary_text, note_round
from app.models import BestiarySighting, Round, RoundStatus, WinRule


def _open_round(day_index: int, rule=WinRule.MAJORITY, sealed=False) -> Round:
    return Round(
        day_index=day_index,
        status=RoundStatus.OPEN,
        win_rule=rule,
        rule_commitment="c",
        sealed=sealed,
        chapter_title="t",
        chapter_text="x",
        lore_summary="l",
        season="2026-08",
        opens_at=datetime(2026, 8, 1, 11, tzinfo=timezone.utc),
        voting_ends_at=datetime(2026, 8, 2, 10, tzinfo=timezone.utc),
        tally_ends_at=datetime(2026, 8, 2, 11, tzinfo=timezone.utc),
        vote_counts_json="{}",
    )


async def test_note_round_records_masks_once_per_season(session) -> None:
    first = _open_round(1000, rule=WinRule.MINORITY)
    session.add(first)
    await session.flush()
    created = await note_round(session, first, season_key_value="2026-08")
    await session.commit()
    # День 1 сезона: маска закона + Крыса (ранний круг сансары) + Дневник (закон объявлен).
    assert created == 3
    keys = {
        row[0]
        for row in (await session.execute(select(BestiarySighting.beast_key))).all()
    }
    assert {"wolf", "rat", "journal"} <= keys

    # Второй день с той же маской — дубля нет.
    second = _open_round(1001, rule=WinRule.MINORITY)
    second.season = "2026-08"
    session.add(second)
    await session.flush()
    created = await note_round(session, second, season_key_value="2026-08")
    await session.commit()
    assert created == 0

    # Новый сезон — запись появляется заново.
    created = await note_round(session, second, season_key_value="2026-09")
    await session.commit()
    assert created == 3


async def test_sealed_day_records_pit(session) -> None:
    blind = _open_round(1010, rule=WinRule.MAJORITY, sealed=True)
    session.add(blind)
    await session.flush()
    created = await note_round(session, blind, season_key_value="2026-08")
    await session.commit()
    # Маска большинства + Слепая Яма.
    keys = {
        row[0]
        for row in (await session.execute(select(BestiarySighting.beast_key))).all()
    }
    assert {"choir", "pit"} <= keys
    assert created >= 2


async def test_bestiary_text_hides_undiscovered(session) -> None:
    await session.execute(delete(BestiarySighting))
    session.add(
        BestiarySighting(
            season="2026-08", beast_key="wolf", day_index=5,
            title=BEASTIES["wolf"][0], description=BEASTIES["wolf"][1],
        )
    )
    await session.commit()
    text = await bestiary_text(session)
    assert "Одинокий Волк" in text
    assert "???" in text
    assert "Встречено 1 из" in text
    # Описание встреченного на месте; у невстреченного — только заглушка.
    lines = [line for line in text.splitlines() if line.startswith("•")]
    assert len(lines) == len(BEASTIES)
