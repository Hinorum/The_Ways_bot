"""Рассылка анонсов дней и итогов в чаты, где бот состоит администратором."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
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


def cards_keyboard(round_id: int, remember: bool = False, day_index: int | None = None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="Путь I", callback_data=f"vote:{round_id}:0"),
            InlineKeyboardButton(text="Путь II", callback_data=f"vote:{round_id}:1"),
            InlineKeyboardButton(text="Путь III", callback_data=f"vote:{round_id}:2"),
        ],
    ]
    if remember:
        # Кнопка памяти живёт только в дни, когда в главу реально всплыло эхо.
        # Кодируем и PK раунда (для отметки MemoryHit), и его day_index (для
        # поиска всплывших эхо) — после /resetgame id уже не равен day_index.
        if day_index is None:
            day_index = round_id
        rows.append(
            [
                InlineKeyboardButton(
                    text="🧠 Я помню этот след",
                    callback_data=f"remember:{round_id}:{day_index}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _clamp(text: str, limit: int) -> str:
    """Обрезка с многоточием, чтобы служебные строки не вытеснялись из поста."""
    return text if len(text) <= limit else text[: limit - 1].rstrip(" ,.;:") + "…"


def _utc(value: datetime) -> datetime:
    return value if getattr(value, "tzinfo", None) else value.replace(tzinfo=timezone.utc)


def status_text(
    round_row: Round, *, show_title: bool = True, include_story: bool = True
) -> str:
    from app.models import RULE_PHRASES

    sealed = bool(getattr(round_row, "sealed", False))
    if round_row.status.value == "open":
        if sealed:
            commitment = round_row.rule_commitment.split(":")[0][:12]
            phase = (
                "🗝 Закон дня запечатан архивом до итогов. "
                f"Обязательство: {commitment}…"
            )
        else:
            phase = f"⚖️ Закон дня: {RULE_PHRASES[round_row.win_rule]}. Счёт скрыт до итогов."
    elif round_row.status.value == "tallying":
        phase = "⏳ Подсчёт: итоги через мгновение."
    else:
        phase = "🌙 День закрыт."
    # Пути голосования читаются словами: заголовок + суть каждого.
    # (Раньше описания жили в подписях трёх фото-карт — генерацию карт
    # убрали, и текст снова стал носителем смысла развилки.)
    # Компактный профиль: промпт просит карту не длиннее 210 знаков, а показ
    # здесь даёт задел до 260 — текст развилки не режется многоточием.
    cards = "\n".join(
        f"{POSITIONS[card.position]}. {_clamp(card.title, 80)} — {_clamp(card.description, 260)}"
        for card in sorted(round_row.cards, key=lambda item: item.position)
    )
    bank_line = ""
    if settings.ton_enabled and getattr(round_row, "money_mode", True) is not False:
        from app.rounds import get_cached_pot

        nano, _bets = get_cached_pot(round_row.id)
        if nano:
            bank_line = f"\n💰 Банк дня: {nano / 1e9:.2f} Gram"
    # Бесшовные сутки: подсчёт мгновенный, оба времени совпадают — хватит
    # одного дедлайна. Легаси-раунды с зазором показывают обе строки.
    voting_at = _utc(round_row.voting_ends_at)
    tally_at = _utc(round_row.tally_ends_at)
    if tally_at - voting_at > timedelta(minutes=5):
        deadline = (
            f"🗳 Голосование до: {voting_at:%H:%M} UTC · "
            f"🏁 Итоги и новый день: {tally_at:%H:%M} UTC"
        )
    else:
        deadline = f"🗳 Голосование до {voting_at:%H:%M} UTC — итоги и новый день придут сразу после"
    banner = _season_banner_line(round_row)
    # Дубль заголовка (инцидент): титул и история могут жить на обложке — тогда
    # status_text их не повторяет (show_title/include_story false). Для легаси-дней
    # без объединённой подписи титул и глава возвращаются в текстовый пост.
    head = ""
    if show_title:
        head += f"{day_mark(str(round_row.id))} {round_row.chapter_title}\n\n"
    if include_story:
        head += f"{_clamp(round_row.chapter_text, 2600)}\n\n"
    text = (
        f"{head}{cards}\n\n{phase}{bank_line}\n{deadline}"
    )
    if banner:
        text = f"📢 {banner}\n\n{text}"
    season_line = _season_status_line(round_row)
    if season_line:
        text += f"\n{season_line}"
    return text[:_MAX_TEXT_LEN]


def _season_banner_line(round_row: Round) -> str | None:
    """Баннер нового сезона: показывается один раз в день 1 сезона >1."""
    try:
        from app.season import get_cached_anchor, season_banner

        moment = round_row.opens_at or round_row.voting_ends_at
        if getattr(moment, "tzinfo", None) is None:
            from datetime import timezone as _tz

            moment = moment.replace(tzinfo=_tz.utc)
        cached = get_cached_anchor(moment)
        return season_banner(cached, moment)
    except Exception:
        return None


def _season_status_line(round_row: Round) -> str | None:
    """Строка сезона в статусе дня: показывает обратный отсчёт только в кризисе.

    Якорь забега берётся из кэша season (обновляется при каждой генерации
    дня и тике), поэтому синхронный код обходится без БД.
    """
    try:
        from app.season import (
            crisis_act,
            get_cached_anchor,
            is_run_finale,
            run_days_left,
            run_position,
        )

        moment = round_row.opens_at or round_row.voting_ends_at
        if getattr(moment, "tzinfo", None) is None:
            from datetime import timezone as _tz

            moment = moment.replace(tzinfo=_tz.utc)
        cached = get_cached_anchor(moment)
        run_day, total = run_position(cached, moment)
        if is_run_finale(run_day, total):
            return "🐺 Сегодня — День Первого Лая. Финал сезона."
        if not crisis_act(run_day, total):
            return None
        left = run_days_left(run_day, total)
        return f"🐺 До финала: {left} дн."
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


def _intro_media(round_row: Round) -> InputMediaPhoto | None:
    """Стартовый кадр мира: прикладывается только к дню 1 забега."""
    if round_row.day_index != 1:
        return None
    path = Path(settings.media_dir) / "run_intro.jpg"
    if not path.exists():
        return None
    caption = (
        f"{day_mark(str(round_row.id))} Мир, который Еретик построил для тех, "
        "кому стал тесен один сон на всех. Выбирай тропу — реальность перестроится."
    )
    return InputMediaPhoto(media=FSInputFile(path), caption=caption[:1000])


def _merged_cover_caption(round_row: Round) -> str | None:
    """Подпись обложки с историей ЦЕЛИКОМ — если влезает в лимит 1024.

    Жалоба хранителя: заголовок дня дублировался в подписи фото и в начале
    текстового поста. Короткие главы объединяются с картинкой в один пост;
    длинные едут отдельным сообщением, но титул всё равно не дублируется
    (status_text(show_title=False)).
    """
    body = (round_row.chapter_text or "").strip()
    if not body:
        return None
    caption = f"{day_mark(str(round_row.id))} {round_row.chapter_title}\n\n{body}"
    return caption if len(caption) <= 1024 else None


def build_day_post(round_row: Round) -> tuple[list[InputMediaPhoto], bool]:
    """Медиа дня + флаг «история уже внутри подписи обложки».

    Новый мир: обложка (объединённая подпись, если помещается) (+ стартовый
    кадр в день 1). Легаси-дни с готовыми фото-картами показываются как раньше.
    """
    media = [_cover_media(round_row)]
    cards = sorted(round_row.cards, key=lambda item: item.position)
    legacy = bool(cards) and all(
        card.image_path and Path(card.image_path).exists() for card in cards
    )
    if legacy:
        media.extend(_card_media(card) for card in cards)
        return media, False
    merged_caption = _merged_cover_caption(round_row)
    if merged_caption is not None:
        media[0].caption = merged_caption
        merged = True
    else:
        merged = False
    intro = _intro_media(round_row)
    if intro is not None:
        media.append(intro)
    return media, merged


def day_media_group(round_row: Round) -> list[InputMediaPhoto]:
    """Совместимая обёртка: только медиа (без флага слияния)."""
    media, _story_in_caption = build_day_post(round_row)
    return media


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


async def active_player_ids() -> list[int]:
    """Игроки, подписанные на личные дубликаты рассылок (/start → dm_subscribed)."""
    from app.models import Player

    async with SessionLocal() as session:
        rows = await session.execute(
            select(Player.id).where(Player.dm_subscribed.is_(True))
        )
        return [row[0] for row in rows.all()]


async def _dm_send_all(bot: Bot, deliver, label: str) -> int:
    """Рассылка одного сообщения всем подписанным игрокам в личку.

    Промахи не критичны: бот не имеет права писать тем, кто его не начинал
    (forbidden) — их молча пропускаем, как в личном эхе. Возвращает число
    доставленных сообщений.
    """
    if bot is None or not settings.player_dm:
        return 0
    player_ids = await active_player_ids()
    if not player_ids:
        return 0
    semaphore = asyncio.Semaphore(_BROADCAST_PARALLELISM)

    async def worker(player_id: int) -> bool:
        async with semaphore:
            try:
                await deliver(player_id)
                return True
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
                try:
                    await deliver(player_id)
                    return True
                except Exception:
                    return False
            except Exception:
                return False

    outcomes = await asyncio.gather(*(worker(pid) for pid in player_ids))
    delivered = sum(1 for ok in outcomes if ok)
    logger.info("%s: доставлено %d из %d игроков", label, delivered, len(player_ids))
    return delivered


async def results_body(finished: Round, session=None) -> str:
    """Сухие итоги + экономика дня (БЕЗ нейро-эпилога).

    Быстрая, только БД — это то, что уходит пользователям СРАЗУ после вскрытия
    итогов, пока эпилог ещё пишется нейросетью. session можно передать готовую.
    """
    from app.tally import day_economics, format_economics

    text = format_results(finished)
    round_id = getattr(finished, "id", None)
    if round_id is not None:
        try:
            if session is not None:
                stats = await day_economics(session, finished)
            else:
                stats = await _economics_own_session(finished)
            economics = format_economics(stats)
            if economics:
                text += f"\n\n{economics}"
        except Exception:
            logger.exception("Экономика дня %s не посчитана", getattr(finished, "day_index", "?"))
    return text


async def results_message(finished: Round, session=None) -> str:
    """Полные итоги дня: сухой блок + экономика + эпилог от нейросети (если готов).

    session можно передать готовую (тесты, вызовы внутри транзакции);
    иначе открывается своя краткоживущая сессия.
    """
    text = await results_body(finished, session)
    epilogue = getattr(finished, "epilogue_text", "") or ""
    if epilogue:
        text += f"\n\n{epilogue}"
    return text


async def _economics_own_session(row: Round) -> dict:
    from app.tally import day_economics

    async with SessionLocal() as own:
        return await day_economics(own, row)


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
        if results_text is None:
            results_text = await results_message(finished)
        # Итоги дня — только текстом. Фото победившей ветки не постим: это был
        # дубль обложки нового дня, а вечерний костёр уже дал отдельный кадр.
        if results_text:
            await bot.send_message(chat_id, results_text)
    media, story_in_caption = build_day_post(round_row)
    if len(media) >= 2:
        await bot.send_media_group(chat_id, media=media)
    elif media:
        # Инцидент-регрессия: Telegram принимает mediaGroup только от двух
        # вложений. Новый мир даёт один кадр дня — шлём обычным фото, иначе
        # анонс падал ПОСЛЕ обложки и статус с кнопками голосования не уходил.
        await bot.send_photo(chat_id, photo=media[0].media, caption=media[0].caption)
    await bot.send_message(
        chat_id,
        status_text(
            round_row,
            show_title=not story_in_caption,
            include_story=not story_in_caption,
        ),
        reply_markup=cards_keyboard(round_row.id, remember=remember, day_index=round_row.day_index),
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
    """Обложка и карты НОВОГО дня; итоги прошлого дня — если передан finished.

    В автопереходе итоги постятся отдельно (announce_results) сразу после
    вскрытия, а сюда новый день передаётся без finished — чтобы не дублировать
    итоги. Ручной /advance по-прежнему передаёт finished и постит всё вместе.

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
    # Личные дубликаты подписчикам: тот же пакет дня (итоги, обложка, кнопки
    # выбора) в личку. Без /start у игрока бот писать не может — такие молча
    # пропускаются; кнопки голосования работают из лички, как и из группы.
    if settings.player_dm:
        delivered_dm = await _dm_send_all(
            bot,
            lambda pid: _deliver_day(
                bot, pid, round_row, finished, results_text, remember=remember
            ),
            f"Личный пакет дня {round_row.day_index}",
        )
        if delivered_dm:
            logger.info(
                "Личный пакет дня %s доставлен %d игроку(ам)",
                round_row.day_index,
                delivered_dm,
            )
    return delivered


