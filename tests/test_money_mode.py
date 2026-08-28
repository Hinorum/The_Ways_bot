"""Версия игры: рубильник хранителя (WatcherState), снимок режима на день
(Round.money_mode), скрытие банка и гейт открытого дня."""

from datetime import datetime, timedelta, timezone

from app import ops, rounds
from app.broadcast import status_text
from app.config import settings
from app.db import SessionLocal
from app.handlers import _active_round_money_mode
from app.models import Card, Round, RoundStatus, WinRule


def _round_row(money_mode: bool, day_index: int, _id: int, with_cards: bool = False) -> Round:
    round_row = Round(
        id=_id,
        day_index=day_index,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        rule_commitment="c:s",
        chapter_title="День версии",
        chapter_text="Текст.",
        lore_summary="лор",
        cover_path="",
        money_mode=money_mode,
        opens_at=datetime.now(timezone.utc),
        voting_ends_at=datetime.now(timezone.utc) + timedelta(hours=23),
        tally_ends_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    if with_cards:
        for position in range(3):
            round_row.cards.append(
                Card(
                    round_id=_id,
                    position=position,
                    title=f"Путь {position}",
                    description="описание",
                    consequence="канон",
                    tag="care",
                    image_path="",
                )
            )
    return round_row


async def _delete_round(round_id: int) -> None:
    async with SessionLocal() as session:
        await session.execute(Round.__table__.delete().where(Round.id == round_id))
        await session.commit()


async def test_money_mode_defaults_on_and_toggles() -> None:
    """Отсутствие ключа = денежная версия; двойной тап — no-op."""
    async with SessionLocal() as session:
        assert await ops.money_mode_enabled(session) is True
        assert await ops.set_money_mode(session, False) is True
        assert await ops.money_mode_enabled(session) is False
        assert await ops.set_money_mode(session, False) is False  # тот же тап
        assert await ops.set_money_mode(session, True) is True
        assert await ops.money_mode_enabled(session) is True


async def test_stamp_persists_snapshot_on_round() -> None:
    """День снимает режим на материализацию: свободный день остаётся свободным,
    даже если рубильник потом вернули."""
    async with SessionLocal() as session:
        await ops.set_money_mode(session, False)
        free_day = _round_row(money_mode=True, day_index=6, _id=87_900)
        await rounds._stamp_day_money_mode(session, free_day)
        session.add(free_day)
        await session.commit()
    try:
        async with SessionLocal() as session:
            row = await session.get(Round, free_day.id)
        assert row is not None and row.money_mode is False
    finally:
        await _delete_round(free_day.id)


async def test_free_day_hides_bank_line(tmp_path, monkeypatch) -> None:
    """День без ставок не показывает банк, даже при настроенном TON;
    денежный день — показывает."""
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    rounds._POT_CACHE[87_910] = (3_000_000_000, 5)
    rounds._POT_CACHE[87_911] = (3_000_000_000, 5)
    mono = settings.ton_enabled
    settings.ton_enabled = True
    try:
        free_day = _round_row(money_mode=False, day_index=9, _id=87_910, with_cards=True)
        money_day = _round_row(money_mode=True, day_index=10, _id=87_911, with_cards=True)
        assert "Банк дня" not in status_text(free_day)
        assert "Банк дня: 3.00 Gram" in status_text(money_day)
    finally:
        settings.ton_enabled = mono
        rounds._POT_CACHE.pop(87_910, None)
        rounds._POT_CACHE.pop(87_911, None)


async def test_active_day_uses_its_snapshot() -> None:
    """Открытый день живёт по своему снимку, а не по живому рубильнику."""
    async with SessionLocal() as session:
        await ops.set_money_mode(session, False)
    money_day = _round_row(money_mode=True, day_index=7, _id=87_901)
    free_day = _round_row(money_mode=False, day_index=8, _id=87_902)
    try:
        async with SessionLocal() as session:
            session.add_all([money_day, free_day])
            await session.commit()
        # Последний открытый день — свободный: гейт дня = False.
        async with SessionLocal() as session:
            row = await session.get(Round, free_day.id)
            row.day_index = 998
            await session.commit()
        assert await _active_round_money_mode() is False
        # Снимок поверх вычеркнут — денежный день открыт и ближе всего.
        async with SessionLocal() as session:
            row = await session.get(Round, money_day.id)
            row.day_index = 999
            await session.commit()
        assert await _active_round_money_mode() is True
    finally:
        await _delete_round(money_day.id)
        await _delete_round(free_day.id)