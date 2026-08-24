"""Личное эхо проигравшим: только проигравшие, только публичные данные."""

from datetime import datetime, timedelta, timezone

from app.broadcast import personal_echo_text, send_personal_echoes
from app.config import settings
from app.db import SessionLocal
from app.models import Card, Round, RoundStatus, Vote, WinRule


def test_personal_echo_text_is_deterministic_and_in_world() -> None:
    first = personal_echo_text("77:1001", "Кабель в зубах", "Кабель удержался.", "Тёплые миски")
    second = personal_echo_text("77:1001", "Кабель в зубах", "Кабель удержался.", "Тёплые миски")
    assert first == second  # один игрок в один день — одно сообщение
    assert "Кабель в зубах" in first
    assert "Тёплые миски" in first
    assert len(first) <= 900


async def test_send_personal_echoes_targets_losers_only(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    messages: dict[int, str] = {}

    class FakeBot:
        async def send_message(self, chat_id, text):
            messages[chat_id] = text

    async with SessionLocal() as db:
        rnd = Round(
            id=88_001,
            day_index=8800,
            status=RoundStatus.CLOSED,
            win_rule=WinRule.MAJORITY,
            rule_commitment="c",
            chapter_title="t",
            chapter_text="text",
            lore_summary="lore",
            cover_path="",
            opens_at=now - timedelta(hours=25),
            voting_ends_at=now - timedelta(hours=2),
            tally_ends_at=now - timedelta(hours=1),
            winner_card=1,
            vote_counts_json='{"0": 2, "1": 5}',
        )
        for position in range(3):
            rnd.cards.append(
                Card(
                    round_id=rnd.id,
                    position=position,
                    title=f"Путь {position}",
                    description="описание",
                    consequence=f"канон {position}",
                    tag="care",
                    image_path="",
                )
            )
        db.add(rnd)
        await db.flush()
        # Игрок 101 угадал; 102 и 103 голосовали мимо победителя.
        db.add_all(
            [
                Vote(round_id=rnd.id, player_id=101, card_position=1),
                Vote(round_id=rnd.id, player_id=102, card_position=0),
                Vote(round_id=rnd.id, player_id=103, card_position=2),
            ]
        )
        await db.commit()
        try:
            delivered = await send_personal_echoes(FakeBot(), rnd)
            assert delivered == 2
            assert set(messages) == {102, 103}  # победителя не беспокоим
            for chat_id, text in messages.items():
                loser_position = 0 if chat_id == 102 else 2
                assert f"Путь {loser_position}" in text
                assert f"канон {loser_position}" in text
                assert "канон 1" not in text  # последствие победителя не светим

            monkeypatch.setattr(settings, "personal_echo", False)
            assert await send_personal_echoes(FakeBot(), rnd) == 0
            assert await send_personal_echoes(None, rnd) == 0
        finally:
            await db.execute(Vote.__table__.delete().where(Vote.round_id == rnd.id))
            await db.execute(Card.__table__.delete().where(Card.round_id == rnd.id))
            await db.execute(Round.__table__.delete().where(Round.day_index >= 8000))
            await db.commit()


async def test_send_personal_echoes_without_winner_is_noop() -> None:
    from types import SimpleNamespace

    finished = SimpleNamespace(winner_card=None, cards=[], id=1, day_index=1)
    assert await send_personal_echoes(object(), finished) == 0
