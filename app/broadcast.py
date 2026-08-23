"""Рассылка анонсов дней и итогов в чаты, где бот состоит администратором."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Chat, Round
from app.story import render_card, render_cover
from app.style import day_mark, path_mark
from app.tally import format_results

logger = logging.getLogger(__name__)

POSITIONS = ("I", "II", "III")
_MAX_TEXT_LEN = 3900
_FORGET_MARKS = ("forbidden", "not found", "kicked", "deactivated", "migrated")


def cards_keyboard(round_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Путь I", callback_data=f"vote:{round_id}:0"),
                InlineKeyboardButton(text="Путь II", callback_data=f"vote:{round_id}:1"),
                InlineKeyboardButton(text="Путь III", callback_data=f"vote:{round_id}:2"),
            ],
            [
                InlineKeyboardButton(
                    text="🧠 Я помню этот след",
                    callback_data=f"remember:{round_id}",
                )
            ],
        ]
    )


def _clamp(text: str, limit: int) -> str:
    """Обрезка с многоточием, чтобы служебные строки не вытеснялись из поста."""
    return text if len(text) <= limit else text[: limit - 1].rstrip(" ,.;:") + "…"


def status_text(round_row: Round) -> str:
    from app.models import RULE_PHRASES

    mark = day_mark(str(round_row.id))
    if round_row.status.value == "open":
        phase = f"⚖️ Закон дня: {RULE_PHRASES[round_row.win_rule]}. Счёт скрыт до итогов."
    elif round_row.status.value == "tallying":
        phase = "⏳ Голосование закрыто. Идёт час подсчёта."
    else:
        phase = "🌙 День закрыт."
    cards = "\n\n".join(
        f"{POSITIONS[card.position]}. {_clamp(card.title, 100)}\n{_clamp(card.description, 280)}"
        for card in sorted(round_row.cards, key=lambda item: item.position)
    )
    text = (
        f"{mark} {round_row.chapter_title}\n\n{_clamp(round_row.chapter_text, 1200)}\n\n"
        f"{cards}\n\n{phase}\n"
        f"🗳 Голосование до: {round_row.voting_ends_at:%H:%M} UTC · "
        f"🏁 Итоги и новый день: {round_row.tally_ends_at:%H:%M} UTC"
    )
    season_line = _season_status_line(round_row)
    if season_line:
        text += f"\n{season_line}"
    return text[:_MAX_TEXT_LEN]


def _season_status_line(round_row: Round) -> str | None:
    """Строка сезона в статусе дня: акты месяца и дистанция до Первого Лая."""
    try:
        from app.season import act_line, day_of_season, days_in_season, season_key

        moment = round_row.voting_ends_at
        if getattr(moment, "tzinfo", None) is None:
            from datetime import timezone as _tz

            moment = moment.replace(tzinfo=_tz.utc)
        total = days_in_season(season_key(moment))
        if day_of_season(moment) == total:
            return "🐺 Сегодня — День Первого Лая. Финал сезона."
        return f"🌙 {act_line(day_of_season(moment), total)}"
    except Exception:
        return None


def _card_media(card) -> InputMediaPhoto:
    path = Path(card.image_path)
    if not path.exists():
        render_card(path, card.title, card.description, card.position)
    return InputMediaPhoto(
        media=FSInputFile(path),
        caption=f"{path_mark(getattr(card, 'tag', 'care'), str(card.round_id) + str(card.position))} Путь {POSITIONS[card.position]}. {card.title}",
    )


def _cover_media(round_row: Round) -> InputMediaPhoto:
    """Обложка дня: сюжет главы. Пропавший файл рисуется локально на месте."""
    if round_row.cover_path:
        path = Path(round_row.cover_path)
    else:
        path = Path(settings.media_dir) / f"day{round_row.day_index}_cover.jpg"
    if not path.exists():
        render_cover(path, round_row.chapter_title, round_row.chapter_text)
    return InputMediaPhoto(
        media=FSInputFile(path),
        caption=f"{day_mark(str(round_row.id))} {round_row.chapter_title[:1000]}",
    )


def day_media_group(round_row: Round) -> list[InputMediaPhoto]:
    """Свежий набор медиа на каждый чат: aiogram мутирует объекты при отправке."""
    return [
        _cover_media(round_row),
        *(_card_media(card) for card in sorted(round_row.cards, key=lambda item: item.position)),
    ]


async def active_chat_ids() -> list[int]:
    async with SessionLocal() as session:
        rows = await session.execute(select(Chat.id).where(Chat.active.is_(True)))
        return [row[0] for row in rows.all()]


async def deactivate_chat(chat_id: int) -> None:
    async with SessionLocal() as session:
        row = await session.get(Chat, chat_id)
        if row is not None and row.active:
            row.active = False
            await session.commit()
            logger.info("Чат %s помечен неактивным", chat_id)


async def results_message(finished: Round, session=None) -> str:
    """Сухие итоги + экономика дня + эпилог от нейросети (если он написан).

    session можно передать готовую (тесты, вызовы внутри транзакции);
    иначе открывается своя краткоживущая сессия.
    """
    from app.tally import day_economics, format_economics

    text = format_results(finished)
    round_id = getattr(finished, "id", None)
    if round_id is not None:
        if session is not None:
            row = await session.get(Round, round_id)
        else:
            async with SessionLocal() as fresh_session:
                row = await fresh_session.get(Round, round_id)
        if row is not None:
            stats = await day_economics(session, row) if session is not None else (
                await _economics_own_session(row)
            )
            economics = format_economics(stats)
            if economics:
                text += f"\n\n{economics}"
    epilogue = getattr(finished, "epilogue_text", "") or ""
    if epilogue:
        text += f"\n\n{epilogue}"
    return text


async def _economics_own_session(row: Round) -> dict:
    from app.tally import day_economics

    async with SessionLocal() as own:
        return await day_economics(own, row)


def winner_card(round_row: Round):
    return next(
        (card for card in round_row.cards if card.position == round_row.winner_card),
        None,
    )


def winner_photo(round_row: Round) -> FSInputFile | None:
    """Фото победившей карты для поста итогов: файл уже сгенерирован днём ранее.

    Пропавший файл дорисовывается локальным шаблоном; карты без победителя
    (пустой день) фото не получают.
    """
    card = winner_card(round_row)
    if card is None:
        return None
    path = Path(card.image_path or "")
    if not path.exists():
        render_card(path, card.title, card.description, card.position)
    return FSInputFile(path)


_BROADCAST_PARALLELISM = 8


async def _deliver_day(
    bot: Bot,
    chat_id: int,
    round_row: Round,
    finished: Round | None,
    results_text: str | None = None,
) -> None:
    """Полный пакет дня в один чат. Итоги передаются готовым текстом:
    экономика дня считается один раз на рассылку, а не на каждый чат."""
    if finished is not None:
        photo = winner_photo(finished)
        if photo is not None:
            card = winner_card(finished)
            await bot.send_photo(
                chat_id,
                photo=photo,
                caption=f"Канон дня: {card.title}"[:1000],
            )
        if results_text is None:
            results_text = await results_message(finished)
        await bot.send_message(chat_id, results_text)
    media = day_media_group(round_row)
    if media:
        await bot.send_media_group(chat_id, media=media)
    await bot.send_message(
        chat_id,
        status_text(round_row),
        reply_markup=cards_keyboard(round_row.id),
    )


async def _deliver_chat(
    bot: Bot,
    chat_id: int,
    round_row: Round,
    finished: Round | None,
    results_text: str | None,
) -> int | None:
    """Доставка в чат с одним ретраем после флуд-контроля. None — неудача."""
    try:
        await _deliver_day(bot, chat_id, round_row, finished, results_text)
        return chat_id
    except TelegramRetryAfter as exc:
        logger.warning("Флуд-контроль в чате %s: пауза %d с", chat_id, exc.retry_after)
        await asyncio.sleep(exc.retry_after + 1)
        await _deliver_day(bot, chat_id, round_row, finished, results_text)
        return chat_id
    except TelegramForbiddenError:
        await deactivate_chat(chat_id)
        return None
    except Exception as exc:
        logger.warning(
            "Анонс дня %s не доставлен в чат %s: %s", round_row.day_index, chat_id, exc
        )
        lowered = str(exc).lower()
        if any(mark in lowered for mark in _FORGET_MARKS):
            await deactivate_chat(chat_id)
        return None


async def announce_new_day(
    bot: Bot | None,
    round_row: Round,
    finished: Round | None = None,
) -> list[int]:
    """Итоги прошлого дня (если передан) + обложка и карты нового дня.

    Чаты доставляются параллельно ограниченным пулом: последовательная
    рассылка (~7 сообщений и 4 аплоада на чат) упирается в часы уже на
    сотнях чатов. Возвращает список чатов, куда рассылка прошла успешно.
    """
    if bot is None:
        return []
    chat_ids = await active_chat_ids()
    results_text = await results_message(finished) if finished is not None else None
    semaphore = asyncio.Semaphore(_BROADCAST_PARALLELISM)

    async def worker(chat_id: int) -> int | None:
        async with semaphore:
            return await _deliver_chat(bot, chat_id, round_row, finished, results_text)

    outcomes = await asyncio.gather(*(worker(chat_id) for chat_id in chat_ids))
    delivered = [chat_id for chat_id in outcomes if chat_id is not None]
    logger.info(
        "Анонс дня %s разослан: доставлено %d из %d чатов",
        round_row.day_index,
        len(delivered),
        len(chat_ids),
    )
    return delivered


async def whisper_to_chats(bot: Bot | None, text: str) -> int:
    """Полуденный шёпот мира: короткое сообщение во все живые чаты.

    Возвращает число доставленных чатов; провалы не критичны по определению.
    """
    if bot is None or not text:
        return 0
    chat_ids = await active_chat_ids()
    semaphore = asyncio.Semaphore(_BROADCAST_PARALLELISM)

    async def worker(chat_id: int) -> bool:
        async with semaphore:
            try:
                await bot.send_message(chat_id, text)
                return True
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
                try:
                    await bot.send_message(chat_id, text)
                    return True
                except Exception:
                    return False
            except TelegramForbiddenError:
                await deactivate_chat(chat_id)
                return False
            except Exception as exc:
                lowered = str(exc).lower()
                if any(mark in lowered for mark in _FORGET_MARKS):
                    await deactivate_chat(chat_id)
                return False

    outcomes = await asyncio.gather(*(worker(c) for c in chat_ids))
    delivered = sum(1 for ok in outcomes if ok)
    logger.info("Шёпот дня разослан в %d из %d чатов", delivered, len(chat_ids))
    return delivered