async def _broadcast_text(bot: Bot, text: str) -> int:
    """Одно текстовое сообщение во все живые чаты с одним ретраем и флуд-контролем.

    Плюс — личные дубликаты подписчикам (итоги, эпилог, анонсы пауз/церемоний).
    Возвращает число доставленных чатов; провалы не критичны.
    """
    if not text.strip():
        return 0
    chat_ids = await active_chat_ids()
    if not chat_ids:
        logger.warning("_broadcast_text: нет активных чатов для рассылки (все деактивированы?)")
    if chat_ids:
        semaphore = asyncio.Semaphore(_BROADCAST_PARALLELISM)

        async def worker(chat_id: int) -> int | None:
            async with semaphore:
                try:
                    await bot.send_message(chat_id, text)
                    return chat_id
                except TelegramRetryAfter as exc:
                    logger.warning("Флуд-контроль в чате %s: пауза %d с", chat_id, exc.retry_after)
                    await asyncio.sleep(exc.retry_after + 1)
                    try:
                        await bot.send_message(chat_id, text)
                        return chat_id
                    except Exception:
                        return None
                except TelegramForbiddenError:
                    await deactivate_chat(chat_id)
                    return None
                except Exception as exc:
                    logger.warning("Текст не доставлен в чат %s: %s", chat_id, exc)
                    if any(mark in str(exc).lower() for mark in _FORGET_MARKS):
                        await deactivate_chat(chat_id)
                    return None

        outcomes = await asyncio.gather(*(worker(chat_id) for chat_id in chat_ids))
        delivered = len([c for c in outcomes if c is not None])
    else:
        delivered = 0
    # Личные дубликаты подписчикам — даже если живых чатов нет.
    delivered_dm = await _dm_send_all(
        bot, lambda pid: bot.send_message(pid, text), "Личный текст"
    )
    return delivered + delivered_dm


