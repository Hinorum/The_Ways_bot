"""Сюжет-машина сезона, глухие дни, порядок карт и личная хроника."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Card, Player, Round, RoundStatus, Vote, WatcherState, WinRule
from app.rounds import _materialize_round, _villain_block, public_round_view, sealed_day
from app.season import (
    VILLAIN_KEY,
    villain_event,
    villain_stage,
)
from app.story import _build_story_prompt, _parse_chapter


# ---------- План Хозяина Ошибки ----------


def test_villain_stage_boundaries() -> None:
    assert villain_stage(1, 30) == 0
    assert villain_stage(6, 30) == 0
    assert villain_stage(7, 30) == 1
    assert villain_stage(15, 30) == 2
    assert villain_stage(24, 30) == 3
    assert villain_stage(29, 28) == 3  # финальный день — всегда кризис


def test_villain_event_deterministic_per_season() -> None:
    a = villain_event("2026-08", 1)
    b = villain_event("2026-08", 1)
    assert a == b and len(a) > 20
    # Другой сезон — другая версия события (пулы >1 фразы на ступень).
    events = {villain_event(f"2025-{month:02d}", 2) for month in range(1, 13)}
    assert len(events) >= 2


async def test_villain_block_progresses_and_persists(session: AsyncSession) -> None:
    """Ступени открываются по одной; события копятся; новый сезон стартует заново."""
    moment = datetime(2026, 8, 2, tzinfo=timezone.utc)
    block = await _villain_block(session, moment)
    assert block is not None and "план Хозяина Ошибки" in block

    mid_month = moment.replace(day=15)
    await _villain_block(session, mid_month)
    row = await session.get(WatcherState, VILLAIN_KEY)
    data = json.loads(row.value)
    assert data["season"] == "2026-08"
    assert len(data["events"]) == data["stage"] + 1 >= 2

    next_season = datetime(2026, 9, 3, tzinfo=timezone.utc)
    await _villain_block(session, next_season)
    row = await session.get(WatcherState, VILLAIN_KEY)
    data = json.loads(row.value)
    assert data["season"] == "2026-09" and data["stage"] == 0


# ---------- Глухой день ----------


def test_sealed_day_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sealed_day_every", 10)
    assert sealed_day(7) is True
    assert sealed_day(17) is True
    assert sealed_day(8) is False
    assert sealed_day(1) is False
    monkeypatch.setattr(settings, "sealed_day_every", 0)
    assert sealed_day(7) is False


def test_sealed_prompt_hides_the_law() -> None:
    prompt_open = _build_story_prompt(7, [], WinRule.MINORITY)
    prompt_sealed = _build_story_prompt(
        7, [], WinRule.MINORITY, sealed=True
    )
    assert "побеждает карта, собравшая меньше всех голосов" in prompt_open
    assert "побеждает карту" not in prompt_sealed
    assert "ЗАПЕЧАТАН" in prompt_sealed


def test_parse_chapter_shuffles_card_order_deterministically() -> None:
    """Позиции карт не обязаны совпадать с тегами: риск не всегда Путь I."""
    cards = [
        {"title": f"T{i}", "description": "d", "consequence": "c", "tag": tag}
        for i, tag in enumerate(("risk", "care", "cunning"))
    ]
    payload_text = json.dumps(
        {"title": "День N. Тест", "text": "текст главы", "lore_summary": "l",
         "cards": cards}
    )
    payload = {"choices": [{"message": {"content": payload_text}}]}

    seen_orders: set[tuple[str, ...]] = set()
    for day_index in range(1, 13):
        parsed = _parse_chapter(json.loads(json.dumps(payload)), day_index)
        order = tuple(card["tag"] for card in parsed["cards"])
        seen_orders.add(order)
        # Повтор того же дня детерминирован.
        again = _parse_chapter(json.loads(json.dumps(payload)), day_index)
        assert tuple(c["tag"] for c in again["cards"]) == order
    assert len(seen_orders) > 1  # порядок реально варьируется ото дня ко дню


async def test_materialize_round_reads_sealed_flag(session: AsyncSession) -> None:
    base = {
        "day_index": 501,
        "rule": "minority",
        "commitment": "abc:def",
        "chapter_title": "t",
        "chapter_text": "text",
        "lore_summary": "lore",
        "cover_path": "",
        "cards": [{"position": 0, "title": "a", "description": "b", "consequence": "c",
                   "tag": "risk", "image_path": ""}],
    }
    sealed_payload = dict(base, sealed=True)
    round_row = await _materialize_round(session, sealed_payload, latest=None)
    assert round_row.sealed is True
    plain = dict(base)
    plain["day_index"] = 502
    round_plain = await _materialize_round(session, plain, latest=None)
    assert round_plain.sealed is False


def test_status_text_hides_law_on_sealed_day() -> None:
    from app.broadcast import status_text

    now = datetime.now(timezone.utc)
    round_row = Round(
        day_index=601,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MINORITY,
        rule_commitment="deadbeef00:salt",
        chapter_title="Заголовок",
        chapter_text="Текст главы.",
        lore_summary="lore",
        opens_at=now - timedelta(hours=1),
        voting_ends_at=now + timedelta(hours=22),
        tally_ends_at=now + timedelta(hours=23),
        winner_card=None,
        vote_counts_json="{}",
        sealed=True,
    )
    round_row.cards = [
        Card(position=i, title=f"C{i}", description="описание пути",
             consequence="с", tag="care", image_path="")
        for i in range(3)
    ]
    text = status_text(round_row)
    assert "ЗАПЕЧАТАН" in text and "deadbeef00…" in text
    assert "собравшая меньше всех голосов" not in text


def test_results_reveal_sealed_law() -> None:
    from app.tally import format_results

    round_row = SimpleNamespace(
        day_index=9,
        id="90",
        win_rule=WinRule.MEDIAN,
        winner_card=1,
        vote_counts_json='{"0": 5, "1": 2, "2": 3}',
        tie_note=None,
        sealed=True,
        cards=[
            SimpleNamespace(position=i, title=f"P{i}", consequence=f"с{i}")
            for i in range(3)
        ],
    )
    text = format_results(round_row)
    assert "Запечатанный с утра закон оказался" in text
    assert "средним числом голосов" in text


def test_public_view_hides_win_rule_when_sealed() -> None:
    now = datetime.now(timezone.utc)
    round_row = Round(
        day_index=602,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MINORITY,
        rule_commitment="c",
        chapter_title="t",
        chapter_text="x",
        lore_summary="l",
        opens_at=now,
        voting_ends_at=now,
        tally_ends_at=now,
        vote_counts_json="{}",
        sealed=True,
    )
    view = public_round_view(round_row)
    assert view["sealed"] is True and view["win_rule"] is None


# ---------- Личная хроника /score ----------


async def test_chronicle_lists_recent_days_with_marks(session: AsyncSession) -> None:
    from app.handlers import _chronicle

    player = Player(id=77_001, username="chron")
    session.add(player)
    now = datetime.now(timezone.utc)
    for offset, (winner, choice) in enumerate([(0, 0), (1, 0), (2, 2)]):
        round_row = Round(
            day_index=70_100 + offset,
            status=RoundStatus.CLOSED,
            win_rule=WinRule.MAJORITY,
            rule_commitment="c",
            chapter_title="t",
            chapter_text="x",
            lore_summary="l",
            opens_at=now - timedelta(days=offset + 1),
            voting_ends_at=now - timedelta(days=offset),
            tally_ends_at=now - timedelta(days=offset),
            winner_card=winner,
            vote_counts_json="{}",
        )
        session.add(round_row)
        await session.flush()
        session.add(Card(round_id=round_row.id, position=choice,
                         title=f"Путь-{offset}", description="d", consequence="c",
                         image_path=""))
        for position in range(3):
            if position != choice:
                session.add(Card(round_id=round_row.id, position=position,
                                 title=f"Иная-{offset}-{position}", description="d",
                                 consequence="c", image_path=""))
        session.add(Vote(round_id=round_row.id, player_id=player.id, card_position=choice))
    await session.commit()

    lines = await _chronicle(session, player.id)
    assert lines[0] == "Д70102 · Путь-2 🏆"
    assert lines[1] == "Д70101 · Путь-1 ·"
    assert lines[2] == "Д70100 · Путь-0 🏆"
