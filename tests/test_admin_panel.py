"""Пульт хранителя (/panel): состояние игры + справочник команд.

Админ должен видеть банк дня, очередь выплат и здоровье watcher'а одним
нажатием, с кнопкой обновления — без раскрытия игрокам.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import handlers as handlers_module
from app.config import settings
from app.db import SessionLocal
from app.handlers import _admin_panel_text, cmd_panel, on_panel_view
from app.models import Payout, Round, RoundStatus, WinRule
from app.rounds import _POT_CACHE


def make_message(uid: int) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=uid),
        text="/panel",
        answer=AsyncMock(),
    )


def make_callback(uid: int) -> SimpleNamespace:
    return SimpleNamespace(
        data="panel:view",
        from_user=SimpleNamespace(id=uid),
        message=SimpleNamespace(edit_text=AsyncMock(), chat=SimpleNamespace(type="private")),
        answer=AsyncMock(),
    )


async def test_panel_is_admin_only(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", "4242")
    outsider = make_message(1)
    await cmd_panel(outsider)
    assert "только для хранителя" in outsider.answer.call_args.args[0]


async def test_panel_builder_contains_core_sections(session, monkeypatch) -> None:
    """Панель читает ГЛОБАЛЬНУЮ базу (как прод): сеем туда и чистим после."""
    monkeypatch.setattr(settings, "admin_ids", "4242")
    monkeypatch.setattr(settings, "ton_enabled", True)
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        db.add(Round(
            day_index=97_500,
            status=RoundStatus.OPEN,
            win_rule=WinRule.MAJORITY,
            rule_commitment="c",
            chapter_title="t",
            chapter_text="x",
            lore_summary="l",
            opens_at=now,
            voting_ends_at=now + timedelta(hours=10),
            tally_ends_at=now + timedelta(hours=11),
            season="2026-08",
        ))
        db.add(Payout(
            round_id=None,
            kind="prize",
            amount_nanotons=300_000_000,
            dest_address="0:" + "ab" * 32,
            status="failed",
            attempts=5,
            last_error="have no alive peers: задай LITESERVER_CONFIG_URL"[:200],
        ))
        await db.commit()
    _POT_CACHE[97_500] = (1_250_000_000, 3)

    try:
        async with SessionLocal() as g:
            text = await _admin_panel_text(g)
        assert "ПУЛЬТ ХРАНИТЕЛЯ" in text
        assert "День 97500 · open" in text
        assert "Банк дня: 1.25 Gram · ставок 3" in text
        assert "Акт" in text and "до Лая" in text  # строка забега
        assert "Выплаты:" in text and "failed 1" in text
        assert "#1" in text or "#2" in text  # топ долгов в панели
        assert "LITESERVER_CONFIG_URL" in text  # причина видна прямо тут
        assert "/resetgame confirm" in text  # справочник команд на месте
    finally:
        _POT_CACHE.pop(97_500, None)
        from sqlalchemy import delete as _d

        async with SessionLocal() as db:
            await db.execute(_d(Payout).where(Payout.status == "failed"))
            await db.execute(_d(Round).where(Round.day_index == 97_500))
            await db.commit()


async def test_panel_refresh_callback_edits_for_admin(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", "4242")
    callback = make_callback(4242)

    async def fake_text(session=None):
        return "🎛 ПУЛЬТ ХРАНИТЕЛЯ"

    monkeypatch.setattr(handlers_module, "_admin_panel_text", fake_text)
    await on_panel_view(callback)
    callback.message.edit_text.assert_awaited_once()
    callback.answer.assert_awaited_once()

    # Не-хранителю обновление закрыто.
    outsider = make_callback(1)
    await on_panel_view(outsider)
    args, kwargs = outsider.answer.call_args
    assert kwargs.get("show_alert") is True
