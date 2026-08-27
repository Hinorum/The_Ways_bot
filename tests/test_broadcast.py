"""Рассылка дня: падение одного чата не мешает дню открыться в остальных."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from unittest.mock import AsyncMock
from types import SimpleNamespace

from aiogram.exceptions import TelegramForbiddenError

from app.broadcast import _deliver_day, announce_new_day
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


def test_status_bank_line_shows_amount_only(monkeypatch, tmp_path) -> None:
    """Банк дня в посте — только сумма, без числа ставок."""
    from app import rounds as rounds_mod
    from app.broadcast import status_text

    monkeypatch.setattr(settings, "ton_enabled", True)
    round_row = _round(9400, tmp_path)
    rounds_mod._POT_CACHE[round_row.id] = (1_250_000_000, 3)
    try:
        text = status_text(round_row)
        assert "Банк дня: 1.25 Gram" in text
        assert "ставок" not in text
    finally:
        rounds_mod._POT_CACHE.pop(round_row.id, None)

    # Пустой банк — строки нет вовсе.
    text = status_text(round_row)
    assert "Банк дня" not in text


def test_status_carries_paths_and_media_is_single_cover(tmp_path) -> None:
    """Новый мир: один сетевой кадр в день.

    Описания путей вернулись в текст статуса (фото-карт больше нет), медиа
    дня — только обложка «после вчерашнего выбора». Легаси-дни с готовыми
    фото-картами по-прежнему показывают четыре кадра.
    """
    from app.broadcast import day_media_group, status_text

    # Новый день: карты без генерации.
    round_row = _round(9300, tmp_path)
    for card in round_row.cards:
        card.image_path = ""
    status = status_text(round_row)
    for position in range(3):
        assert f"{['I', 'II', 'III'][position]}. Путь {position} — описание" in status
    assert len(status) <= 4096

    media = day_media_group(round_row)
    assert len(media) == 1  # только обложка
    assert "День проверки рассылки" in media[0].caption

    # Легаси-день: у всех карт есть файлы — показываем четыре кадра.
    legacy = _round(9301, tmp_path)
    legacy_media = day_media_group(legacy)
    assert len(legacy_media) == 4
    captions = [item.caption for item in legacy_media[1:]]
    for position, caption in enumerate(captions):
        assert f"Путь {['I', 'II', 'III'][position]}." in caption
        assert len(caption) <= 1024  # лимит Telegram на подпись фото


def test_intro_frame_attached_only_to_day_one(tmp_path, monkeypatch) -> None:
    """Стартовый кадр мира едет в посте первого дня забега и больше нигде."""
    from app.broadcast import day_media_group

    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    intro = tmp_path / "run_intro.jpg"
    intro.write_bytes(b"")

    first_day = _round(1, tmp_path)
    for card in first_day.cards:
        card.image_path = ""
    media = day_media_group(first_day)
    assert len(media) == 2  # обложка + стартовый кадр мира
    assert "Еретик" in media[1].caption

    second_day = _round(2, tmp_path)
    for card in second_day.cards:
        card.image_path = ""
    assert len(day_media_group(second_day)) == 1  # обычный день — без интро


def _finished(day_index: int, media_dir) -> Round:
    finished = _round(day_index, media_dir)
    finished.status = RoundStatus.CLOSED
    finished.sealed = False
    finished.winner_card = 1
    finished.vote_counts_json = '{"0":2,"1":1,"2":4}'
    return finished


async def test_finished_day_results_sent_as_text_no_photo(tmp_path, monkeypatch) -> None:
    """Итоги дня — только текстом: без фото победившей ветки (это был дубль
    обложки нового дня). Текст «Итог дня» уходит обычным сообщением."""
    media_dir = tmp_path
    monkeypatch.setattr(settings, "media_dir", str(media_dir))
    finished = _finished(9110, media_dir)
    next_day = _round(9111, media_dir)
    bot = _bot({})
    results = "🎊 Итог дня 1\n📜 Канон: Путь 1\nканон-текст"

    await _deliver_day(bot, 777_010, next_day, finished, results_text=results)

    sent_texts = [c.args[1] if len(c.args) > 1 else "" for c in bot.send_message.await_args_list]
    assert results in sent_texts
    # Ни один фото-пост не несёт «Итог дня» в подписи: картинку итога не шлём.
    for call in bot.send_photo.await_args_list:
        caption = call.kwargs.get("caption", "") or ""
        assert "Итог дня" not in caption
