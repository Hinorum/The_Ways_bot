"""Режиссура дня: драматургия вскрытия урны и тизер в час подсчёта."""

from __future__ import annotations

import json

import pytest
from app.db import SessionLocal
from app.models import Round, RoundStatus, WatcherState, WinRule
from app.scheduler import _teaser_job
from app.tally import format_results


def _round(rule: WinRule, counts: dict[int, int], winner: int, day_index: int = 5):
    return SimpleRound(rule, counts, winner, day_index)


class SimpleRound:
    """Лёгкий двойник раунда для format_results без БД."""

    def __init__(self, win_rule, vote_counts_json_dict, winner_card, day_index):
        self.win_rule = win_rule
        self.vote_counts_json = json.dumps({str(k): v for k, v in vote_counts_json_dict.items()})
        self.winner_card = winner_card
        self.day_index = day_index
        self.id = day_index * 10
        self.tie_note = None
        self.cards = [
            type("C", (), {
                "position": i,
                "title": f"Путь {i}",
                "consequence": f"с{i}",
                "food_cost": 0,
                "water_cost": 0,
                "health_risk": 0,
                "trust_change": 0,
                "emotional_consequence": "",
                "npc_reactions_json": "[]",
            })()
            for i in range(3)
        ]


def test_results_majority_law_and_crowd_agree() -> None:
    text = format_results(_round(WinRule.MAJORITY, {0: 9, 1: 3, 2: 1}, winner=0))
    assert "Страница" in text
    assert "большинство" in text or "кричала" in text or "решили всё" in text
    assert "Путь 0: 9 ← 🏆 След" in text


def test_results_minority_twist_when_crowd_votes_against_law() -> None:
    """Меньшинство по закону, но стая громче всех лаяла за победителя — бунт."""
    text = format_results(_round(WinRule.MINORITY, {0: 8, 1: 2, 2: 3}, winner=0))
    reveal_line = next(line for line in text.splitlines() if "Страница" in line)
    assert any(
        word in reveal_line
        for word in ("парадокс", "против правила", "не заметила")
    )


def test_results_minority_quiet_win() -> None:
    text = format_results(_round(WinRule.MINORITY, {0: 7, 1: 1, 2: 4}, winner=1))
    assert (
        "тих" in text or "меньшинство" in text or "хвостов пошло" in text
    )
    assert "Путь 1: 1 ← 🏆 След" in text


def test_results_median_takes_middle() -> None:
    text = format_results(_round(WinRule.MEDIAN, {0: 6, 1: 2, 2: 4}, winner=2))
    assert "середина" in text or "меру" in text


def test_results_flip_margin_for_close_race() -> None:
    """«Канон на волоске»: плотная развилка показывает, чего не хватило."""
    text = format_results(_round(WinRule.MAJORITY, {0: 9, 1: 8, 2: 1}, winner=0))
    assert "на волоске" in text
    assert "ещё 1 голос за «Путь 1»" in text


def test_results_flip_margin_minority_twist() -> None:
    """Меньшинство победило с минимальным перевесом — волосок виден."""
    text = format_results(_round(WinRule.MINORITY, {0: 2, 1: 3, 2: 4}, winner=0))
    assert "на волоске" in text
    assert "ещё 1 голос" in text


def test_results_no_flip_line_for_blowout() -> None:
    """Разгром не притворяется драмой: строки про волосок нет."""
    text = format_results(_round(WinRule.MAJORITY, {0: 50, 1: 1, 2: 0}, winner=0))
    assert "на волоске" not in text


def test_results_keeps_tie_note(test_rules_round) -> None:
    text = format_results(test_rules_round)
    assert "жребий правила" in text


@pytest.fixture
def test_rules_round():
    from types import SimpleNamespace

    return SimpleNamespace(
        day_index=1,
        win_rule=WinRule.MEDIAN,
        winner_card=2,
        vote_counts_json='{"0": 0, "1": 1, "2": 1}',
        tie_note="Голоса разделились (II и III) — жребий правила по обязательству дня выбрал путь III.",
        cards=[
            SimpleNamespace(
                position=0, title="Сон вповалку", consequence="с0",
                food_cost=0, water_cost=0, health_risk=0, trust_change=0,
                emotional_consequence="", npc_reactions_json="[]",
            ),
            SimpleNamespace(
                position=1, title="Чужое имя", consequence="с1",
                food_cost=0, water_cost=0, health_risk=0, trust_change=0,
                emotional_consequence="", npc_reactions_json="[]",
            ),
            SimpleNamespace(
                position=2, title="Красный сигнал", consequence="с2",
                food_cost=0, water_cost=0, health_risk=0, trust_change=0,
                emotional_consequence="", npc_reactions_json="[]",
            ),
        ],
    )


async def test_teaser_job_sends_once_then_marker_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Тизер уходит один раз за день; победитель и цифры не раскрываются."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        round_row = Round(
            day_index=700_100,
            status=RoundStatus.TALLYING,
            win_rule=WinRule.MINORITY,
            rule_commitment="c",
            chapter_title="t",
            chapter_text="text",
            lore_summary="lore",
            opens_at=now - timedelta(hours=24),
            voting_ends_at=now - timedelta(minutes=45),
            tally_ends_at=now + timedelta(minutes=15),
            winner_card=None,
            vote_counts_json="{}",
        )
        session.add(round_row)
        await session.commit()

        sent: list[str] = []

        async def fake_whisper(bot, text) -> int:
            sent.append(text)
            return len(sent)

        async def fake_teaser(day_index, rule_phrase) -> str:
            assert rule_phrase  # фраза закона дня дошла до генератора
            return "Тизер без цифр."

        monkeypatch.setattr("app.broadcast.whisper_to_chats", fake_whisper)
        monkeypatch.setattr("app.story.generate_teaser", fake_teaser)
        try:
            await _teaser_job(round_row.id)
            assert len(sent) == 1 and "Тизер без цифр." in sent[0]
            # Цифры и победитель не раскрываются даже случайно.
            assert str(round_row.winner_card) not in sent[0]
            await _teaser_job(round_row.id)
            assert len(sent) == 1  # маркер в watcher_state не даёт повторить
        finally:
            await session.execute(
                WatcherState.__table__.delete().where(
                    WatcherState.key == f"teaser:{round_row.id}"
                )
            )
            await session.delete(round_row)
            await session.commit()