async def announce_results(bot: Bot | None, finished: Round) -> int:
    """Постит СРАЗУ только итоги прошлого дня (без нейро-эпилога и без нового дня).

    Отделено от announce_new_day, чтобы итоги уходили пользователям немедленно
    после вскрытия, не дожидаясь нейро-контента нового дня. Возвращает число
    доставленных чатов.
    """
    if bot is None:
        return 0
    try:
        text = await results_body(finished)
    except Exception:
        logger.exception("Итоги дня %s не собраны — отдаём сухой шаблон", getattr(finished, "day_index", "?"))
        text = ""
    if not text:
        return 0
    delivered = await _broadcast_text(bot, text)
    logger.info("Итоги дня %s разосланы: доставлено %d чатов", getattr(finished, "day_index", "?"), delivered)
    return delivered


async def announce_epilogue(bot: Bot | None, finished: Round) -> int:
    """Доносит нейро-эпилог отдельным коротким постом, когда он дописан.

    Итоги ушли сразу без эпилога; здесь эпилог приходит следом. Возвращает
    число доставленных чатов.
    """
    if bot is None:
        return 0
    epilogue = (getattr(finished, "epilogue_text", "") or "").strip()
    if not epilogue:
        return 0
    delivered = await _broadcast_text(bot, epilogue)
    logger.info("Эпилог дня %s разослан: доставлено %d чатов", getattr(finished, "day_index", "?"), delivered)
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
    # Вечерний привал — и в личку подписчикам (личный дубликат вечернего поста).
    delivered_dm = await _dm_send_all(
        bot, lambda pid: bot.send_message(pid, text), "Личный шёпот (текст)"
    )
    return delivered + delivered_dm


