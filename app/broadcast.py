"""Рассылка анонсов дней и итогов в чаты, где бот состоит администратором."""

from __future__ import annotations

import asyncio
import logging
import random
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


def cards_keyboard(round_id: int, remember: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="Путь I", callback_data=f"vote:{round_id}:0"),
            InlineKeyboardButton(text="Путь II", callback_data=f"vote:{round_id}:1"),
            InlineKeyboardButton(text="Путь III", callback_data=f"vote:{round_id}:2"),
        ],
    ]
    if remember:
        # Кнопка памяти живёт только в дни, когда в главу реально всплыло эхо.
        rows.append(
            [
                InlineKeyboardButton(
                    text="🧠 Я помню этот след",
                    callback_data=f"remember:{round_id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _clamp(text: str, limit: int) -> str:
    """Обрезка с многоточием, чтобы служебные строки не вытеснялись из поста."""
    return text if len(text) <= limit else text[: limit - 1].rstrip(" ,.;:") + "…"


def status_text(round_row: Round) -> str:
    from app.models import RULE_PHRASES

    mark = day_mark(str(round_row.id))
    sealed = bool(getattr(round_row, "sealed", False))
    if round_row.status.value == "open":
        if sealed:
            commitment = round_row.rule_commitment.split(":")[0][:12]
            phase = (
                "🗝 Закон дня: ЗАПЕЧАТАН архивом до итогов. "
                f"Обязательство: {commitment}…"
            )
        else:
            phase = f"⚖️ Закон дня: {RULE_PHRASES[round_row.win_rule]}. Счёт скрыт до итогов."
    elif round_row.status.value == "tallying":
        phase = "⏳ Голосование закрыто. Идёт час подсчёта."
    else:
        phase = "🌙 День закрыт."
    # Описания путей живут в подписях фото (лимит подписи 1024), поэтому
    # текстовый пост остаётся главе и фазе — больше воздуха для истории.
    cards = "\n".join(
        f"{POSITIONS[card.position]}. {_clamp(card.title, 100)}"
        for card in sorted(round_row.cards, key=lambda item: item.position)
    )
    bank_line = ""
    if settings.ton_enabled:
        from app.rounds import get_cached_pot

        nano, _bets = get_cached_pot(round_row.id)
        if nano:
            bank_line = f"\n💰 Банк дня: {nano / 1e9:.2f} Gram"
    text = (
        f"{mark} {round_row.chapter_title}\n\n{_clamp(round_row.chapter_text, 3200)}\n\n"
        f"{cards}\n\n{phase}{bank_line}\n"
        f"🗳 Голосование до: {round_row.voting_ends_at:%H:%M} UTC · "
        f"🏁 Итоги и новый день: {round_row.tally_ends_at:%H:%M} UTC"
    )
    season_line = _season_status_line(round_row)
    if season_line:
        text += f"\n{season_line}"
    return text[:_MAX_TEXT_LEN]


def _season_status_line(round_row: Round) -> str | None:
    """Строка сезона в статусе дня: акты забега и дистанция до Первого Лая.

    Якорь забега берётся из кэша season (обновляется при каждой генерации
    дня и тике), поэтому синхронный код обходится без БД.
    """
    try:
        from app.season import (
            act_line,
            get_cached_anchor,
            is_run_finale,
            run_position,
        )

        moment = round_row.voting_ends_at
        if getattr(moment, "tzinfo", None) is None:
            from datetime import timezone as _tz

            moment = moment.replace(tzinfo=_tz.utc)
        run_day, total = run_position(get_cached_anchor(moment), moment)
        if is_run_finale(run_day, total):
            return "🐺 Сегодня — День Первого Лая. Финал сезона."
        line = f"🌙 {act_line(run_day, total)}"
        from app.prologue import prologue_title

        title = prologue_title(run_day)
        if title:
            line += f" Пролог дня: «{title}»."
        return line
    except Exception:
        return None


def _card_media(card) -> InputMediaPhoto:
    path = Path(card.image_path)
    if not path.exists():
        render_card(path, card.title, card.description, card.position)
    # Подпись несёт полное описание пути (лимит Telegram — 1024 знака):
    # игрок читает развилку прямо на картинке, не отрываясь от истории.
    caption = (
        f"{path_mark(getattr(card, 'tag', 'care'), str(card.round_id) + str(card.position))} "
        f"Путь {POSITIONS[card.position]}. {_clamp(card.title, 100)}\n\n"
        f"{_clamp(card.description, 700)}"
    )
    return InputMediaPhoto(media=FSInputFile(path), caption=caption)


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
    remember: bool = False,
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
        reply_markup=cards_keyboard(round_row.id, remember=remember),
    )


async def _deliver_chat(
    bot: Bot,
    chat_id: int,
    round_row: Round,
    finished: Round | None,
    results_text: str | None,
    remember: bool = False,
) -> int | None:
    """Доставка в чат с одним ретраем после флуд-контроля. None — неудача."""
    try:
        await _deliver_day(bot, chat_id, round_row, finished, results_text, remember=remember)
        return chat_id
    except TelegramRetryAfter as exc:
        logger.warning("Флуд-контроль в чате %s: пауза %d с", chat_id, exc.retry_after)
        await asyncio.sleep(exc.retry_after + 1)
        await _deliver_day(bot, chat_id, round_row, finished, results_text, remember=remember)
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
    # Кнопка памяти — только когда в главу дня реально всплыло эхо.
    remember = False
    try:
        from app.echoes import surfaced_echoes_for_round

        async with SessionLocal() as session:
            remember = bool(await surfaced_echoes_for_round(session, round_row.day_index))
    except Exception:
        logger.warning("Не удалось проверить всплытие эха дня %s", round_row.day_index, exc_info=True)
    semaphore = asyncio.Semaphore(_BROADCAST_PARALLELISM)

    async def worker(chat_id: int) -> int | None:
        async with semaphore:
            return await _deliver_chat(bot, chat_id, round_row, finished, results_text, remember=remember)

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


# Личное эхо проигравшим: у каждого голосовавшего «не туда» остаётся личная
# незакрытая ветка — причина вернуться к следующей развилке. Только публичные
# данные (карты дня видны всем), никаких цифр и механики.
_PERSONAL_ECHO_TEMPLATES = (
    (
        "Стая пошла иначе — за «{winner}». Твоя тропа «{title}» никуда не делась: "
        "{consequence} Такие тропы мир помнит — иногда они всплывают там, где их "
        "не ждут. Новая развилка уже открыта: один выбор на всех."
    ),
    (
        "Ты звал стаю на «{title}», но она ушла за «{winner}». Несостоявшийся путь "
        "остался приметой мира: {consequence} Вечером станет видно, чья дорога "
        "была дальновиднее. Сегодняшние карты уже ждут."
    ),
    (
        "«{title}» — твой вчерашний путь — не стал каноном: стая выбрала "
        "«{winner}». Но невыбранное здесь не исчезает: {consequence} Мир запомнил "
        "и это. Загляни на новую развилку, когда сможешь."
    ),
    (
        "Стая свернула к «{winner}», а твоя тропа «{title}» осталась тлеть на "
        "обочине: {consequence} Здесь нет неверных дорог — есть недожитые. "
        "Новая развилка открыта."
    ),
    (
        "Вчера ты был за «{title}», стая — за «{winner}». След твоего пути "
        "вплетён в мир: {consequence} Через несколько дней его можно узнать на "
        "тропе. А пока — новый день и новые карты."
    ),
)


def personal_echo_text(
    seed_key: str, loser_title: str, loser_consequence: str, winner_title: str
) -> str:
    """Детерминированное личное сообщение проигравшему: один игрок в один день
    всегда получает одну и ту же формулировку."""
    rng = random.Random(f"pecho:{seed_key}")
    template = _PERSONAL_ECHO_TEMPLATES[rng.randrange(len(_PERSONAL_ECHO_TEMPLATES))]
    return template.format(
        title=_clamp(loser_title.strip(), 80),
        consequence=_clamp(loser_consequence.strip(), 240),
        winner=_clamp(winner_title.strip(), 80),
    )


async def send_personal_echoes(bot: Bot | None, finished) -> int:
    """Личное эхо каждому, кто голосовал мимо победившего пути.

    Читает голоса закрытого дня из базы, пишет только в личку игрока
    (chat_id = player_id); недоставленные сообщения молча пропускаются —
    бот не имеет права писать тем, кто его не начинал. Возвращает число
    доставленных сообщений.
    """
    if bot is None or not settings.personal_echo or finished is None:
        return 0
    winner_pos = getattr(finished, "winner_card", None)
    cards = {card.position: card for card in finished.cards}
    winner = cards.get(winner_pos)
    if winner_pos is None or winner is None:
        return 0
    from app.models import Vote

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Vote.player_id, Vote.card_position).where(Vote.round_id == finished.id)
            )
        ).all()
        # Окраска эха: призвание сильнее, иначе — клетка Следа.
        from app.callings import echo_tail
        from app.models import Player as _Player
        from app.trail import trail_stats, trail_tint_line

        loser_ids = [pid for pid, pos in rows if pos != winner_pos]
        tail_map: dict[int, str] = {}
        if loser_ids:
            calling_rows = (
                await session.execute(
                    select(_Player.id, _Player.calling).where(_Player.id.in_(loser_ids))
                )
            ).all()
            for pid, calling in calling_rows:
                tail = echo_tail(calling)
                if tail:
                    tail_map[pid] = tail
            # Без призвания окраску даёт След (если уже проявился).
            for pid in loser_ids:
                if pid in tail_map:
                    continue
                try:
                    tint = trail_tint_line(await trail_stats(session, pid))
                except Exception:
                    tint = None
                if tint:
                    tail_map[pid] = tint
    losers = [(pid, pos) for pid, pos in rows if pos != winner_pos]
    semaphore = asyncio.Semaphore(_BROADCAST_PARALLELISM)

    async def worker(player_id: int, position: int) -> bool:
        card = cards.get(position)
        if card is None:
            return False
        text = personal_echo_text(
            f"{finished.id}:{player_id}", card.title, card.consequence, winner.title
        )
        tail = tail_map.get(player_id)
        if tail:
            text += f"\n\n{tail}"
        async with semaphore:
            try:
                await bot.send_message(player_id, text)
                return True
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
                try:
                    await bot.send_message(player_id, text)
                    return True
                except Exception:
                    return False
            except Exception:
                return False

    outcomes = await asyncio.gather(*(worker(pid, pos) for pid, pos in losers))
    delivered = sum(1 for ok in outcomes if ok)
    logger.info(
        "Личное эхо дня %d доставлено %d из %d проигравших",
        getattr(finished, "day_index", "?"),
        delivered,
        len(losers),
    )
    return delivered
