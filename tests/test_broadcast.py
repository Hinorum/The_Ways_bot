"""Рассылка дня: падение одного чата не мешает дню открыться в остальных."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from unittest.mock import AsyncMock
from types import SimpleNamespace

from aiogram.exceptions import TelegramForbiddenError

from app.broadcast import announce_new_day
from app.config import settings
from app.db import SessionLocal
from app.models import Card, Chat, Round, RoundStatus, WinRule


def _round(day_index: int, media_dir) -> Round:
    cover = f"day{day_index}_cover.jpg"
    (media_dir / cover).write_bytes(b"")
    round_row = Round(
        id=90_000 + day_index,
        day_index=day_index,
        status=RoundStatus.OPEN,
        win_rule=WinRule.MAJORITY,
        rule_commitment="c:s",
        chapter_title="День проверки рассылки",
        chapter_text="Текст.",
        lore_summary="лор",
        cover_path=str(media_dir / cover),
        opens_at=datetime.now(timezone.utc),
        voting_ends_at=datetime.now(timezone.utc) + timedelta(hours=23),
        tally_ends_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    for position in range(3):
        name = f"day{day_index}_card{position}.jpg"
        (media_dir / name).write_bytes(b"")
        round_row.cards.append(
            Card(
                round_id=round_row.id,
                position=position,
                title=f"Путь {position}",
                description="описание",
                consequence="канон",
                tag="care",
                image_path=str(media_dir / name),
            )
        )
    return round_row


def _bot(side_effects: dict[int, Exception]) -> SimpleNamespace:
    async def sender(chat_id, *args, **kwargs):
        exc = side_effects.get(chat_id)
        if exc is not None:
            raise exc
        return SimpleNamespace()

    return SimpleNamespace(
        send_photo=AsyncMock(side_effect=sender),
        send_media_group=AsyncMock(side_effect=sender),
        send_message=AsyncMock(side_effect=sender),
    )


async def test_forbidden_chat_does_not_block_day(tmp_path, monkeypatch) -> None:
    media_dir = tmp_path
    monkeypatch.setattr(settings, "media_dir", str(media_dir))
    """Заблокировавший бота чат деактивируется; день доходит до остальных."""
    blocked, healthy = 777_001, 777_002
    async with SessionLocal() as db:
        db.add_all([Chat(id=blocked, type="group", active=True), Chat(id=healthy, type="group", active=True)])
        await db.commit()
    try:
        bot = _bot({blocked: TelegramForbiddenError(None, "bot was blocked by the user")})
        delivered = await announce_new_day(bot, _round(9100, media_dir), finished=None)
        assert delivered == [healthy]
        assert bot.send_media_group.await_count == 2  # попытались обоим, дошло одному
        async with SessionLocal() as db:
            rows = {
                row.id: row.active
                for row in (await db.execute(select(Chat).where(Chat.id.in_([blocked, healthy])))).scalars()
            }
        assert rows == {blocked: False, healthy: True}
    finally:
        async with SessionLocal() as cleanup:
            await cleanup.execute(Chat.__table__.delete().where(Chat.id.in_([blocked, healthy])))
            await cleanup.execute(Round.__table__.delete().where(Round.day_index >= 9000))
            await cleanup.commit()


async def test_transient_failure_does_not_stop_other_chats(tmp_path, monkeypatch) -> None:
    media_dir = tmp_path
    monkeypatch.setattr(settings, "media_dir", str(media_dir))
    """Сетевой сбой в одном чате не отменяет рассылку и не деактивирует его."""
    flaky, healthy = 777_003, 777_004
    async with SessionLocal() as db:
        db.add_all([Chat(id=flaky, type="group", active=True), Chat(id=healthy, type="group", active=True)])
        await db.commit()
    try:
        bot = _bot({flaky: ValueError("telegram timeout")})
        delivered = await announce_new_day(bot, _round(9101, media_dir), finished=None)
        assert delivered == [healthy]
        async with SessionLocal() as db:
            rows = {
                row.id: row.active
                for row in (await db.execute(select(Chat).where(Chat.id.in_([flaky, healthy])))).scalars()
            }
        # «Не похоже на удаление» — чат остаётся активным, попробуем в следующий раз.
        assert rows == {flaky: True, healthy: True}
    finally:
        async with SessionLocal() as cleanup:
            await cleanup.execute(Chat.__table__.delete().where(Chat.id.in_([flaky, healthy])))
            await cleanup.execute(Round.__table__.delete().where(Round.day_index >= 9000))
            await cleanup.commit()


async def test_migrated_chat_is_forgotten(tmp_path, monkeypatch) -> None:
    media_dir = tmp_path
    monkeypatch.setattr(settings, "media_dir", str(media_dir))
    gone, alive = 777_005, 777_006
    async with SessionLocal() as db:
        db.add_all([Chat(id=gone, type="group", active=True), Chat(id=alive, type="group", active=True)])
        await db.commit()
    try:
        bot = _bot({gone: ValueError("chat not found")})
        delivered = await announce_new_day(bot, _round(9102, media_dir), finished=None)
        assert delivered == [alive]
        async with SessionLocal() as db:
            row = await db.get(Chat, gone)
        assert row is not None and row.active is False
    finally:
        async with SessionLocal() as cleanup:
            await cleanup.execute(Chat.__table__.delete().where(Chat.id.in_([gone, alive])))
            await cleanup.execute(Round.__table__.delete().where(Round.day_index >= 9000))
            await cleanup.commit()


async def test_no_bot_no_broadcast() -> None:
    assert await announce_new_day(None, SimpleNamespace(day_index=1)) == []


def test_status_hides_descriptions_and_captions_carry_them(tmp_path) -> None:
    """Описания путей переехали в подписи фото: пост дня остаётся главе."""
    from app.broadcast import day_media_group, status_text

    round_row = _round(9300, tmp_path)
    status = status_text(round_row)
    assert "описание" not in status  # описания больше не в текстовом посте
    for position in range(3):
        assert f"{['I', 'II', 'III'][position]}. Путь {position}" in status

    media = day_media_group(round_row)
    captions = [item.caption for item in media[1:]]
    assert len(captions) == 3
    for position, caption in enumerate(captions):
        assert f"Путь {['I', 'II', 'III'][position]}." in caption
        assert "описание" in caption  # полное описание живёт на картинке пути
        assert len(caption) <= 1024  # лимит Telegram на подпись фото