async def whisper_photo_to_chats(bot: Bot | None, photo, caption: str) -> int:
    """Вечерний кадр: фото с подписью-микросценой во все живые чаты.

    Параллелен whisper_to_chats, только мимо send_photo (чтобы кадр костра
    летел вместе с текстом). Возвращает число доставленных чатов.
    """
    if bot is None or not caption:
        return 0
    # Лимит подписи фото в Telegram — 1024 знака. Не даём длинной микросцене
    # ронять send_photo и молча лишать все чаты вечернего кадра.
    caption = caption[:1024]
    chat_ids = await active_chat_ids()
    semaphore = asyncio.Semaphore(_BROADCAST_PARALLELISM)

    async def worker(chat_id: int) -> bool:
        async with semaphore:
            try:
                await bot.send_photo(chat_id, photo=photo, caption=caption)
                return True
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
                try:
                    await bot.send_photo(chat_id, photo=photo, caption=caption)
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
    logger.info("Вечерний кадр доставлен в %d из %d чатов", delivered, len(chat_ids))
    # Вечерний кадр костра — и в личку подписчикам (самим фото с подписью).
    delivered_dm = await _dm_send_all(
        bot,
        lambda pid: bot.send_photo(pid, photo=photo, caption=caption),
        "Личный вечерний кадр",
    )
    return delivered + delivered_dm


# Личное эхо проигравшим: у каждого голосовавшего «не туда» остаётся личная
# незакрытая ветка — причина вернуться к следующей развилке. Только публичные
# данные (карты дня видны всем), никаких цифр и механики.
_PERSONAL_ECHO_TEMPLATES = (
    (
        "Стая пошла за «{winner}», но твоя тропа «{title}» не исчезла: "
        "{consequence} Мир помнит невыбранное — оно всплывает там, где "
        "его не ждут. Новая развилка открыта."
    ),
    (
        "Ты звал стаю на «{title}» — она ушла за «{winner}». "
        "{consequence} Несостоявшаяся тропа стала приметой мира. "
        "Сегодняшние карты уже ждут."
    ),
    (
        "«{title}» не стал каноном — стая выбрала «{winner}». "
        "{consequence} Но невыбранное здесь не умирает. "
        "Мир запомнил и это."
    ),
    (
        "Стая свернула к «{winner}», а твоя тропа «{title}» тлеет на обочине: "
        "{consequence} Здесь нет неверных дорог — есть недожитые."
    ),
    (
        "Вчера ты был за «{title}», стая — за «{winner}». "
        "{consequence} След твоего пути вплетён в мир. "
        "Скоро его можно будет узнать на тропе."
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
