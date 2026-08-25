from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from sqlalchemy import func, select

from app.broadcast import (
    POSITIONS,
    announce_new_day,
    cards_keyboard,
    day_media_group,
    results_message,
    status_text,
)
from app.config import settings
from app.db import SessionLocal
from app.models import (
    Chat,
    Income,
    LeaderboardPot,
    Payout,
    Player,
    RevoteGrant,
    Round,
    RoundStatus,
    Stake,
    StoryBeat,
    Vote,
    WalletDialog,
)
from app.payments import build_revote_payload, parse_revote_payload, revote_memo
from app.rounds import claim_announcement, close_voting, create_next_round_detailed, ensure_current_round, finish_tally, get_active_round, get_latest_round, get_round, patch_prepared_day, reset_game, write_epilogue
from app.style import day_mark, hint_mark, money_mark, ok_mark, path_mark, result_mark, warn_mark
from app.tally import award_points
from app.ton_pay import pending_payout_count
from app.ton_utils import from_nano, friendly_address, is_valid_ton_address, normalize_address
from app.voting import cast_vote, change_vote, get_vote, upsert_player


logger = logging.getLogger(__name__)

router = Router()

_ACTIVE_STATUSES = {"member", "administrator", "creator"}


async def _ensure_round():
    async with SessionLocal() as session:
        return await ensure_current_round(session)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    async with SessionLocal() as session:
        await upsert_player(session, message.from_user)
    lines = [
        f"{day_mark(str(message.from_user.id))} Это «{settings.world_name}» — фанатская игра по мотивам Lost Dogs.",
        "Раз в сутки — три карты. Каждое утро объявляется закон дня: "
        "побеждает карта с большим, меньшим или средним числом голосов — "
        "так что видно, в какую сторону голосовать. Цифры голосов скрыты до итогов.",
        "",
        "Сутки Стаи: голосование идёт 23 часа, затем час подсчёта. "
        "Итоги и новый день приходят ровно через сутки после открытия — "
        "всегда в одно и то же время.",
        "",
        "/today — карты дня",
        "/lore — канон",
        "/score — твои Следы",
    ]
    if settings.revote_enabled:
        lines.append("/change — сменить выбор (⭐ Stars или Gram)")
    if settings.ton_enabled:
        lines.append("/wallet — привязать кошелёк для ставок")
        lines.append("/stake — как поставить Gram на путь")
        lines.append("/top — копилка месяца и лидеры")
        lines.append(
            "\nФонд дня: 97% — поставившим на верный путь пропорционально, "
            "2% — копилка недели (/top), 0,5% — копилка месяца (/top), "
            "0,5% — на поддержку Стаи."
        )
    lines.append(
        f"\n{settings.world_name} — игра, а не вклад: бот и хранитель не отвечают за "
        "утраченные средства. Ты сам решаешь, на что ставить, и сам за это отвечаешь."
    )
    await message.answer("\n".join(lines))
    await cmd_today(message)


async def _remember_flag(day_index: int) -> bool:
    """Кнопка памяти живёт только в дни с реально всплывшим эхом."""
    from app.echoes import surfaced_echoes_for_round

    try:
        async with SessionLocal() as session:
            return bool(await surfaced_echoes_for_round(session, day_index))
    except Exception:
        logger.warning("Флаг памяти дня %s не проверен", day_index, exc_info=True)
        return False


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    round_row = await _ensure_round()
    try:
        media = day_media_group(round_row)
        if media:
            await message.answer_media_group(media)
    except Exception as exc:
        # Картинки — украшение, текст дня обязан дойти даже при сбое Telegram
        # или пропавших файлов: игрок должен видеть развилку и кнопки.
        logger.warning("Медиа-группа дня %s не ушла (%s) — доставляем текст", round_row.day_index, exc)
    await message.answer(
        status_text(round_row),
        reply_markup=cards_keyboard(round_row.id, remember=await _remember_flag(round_row.day_index)),
    )


@router.message(Command("lore"))
async def cmd_lore(message: Message) -> None:
    async with SessionLocal() as session:
        beats = (await session.execute(select(StoryBeat).order_by(StoryBeat.day_index))).scalars().all()
    if not beats:
        await message.answer(f"{hint_mark('lore-empty')} Канон ещё пуст: первый След появится после итогов дня.")
        return
    text, truncated = _canon_text(beats)
    if truncated:
        text = f"{hint_mark('lore-cut')} Ранние дни растворились в шуме порталов.\n\n" + text
    await message.answer(text)


def _canon_text(beats) -> tuple[str, bool]:
    """Канон в хронологическом порядке, но окно от свежих дней к старым.

    Возвращает (текст, урезано ли начало). Лимит — 3500 символов сообщения.
    """
    chosen: list[str] = []
    total = 0
    for beat in reversed(beats):
        chunk = f"День {beat.day_index}. {beat.winning_title}\n{beat.winning_text}"
        if total + len(chunk) + 2 > 3500:
            return "\n\n".join(reversed(chosen)), True
        chosen.append(chunk)
        total += len(chunk) + 2
    return "\n\n".join(reversed(chosen)), False


def _personal_keyboard(action: str, label: str) -> InlineKeyboardMarkup:
    """Кнопка личных данных: окно по нажатию видит только тот, кто нажал."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=action)]])


async def _chronicle(session, player_id: int, limit: int = 7) -> list[str]:
    """Личная хроника сезона: последние дни игрока — путь и его исход.

    Голоса и итоги уже лежат в базе; хроника просто собирает их в биографию.
    """
    from app.models import Card

    rows = (
        await session.execute(
            select(Round.day_index, Card.title, Vote.card_position == Round.winner_card)
            .join(Vote, Vote.round_id == Round.id)
            .join(Card, (Card.round_id == Round.id) & (Card.position == Vote.card_position))
            .where(Vote.player_id == player_id, Round.status == RoundStatus.CLOSED)
            .order_by(Round.day_index.desc())
            .limit(limit)
        )
    ).all()
    return [
        f"Д{day} · {title} {'🏆' if won else '·'}"
        for day, title, won in rows
    ]


async def _score_text(user) -> str:
    async with SessionLocal() as session:
        player = await upsert_player(session, user)
        round_row = await get_active_round(session) or await get_latest_round(session)
        vote = await get_vote(session, round_row.id, player.id) if round_row else None
        from sqlalchemy import func as _func

        from app.models import MemoryHit

        from sqlalchemy import select as _select

        memory_hits = (
            await session.execute(
                _select(_func.count()).select_from(MemoryHit).where(MemoryHit.player_id == player.id)
            )
        ).scalar_one()
        chronicle = await _chronicle(session, player.id)
        from app.callings import calling_by_key
        from app.trail import trail_line, trail_stats

        calling = calling_by_key(player.calling)
        try:
            stats = await trail_stats(session, player.id)
        except Exception:
            stats = None
    if vote is None:
        choice = f"{hint_mark(str(user.id))} Сегодня ты ещё не выбрал путь."
    elif round_row.status in (RoundStatus.OPEN, RoundStatus.TALLYING):
        choice = f"{path_mark('care', str(user.id))} Сегодня твой путь: {POSITIONS[vote.card_position]}."
    else:
        choice = f"В прошлом дне ты выбрал путь {POSITIONS[vote.card_position]}."
    text = (
        f"{choice}\n{result_mark(f'score:{user.id}')} Очки: {player.score}\n"
        f"Угаданных законов: {player.correct_picks}\n"
        f"🧠 Память сети: {memory_hits}\n"
        f"✨ Второй нюх: {player.inspiration}"
    )
    if calling is not None:
        text += f"\n{calling.emoji} Призвание: {calling.title}."
    else:
        text += "\nПризвание ещё не выбрано — /calling"
    if stats is not None:
        text += f"\n{trail_line(stats)}"
    if chronicle:
        text += "\n\n📜 Твоя хроника:\n" + "\n".join(chronicle)
    return text


def _sniff_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✨ Потратить нюх", callback_data="sniff:use")]]
    )


@router.message(Command("score"))
async def cmd_score(message: Message) -> None:
    text = await _score_text(message.from_user)
    if message.chat.type == ChatType.PRIVATE:
        keyboard = None
        async with SessionLocal() as session:
            player = await upsert_player(session, message.from_user)
            if player.inspiration > 0:
                keyboard = _sniff_keyboard()
        await message.answer(text, reply_markup=keyboard)
        return
    # В группе личные цифры не показываем: только кнопка с приватным окном.
    await message.answer(
        "Твой счёт увидишь только ты — нажми кнопку.",
        reply_markup=_personal_keyboard("score:view", "Мой счёт"),
    )


@router.callback_query(F.data == "score:view")
async def on_score_view(callback: CallbackQuery) -> None:
    if callback.message is not None and callback.message.chat.type == ChatType.PRIVATE:
        await callback.message.answer(await _score_text(callback.from_user))
        await callback.answer()
        return
    # Лимит окна — 200 символов, счёт компактный и помещается.
    text = await _score_text(callback.from_user)
    await callback.answer(text[:200], show_alert=True)


def _calling_list_text(current, available) -> str:
    from app.callings import CALLINGS

    lines = ["🎭 Призвания Стаи: косметика мира — титул, эхо и касание в главах."]
    for calling in CALLINGS:
        unlocked = any(c.key == calling.key for c in available)
        marker = "✅" if current and current.key == calling.key else ("▫️" if unlocked else "🔒")
        suffix = ""
        if not unlocked:
            field, minimum = calling.requirement
            names = {
                "correct_picks": ("верных путей", minimum),
                "heart_lead": ("заботливых путей вперёд", minimum),
                "minority_correct": ("верных в ночь Одинокого Волка", minimum),
                "memory_hits": ("находок памяти", minimum),
                "votes": ("дней у карт", minimum),
                "sealed_correct": ("верный в Слепой Яме", minimum),
            }
            label = names.get(field, (field, minimum))
            suffix = f" (нужно {label[1]} {label[0]})"
        lines.append(f"{marker} {calling.emoji} {calling.title}{suffix}\n   {calling.description}")
    return "\n".join(lines)


def _calling_keyboard(available, current_key: str | None) -> InlineKeyboardMarkup:
    rows = []
    for calling in available:
        if calling.key == current_key:
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{calling.emoji} Стать: {calling.title}",
                    callback_data=f"calling:pick:{calling.key}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows or [[InlineKeyboardButton(text="Пока так", callback_data="noop")]])


@router.message(Command("calling"))
async def cmd_calling(message: Message) -> None:
    from app.callings import available_callings, calling_by_key

    async with SessionLocal() as session:
        player = await upsert_player(session, message.from_user)
        available = await available_callings(session, player.id)
        current = calling_by_key(player.calling)
    head = (
        f"{current.emoji} Сейчас ты — {current.title}."
        if current is not None
        else "Призвание пока не выбрано. Мир уже смотрит, кем ты станешь."
    )
    await message.answer(
        f"{head}\n\n{_calling_list_text(current, available)}",
        reply_markup=_calling_keyboard(available, player.calling),
    )


@router.callback_query(F.data.startswith("calling:pick:"))
async def on_calling_pick(callback: CallbackQuery) -> None:
    key = callback.data.rsplit(":", 1)[-1]
    from app.callings import available_callings, calling_by_key

    async with SessionLocal() as session:
        player = await upsert_player(session, callback.from_user)
        available = await available_callings(session, player.id)
        target = calling_by_key(key)
        if target is None or all(c.key != key for c in available):
            await callback.answer("Это призвание ещё закрыто.", show_alert=True)
            return
        player.calling = key
        await session.commit()
    await callback.answer(f"Линька прошла. Теперь ты — {target.title}.")
    if callback.message is not None and callback.message.chat.type == ChatType.PRIVATE:
        await callback.message.answer(
            f"{target.emoji} Линька прошла: теперь ты — {target.title}. {target.description}"
        )


_SNIFF_SCENES = (
    "Ты лежишь у карт в «{place}» и вдруг чуешь тропу, которой ещё нет на столе. "
    "{calling_title} умеет ждать: нюх говорит «не сегодня» — но говорит точно.",
    "Вечер в «{place}». Ты поднимаешь морду: где-то за порталами кто-то пересчитывает "
    "стаю заново. {calling_title} не считает — он слышит, когда счёт сбивается.",
    "Короткий отдых в «{place}»: миски остывают, а твой нюх греется о чужое воспоминание. "
    "{calling_title} знает: память — тоже провизия.",
    "Ночь у карт в «{place}». Ты перебираешь запахи дня, как папки Архивариуса. "
    "{calling_title} откладывает один запах на завтра: пригодится.",
    "Во сне в «{place}» ты видишь тропу без карт. {calling_title} просыпается раньше всех "
    "и молчит об этом до итогов.",
)


def compose_sniff_scene(
    seed_key: str, calling, place: str | None, trail_tint: str | None = None
) -> str:
    """Личная микросцена за жетон: детерминированная, офлайн, без информации о законе."""
    import random as _random

    rng = _random.Random(f"sniff:{seed_key}")
    template = _SNIFF_SCENES[rng.randrange(len(_SNIFF_SCENES))]
    scene = template.format(
        place=place or "кружке порталов",
        calling_title=f"«{calling.title}»" if calling is not None else "Собака стаи",
    )
    if trail_tint:
        scene += f"\n{trail_tint}"
    return scene


@router.callback_query(F.data == "sniff:use")
async def on_sniff_use(callback: CallbackQuery) -> None:
    """Трата жетона: личная микросцена дня. Один раз в день, только в личке."""
    from app.models import WatcherState

    if callback.message is None or callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Нюх тратят из личного чата.", show_alert=True)
        return
    async with SessionLocal() as session:
        player = await upsert_player(session, callback.from_user)
        round_row = await get_active_round(session)
        if round_row is None:
            await callback.answer("Сейчас нет открытого дня для нюха.", show_alert=True)
            return
        if player.inspiration <= 0:
            await callback.answer("Жетонов нет: ищи следы памяти и верные серии.", show_alert=True)
            return
        marker = f"sniff:{player.id}:{round_row.id}"
        if await session.get(WatcherState, marker) is not None:
            await callback.answer("Сегодня нюх уже потрачен.", show_alert=True)
            return
        player.inspiration -= 1
        session.add(WatcherState(key=marker, value="1"))
        await session.commit()
        from app.callings import calling_by_key
        from app.trail import trail_stats, trail_tint_line

        calling = calling_by_key(player.calling)
        place = getattr(round_row, "place", None) if round_row is not None else None
        try:
            tint = trail_tint_line(await trail_stats(session, player.id))
        except Exception:
            tint = None
        scene = compose_sniff_scene(str(marker), calling, place, trail_tint=tint)
    await callback.answer()
    try:
        await callback.message.answer(scene)
    except Exception:
        pass


@router.callback_query(F.data == "noop")
async def on_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.message(Command("best"))
async def cmd_best(message: Message) -> None:
    from app.bestiary import bestiary_text

    async with SessionLocal() as session:
        text = await bestiary_text(session)
    await message.answer(text)


@router.callback_query(F.data.startswith("remember:"))
async def on_remember(callback: CallbackQuery) -> None:
    """«Я помню этот след» — старт квиза: откуда всплывший след?

    Кнопка живёт только в дни с реальным всплытием эха. Варианты — истина
    плюс две приманки из давнего канона; расклад детерминирован парой
    игрок+день, одна попытка на день.
    """
    parts = callback.data.split(":")
    if len(parts) != 2:
        await callback.answer("Некорректная метка.", show_alert=True)
        return
    try:
        round_id = int(parts[1])
    except ValueError:
        await callback.answer("Некорректная метка.", show_alert=True)
        return
    from sqlalchemy import select as _select

    from app.echoes import build_memory_quiz, surfaced_echoes_for_round
    from app.models import StoryBeat

    async with SessionLocal() as session:
        player = await upsert_player(session, callback.from_user)
        echoes = await surfaced_echoes_for_round(session, round_id)
        if not echoes:
            await callback.answer("Сегодня в главе не пахло старым.", show_alert=True)
            return
        true_titles = [echo.title for echo in echoes]
        source_days = {echo.source_day for echo in echoes}
        beats = (
            await session.execute(
                _select(StoryBeat.title, StoryBeat.day_index).order_by(StoryBeat.day_index.asc())
            )
        ).all()
        decoys = [
            title for title, day in beats if day not in source_days and (day < min(source_days) - 1 or day > max(source_days) + 1)
        ]
        quiz = build_memory_quiz(player.id, round_id, true_titles, decoys)
        if quiz is None:
            await callback.answer("Архив слишком мал, чтобы проверять память. Позже.", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"📖 {option[:60]}", callback_data=f"remember:pick:{round_id}:{index}")]
                for index, option in enumerate(quiz["options"])
            ]
        )
    await callback.message.answer("🧠 Архивариус прищуривается: «Откуда этот след?»", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("remember:pick:"))
async def on_remember_pick(callback: CallbackQuery) -> None:
    """Ответ на квиз памяти: верно — отметка и нюх; мимо — архив молчит."""
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Некорректный выбор.", show_alert=True)
        return
    try:
        round_id, index = int(parts[2]), int(parts[3])
    except ValueError:
        await callback.answer("Некорректный выбор.", show_alert=True)
        return
    from app.models import WatcherState
    from app.tally import register_memory_hit
    from app.echoes import build_memory_quiz, surfaced_echoes_for_round

    async with SessionLocal() as session:
        player = await upsert_player(session, callback.from_user)
        marker = f"memquiz:{player.id}:{round_id}"
        if await session.get(WatcherState, marker) is not None:
            await callback.answer("Сегодня архив уже закрыл твой вопрос.", show_alert=True)
            return
        echoes = await surfaced_echoes_for_round(session, round_id)
        true_titles = [echo.title for echo in echoes]
        # Пересобираем тот же расклад: серверу нечего хранить в кнопке.
        from sqlalchemy import select as _select
        from app.models import StoryBeat

        beats = (
            await session.execute(
                _select(StoryBeat.title, StoryBeat.day_index).order_by(StoryBeat.day_index.asc())
            )
        ).all()
        source_days = {echo.source_day for echo in echoes}
        decoys = [
            title for title, day in beats if day not in source_days and (day < min(source_days) - 1 or day > max(source_days) + 1)
        ]
        quiz = build_memory_quiz(player.id, round_id, true_titles, decoys)
        correct = (
            quiz is not None
            and 0 <= index < len(quiz["options"])
            and quiz["options"][index] in quiz["correct"]
        )
        session.add(WatcherState(key=marker, value="1"))
        if correct:
            await register_memory_hit(session, player.id, round_id)
            await callback.answer("Архивариус молча ставит галочку. ✨ +1 нюх.", show_alert=True)
            if callback.message is not None:
                try:
                    await callback.message.answer(
                        "📚 «Память сети пополнилась», — шепчет Архивариус и не объясняет, чью."
                    )
                except Exception:
                    pass
        else:
            await callback.answer("Архивариус хмурится: «Не тот след». Попробуй завтра.", show_alert=True)
        await session.commit()


@router.callback_query(F.data.startswith("vote:"))
async def on_vote(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Некорректный выбор.", show_alert=True)
        return
    _, raw_round_id, raw_position = parts
    try:
        round_id = int(raw_round_id)
        position = int(raw_position)
    except ValueError:
        await callback.answer("Некорректный выбор.", show_alert=True)
        return
    async with SessionLocal() as session:
        player = await upsert_player(session, callback.from_user)
        round_row = await get_active_round(session)
        if round_row is None or round_row.id != round_id:
            await callback.answer("Этот день уже закрыт.", show_alert=True)
            return
        result = await cast_vote(session, round_row, player.id, position)
        outcome = ""
        if result == "already":
            vote = await get_vote(session, round_row.id, player.id)
            current_position: int | None = vote.card_position if vote else None
            if (
                settings.revote_enabled
                and round_row.status == RoundStatus.OPEN
                and current_position is not None
                and current_position != position
            ):
                # Есть оплаченный грант — списываем и меняем путь прямо здесь.
                outcome = await change_vote(session, round_row, player.id, position)
        else:
            current_position = None
    if result == "already":
        if outcome == "ok":
            await callback.answer(
                f"Грант списан. Путь изменён на {POSITIONS[position]}.", show_alert=True
            )
            return
        if outcome == "no_grant":
            hint = (
                f"Путь уже выбран. Сменить его можно за ⭐{settings.revote_stars} — команда /change."
            )
            if callback.message is None or callback.message.chat.type != ChatType.PRIVATE:
                hint = "Путь уже выбран. Смена — платно, через личку бота: /change."
            await callback.answer(hint[:200], show_alert=True)
            return
    texts = {
        "ok": f"{ok_mark(str(round_id))} Путь {POSITIONS[position]} принят. Счёт скрыт до итогов.",
        "already": f"{hint_mark('already')} Ты уже оставил След сегодня.",
        "closed": f"{warn_mark('closed')} Голосование закрыто, идёт подсчёт.",
        "invalid": f"{warn_mark('invalid')} Такого пути нет на карте Стаи.",
    }
    await callback.answer(texts.get(result, "Неизвестный ответ."), show_alert=True)


_ECONOMY_TEXT = (
    "\n\nРаспределение фонда дня:\n"
    "• 97% — поставившим на верный путь, пропорционально ставкам\n"
    "• 2% — копилка недели: в понедельник её делят три верхние ступени по числу "
    "верных путей, чем ниже ступень — тем больше доля "
    "(нужен кошелёк и 4+ дня голосования за неделю)\n"
    "• 0,5% — копилка месяца: в конце месяца её забирают лидеры /top\n"
    "• 0,5% — на поддержку Стаи\n"
    "\nЕсли на верный путь не поставил никто — все ставки возвращаются целиком."
)

_DYOR_TEXT = (
    "Игра, а не вклад: бот и хранитель не отвечают за утраченные "
    "средства по любой причине. Ты сам решаешь, на что ставить, "
    "и сам отвечаешь за свои ставки. DYOR."
)


async def _today_stake_line(session, player_id: int) -> str | None:
    """Строка про ставку игрока в открытом дне или None, если показывать нечего.

    Используется везде, где игроку полезно видеть свой вклад в фонд дня:
    /wallet, /stake и приватная кнопка в группах. Любой сбой здесь не имеет
    права хоронить весь вид кошелька — возвращаем None и логируем."""
    if not settings.ton_enabled:
        return None
    try:
        round_row = await get_active_round(session)
        if round_row is None:
            return None
        stake = (
            await session.execute(
                select(Stake).where(Stake.round_id == round_row.id, Stake.player_id == player_id)
            )
        ).scalar_one_or_none()
    except Exception:
        logger.warning("Статус ставки для вида кошелька не прочитан", exc_info=True)
        return None
    if stake is None:
        return None
    state = {
        "confirmed": "подтверждена ✅",
        "pending": "ждёт подтверждения сети ⏳",
        "rejected": "не принята (меньше минимума) — вернём после итогов ↩️",
    }.get(stake.status, "ждёт подтверждения сети ⏳")
    return f"Ставка сегодня: {from_nano(stake.amount_nanotons):g} Gram ({state})."


_WALLET_FALLBACK_TEXT = (
    f"{warn_mark('wallet-view')} Раздел кошелька сейчас не собирается — уже разбираюсь.\n"
    "Привязать или сменить адрес можно прямо сейчас: отправь одной строкой\n"
    "<code>/wallet UQ…</code> (или EQ…).\n"
    "Если кошелёк был привязан ранее — он никуда не делся, переводы с него засчитываются."
)


async def _wallet_view_safe(user) -> str:
    """Вид /wallet, который отвечает ВСЕГДА: сбой сборки подменяется
    статичной инструкцией вместо общего «сеть дрогнула»."""
    try:
        return await _wallet_view_text(user)
    except Exception:
        logger.exception("Вид /wallet не собрался — отвечаем статикой")
        return _WALLET_FALLBACK_TEXT


async def _stake_view_safe(user) -> str:
    """Аналогично для /stake: инструкция доходит даже при сбое статусной части."""
    try:
        return await _stake_view_text(user)
    except Exception:
        logger.exception("Вид /stake не собрался — отвечаем статикой")
        return (
            f"{hint_mark('stake')} Ставка в три шага:\n"
            "1. Привяжи кошелёк: /wallet UQ…\n"
            "2. Переведи сумму казначею со своего кошелька (адрес появится здесь позже).\n"
            "3. Нажми карту пути до закрытия голосования."
        )


def _win_calc_text() -> str:
    """Формула выигрыша по текущим процентам + три примера с цифрами."""
    pool_pct = (
        100
        - settings.owner_rake_pct
        - settings.leaderboard_rake_pct
        - settings.weekly_pot_pct
    )
    # Пример 1: банк 10 G, на верный путь 6 G двумя игроками (4 G и 2 G).
    pool1 = 10 * pool_pct / 100
    # Пример 2: банк 5 G, верный путь собрал 0.5 G одного игрока.
    pool2 = 5 * pool_pct / 100
    coef2 = pool2 / 0.5
    return (
        "\n\n🧮 Как считается выигрыш\n"
        f"Фонд дня = все ставки. Пул призов = {pool_pct:g}% фонда "
        "(остаток — копилки недели/месяца и поддержка). Пул делится между "
        "поставившими на верный путь пропорционально их ставкам.\n"
        f"Пример 1: банк 10 G, на верный путь поставлено 6 G двумя игроками "
        f"(4 G и 2 G) → пул {pool1:.2f} G: первый получает {pool1 * 4 / 6:.2f} G, "
        f"второй {pool1 * 2 / 6:.2f} G.\n"
        f"Пример 2: банк 5 G, верный путь собрал всего 0.5 G одного игрока → "
        f"он забирает {pool2:.2f} G (коэффициент ×{coef2:.1f} к своей ставке).\n"
        "Пример 3: на верный путь не поставил никто — все ставки возвращаются целиком."
    )


async def _wallet_view_text(user) -> str:
    async with SessionLocal() as session:
        player = await upsert_player(session, user)
        stake_line = await _today_stake_line(session, player.id)
        if player.wallet_address:
            shown = friendly_address(player.wallet_address, testnet=settings.is_testnet)
            body = (
                f"{money_mark(str(user.id))} Привязанный кошелёк:\n<code>{shown}</code>\n"
                "Переводы считаются твоими, если отправлены именно с этого кошелька.\n"
                "Чтобы перепривязать: пришли одной строкой <code>/wallet</code> и адрес.\n"
                "Как поставить на путь: /stake"
            )
        else:
            body = (
                f"{money_mark('none')} Кошелёк не привязан.\n"
                "Напиши /wallet — бот сам попросит адрес следующим сообщением.\n"
                "Он нужен для ставок на путь и призовых выплат (включая топ недели).\n"
                "Как поставить на путь: /stake"
            )
        if stake_line:
            body += f"\n\n💸 {stake_line}"
        return f"{body}{_ECONOMY_TEXT}\n\n{_DYOR_TEXT}"


# Диалог «пришли адрес следующим сообщением» живёт в БД (wallet_dialogs):
# переживает рестарт и работает при нескольких инстансах за одним вебхуком.


async def _dialog_open(uid: int) -> bool:
    if uid <= 0:
        return False
    async with SessionLocal() as session:
        return await session.get(WalletDialog, uid) is not None


async def _dialog_start(uid: int) -> None:
    if uid <= 0:
        return
    async with SessionLocal() as session:
        if await session.get(WalletDialog, uid) is None:
            session.add(WalletDialog(player_id=uid))
            await session.commit()


async def _dialog_close(uid: int) -> None:
    if uid <= 0:
        return
    async with SessionLocal() as session:
        row = await session.get(WalletDialog, uid)
        if row is not None:
            await session.delete(row)
            await session.commit()


async def _bind_wallet(message: Message, address: str) -> bool:
    """Общая привязка адреса: для «/wallet и адрес одной строкой» и для
    диалогового режима («пришли адрес следующим сообщением»).

    False — адрес не распознан и можно попробовать снова (диалог открыт).
    """
    if not is_valid_ton_address(address):
        await message.answer(
            f"{warn_mark('badaddr')} Это не похоже на адрес Gram-кошелька (бывший TON).\n"
            "Адрес начинается с UQ или EQ — длинная строка вроде "
            "<code>UQD5…</code>. Пришли её целиком одним сообщением.",
            parse_mode=ParseMode.HTML,
        )
        return False
    uid = message.from_user.id if message.from_user else 0
    async with SessionLocal() as session:
        player = await upsert_player(session, message.from_user)
        # Защита от угона выплат: пока ставка в незакрытом дне, кошелёк менять нельзя.
        locked = await session.execute(
            select(Stake.id)
            .join(Round, Round.id == Stake.round_id)
            .where(
                Stake.player_id == player.id,
                Stake.status.in_(["pending", "confirmed"]),
                Round.payouts_finalized.is_(False),
            )
            .limit(1)
        )
        if locked.scalar_one_or_none() is not None:
            await _dialog_close(uid)
            await message.answer(f"{warn_mark('locked')} У тебя ставка в игре — кошелёк закреплён до итогов дня.")
            return True
        # Храним канонический raw-hex: watcher сопоставляет отправителя
        # транзакции именно с ним, а UQ/EQ-формы разных кошельков дают один raw.
        player.wallet_address = normalize_address(address)
        player.wallet_linked_at = datetime.now(timezone.utc)
        await session.commit()
    await _dialog_close(uid)
    confirmation = f"{ok_mark(str(uid))} Кошелёк привязан. Теперь переводы с него будут считаться твоими ставками."
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(confirmation)
    else:
        # Без деталей: сам адрес уже засветился в сообщении группы.
        await message.answer(f"{ok_mark('group')} Кошелёк привязан (детали — в личке у бота).")
    return True


# Рейт-лимит /wallet: команда дёргает БД и диалоги, спамить её незачем.
# Словарь пер-процессный — при горизонтальном масштабировании лимит просто
# ослабнет до «по одному на инстанс», что приемлемо.
_WALLET_LAST: dict[int, float] = {}
_WALLET_COOLDOWN = 30.0


def _wallet_throttled(user_id: int) -> bool:
    if len(_WALLET_LAST) > 10_000:
        _WALLET_LAST.clear()
    now = time.monotonic()
    last = _WALLET_LAST.get(user_id)
    _WALLET_LAST[user_id] = now
    return last is not None and now - last < _WALLET_COOLDOWN


@router.message(Command("wallet"))
async def cmd_wallet(message: Message) -> None:
    """Иммунитет команды: любой сбой внутри — статическая памятка игроку."""
    uid = message.from_user.id if message.from_user else 0
    try:
        await _cmd_wallet_impl(message)
    except Exception:
        logger.exception("/wallet упал целиком (uid=%s) — отправляю памятку", uid)
        try:
            await message.answer(_WALLET_FALLBACK_TEXT, parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception("Даже статический ответ /wallet не ушёл (uid=%s)", uid)


async def _cmd_wallet_impl(message: Message) -> None:
    if message.from_user is not None and message.from_user.id not in settings.admin_id_set:
        if _wallet_throttled(message.from_user.id):
            await message.answer(
                f"{hint_mark('wallet-throttle')} Не так часто — попробуй ещё раз через полминуты."
            )
            return
    parts = (message.text or "").split()
    if len(parts) == 1:
        if message.chat.type == ChatType.PRIVATE:
            async with SessionLocal() as session:
                player = await upsert_player(session, message.from_user)
            if not player.wallet_address:
                logger.info("/wallet uid=%s: кошелёк не привязан — открываю диалог привязки", message.from_user.id)
                await _dialog_start(message.from_user.id)
                await message.answer(
                    f"{hint_mark('wallet-dialog')} Пришли следующим сообщением адрес своего Gram-кошелька (бывший TON) — привяжу автоматически.\n"
                    "Он начинается с UQ или EQ и выглядит примерно так:\n"
                    "<code>UQD5…длинный набор букв и цифр</code>\n\n"
                    "Отменить: напиши <b>отмена</b>.",
                    parse_mode=ParseMode.HTML,
                )
                return
            logger.info(
                "/wallet uid=%s: кошелёк привязан (%s…) — показываю вид",
                message.from_user.id,
                str(player.wallet_address)[-10:],
            )
            await message.answer(await _wallet_view_safe(message.from_user), parse_mode=ParseMode.HTML)
            return
        await message.answer(
            "Информация о кошельке видна только тебе — нажми кнопку.",
            reply_markup=_personal_keyboard("wallet:view", "Мой кошелёк"),
        )
        return
    await _bind_wallet(message, parts[1])


@router.callback_query(F.data == "wallet:view")
async def on_wallet_view(callback: CallbackQuery) -> None:
    if callback.message is not None and callback.message.chat.type == ChatType.PRIVATE:
        await callback.message.answer(await _wallet_view_safe(callback.from_user), parse_mode=ParseMode.HTML)
        await callback.answer()
        return
    text = await _wallet_view_safe(callback.from_user)
    await callback.answer(text[:200], show_alert=True)


_STAKE_HOWTO = (
    "{mark} Ставка на путь — три шага:\n"
    "1. Привяжи кошелёк: /wallet (потом можно перепривязать — старый адрес просто перестанет считаться).\n"
    "2. Переведи от {min:g} Gram (потолка нет) казначею со СВОЕГО привязанного кошелька:\n"
    "<code>{treasury}</code>\n"
    "Кнопка ниже открывает кошелёк с готовым получателем — останется ввести сумму.\n"
    "Комментарий не нужен: watcher найдёт перевод по отправителю примерно за минуту.\n"
    "3. Нажми кнопку с картой пути — когда угодно до закрытия голосования.\n\n"
    "Порядок не важен: голос и перевод можно заносить в любой последовательности, "
    "важно только успеть до дедлайна «Голосование до». Одна ставка на игрока в день. "
    "Если перевод не смог стать ставкой — кошелёк не привязан, ставка уже есть или день "
    "закрылся — деньги вернутся автоматически."
)


async def _stake_view_text(user) -> str:
    if not settings.ton_enabled:
        return "Приём ставок сейчас выключен. Игра бесплатна: просто выбирай путь кнопкой."
    head = _STAKE_HOWTO.format(
        mark=money_mark(str(user.id)),
        min=settings.stake_min_ton,
        treasury=settings.active_treasury_address or "(адрес казначея ещё не настроен)",
    )
    status = ""
    async with SessionLocal() as session:
        player = await upsert_player(session, user)
        if not player.wallet_address:
            status = f"\n\n{hint_mark('stake-nowallet')} Кошелёк пока не привязан — начни с шага 1: /wallet"
        else:
            round_row = await get_active_round(session)
            if round_row is not None:
                stake = (
                    await session.execute(
                        select(Stake).where(Stake.player_id == player.id, Stake.round_id == round_row.id)
                    )
                ).scalar_one_or_none()
                if stake is not None:
                    line = await _today_stake_line(session, player.id)
                    status = (
                        f"\n\n{money_mark(str(round_row.id))} 💸 {line}\n"
                        "Выигрыш придёт, если твой голос совпадёт с победившим путём."
                    )
                elif await get_vote(session, round_row.id, player.id) is not None:
                    status = f"\n\n{hint_mark('vote-first')} Ставки нет, но голос уже оставлен. Перевод засчитается в этот же день, если успеет до закрытия."
    return f"{head}{status}{_ECONOMY_TEXT}{_win_calc_text()}\n\n{_DYOR_TEXT}"


def _stake_pay_keyboard() -> InlineKeyboardMarkup | None:
    """Кнопка «открыть кошелёк» с уже вписанным адресом казначея.

    Универсальная ссылка Tonkeeper: на телефоне открывает приложение с
    готовым получателем, сумму игрок вводит сам. Важно: отправлять нужно
    с привязанного кошелька — watcher ищет перевод по отправителю.
    """
    if not settings.ton_enabled or not settings.active_treasury_address:
        return None
    addr = friendly_address(settings.active_treasury_address, testnet=settings.is_testnet)
    url = f"https://app.tonkeeper.com/transfer/{addr}"
    label = "💸 Открыть кошелёк для ставки"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, url=url)]])


@router.message(Command("stake"))
async def cmd_stake(message: Message) -> None:
    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "Как поставить Gram на путь — нажми кнопку.",
            reply_markup=_personal_keyboard("stake:view", "Как поставить"),
        )
        return
    await message.answer(
        await _stake_view_safe(message.from_user),
        parse_mode=ParseMode.HTML,
        reply_markup=_stake_pay_keyboard(),
    )


@router.callback_query(F.data == "stake:view")
async def on_stake_view(callback: CallbackQuery) -> None:
    if callback.message is not None and callback.message.chat.type == ChatType.PRIVATE:
        await callback.message.answer(
            await _stake_view_safe(callback.from_user),
            parse_mode=ParseMode.HTML,
            reply_markup=_stake_pay_keyboard(),
        )
        await callback.answer()
        return
    # Попап кнопки виден только нажавшему — личные цифры можно показывать
    # прямо в группе, как у /score: сумма ставки и её статус.
    hint = "Ставка: переведи от 0.1 Gram казначею со своего привязанного кошелька (/wallet), потом жми карту. Подробности: /stake в личке."
    try:
        async with SessionLocal() as session:
            player = await upsert_player(session, callback.from_user)
            if not settings.ton_enabled:
                hint = "Приём ставок сейчас выключен. Игра бесплатна: просто выбирай путь кнопкой."
            elif not player.wallet_address:
                hint = (
                    f"Кошелёк не привязан: /wallet в личке. Потом переведи от "
                    f"{settings.stake_min_ton:g} Gram казначею и жми карту пути."
                )
            else:
                line = await _today_stake_line(session, player.id)
                if line is not None:
                    hint = f"💸 {line} Подробности: /stake в личке."
                elif await get_active_round(session) is None:
                    hint = "Сейчас открытого дня нет: ставки принимаются на открытый день."
                else:
                    hint = (
                        f"Ставки сегодня нет. Переведи от {settings.stake_min_ton:g} Gram "
                        "казначею со своего кошелька — адрес: /stake в личке."
                    )
    except Exception:
        logger.exception("Статус ставки для кнопки не собран — отвечаем общей подсказкой")
    await callback.answer(hint[:200], show_alert=True)


def _format_top(
    week_rows: list[tuple[str, int, bool]],
    week_pot_nanotons: float,
    month_rows: list[tuple[str, int]],
    month_pot_nanotons: float,
) -> str:
    from app.style import money_mark

    pcts = "/".join(part.strip() for part in settings.weekly_prize_pcts.split(",") if part.strip())
    lines = [f"{money_mark('week')} Копилка недели: {week_pot_nanotons:g} Gram · места: {pcts}%"]
    if not week_rows:
        lines.append("Верных путей на этой неделе ещё нет — всё впереди.")
    else:
        lines.append("Лидеры недели:")
        for place, (name, count, eligible) in enumerate(week_rows, 1):
            medal = ("🥇", "🥈", "🥉")[place - 1] if place <= 3 else f"{place}."
            ticket = "🎟" if eligible else "🔒"
            lines.append(f"{medal} {ticket} {name} — {count}")
        lines.append("🎟 призовое место · 🔒 не хватает кошелька или дней голосования")
        lines.append("Выплата — в понедельник. Приз: кошелёк + 4 дня голосования за неделю.")
    lines.append("")
    lines.append(f"{money_mark('top')} Копилка месяца: {month_pot_nanotons:g} Gram")
    if not month_rows:
        lines.append("Верных путей в этом месяце ещё нет.")
    else:
        lines.append("Лидеры месяца по верным путям:")
        for place, (name, count) in enumerate(month_rows, 1):
            lines.append(f"{place}. {name} — {count}")
    return "\n".join(lines)


@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    from app.leaderboard import weekly_top
    from app.models import WeeklyPot
    from app.weeks import iso_week_key, week_bounds

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    week_start, week_end = week_bounds(iso_week_key(now))
    async with SessionLocal() as session:
        async def named(rows: list[tuple[int, ...]]) -> dict[int, str]:
            names: dict[int, str] = {}
            if rows:
                pids = [row[0] for row in rows]
                players = (
                    await session.execute(select(Player).where(Player.id.in_(pids)))
                ).scalars().all()
                names = {
                    player.id: player.username or player.first_name or f"игрок {player.id}"
                    for player in players
                }
            return names

        week_raw = await weekly_top(session, week_start, week_end)
        week_names = await named(week_raw)
        wallets: set[int] = set()
        if week_raw:
            wallet_rows = (
                await session.execute(
                    select(Player.id).where(
                        Player.id.in_([pid for pid, _c, _d in week_raw]),
                        Player.wallet_address.is_not(None),
                    )
                )
            ).scalars().all()
            wallets = set(wallet_rows)
        week_rows = [
            (
                week_names.get(pid, f"игрок {pid}"),
                correct,
                pid in wallets and days >= max(1, settings.weekly_min_days),
            )
            for pid, correct, days in week_raw
        ]

        result = await session.execute(
            select(Vote.player_id, func.count())
            .join(Round, Round.id == Vote.round_id)
            .where(
                Vote.card_position == Round.winner_card,
                Round.status == RoundStatus.CLOSED,
                Round.tally_ends_at >= month_start,
            )
            .group_by(Vote.player_id)
            .order_by(func.count().desc(), Vote.player_id.asc())
            .limit(10)
        )
        month_raw = [(pid, count) for pid, count in result.all()]
        month_names = await named(month_raw)
        month_rows = [(month_names.get(pid, f"игрок {pid}"), count) for pid, count in month_raw]

        month = now.strftime("%Y-%m")
        pot_row = (
            await session.execute(select(LeaderboardPot).where(LeaderboardPot.month == month))
        ).scalar_one_or_none()
        week_pot_row = (
            await session.execute(select(WeeklyPot).where(WeeklyPot.week == iso_week_key(now)))
        ).scalar_one_or_none()
    month_pot_ton = from_nano(pot_row.nanotons) if pot_row is not None else 0.0
    week_pot_ton = from_nano(week_pot_row.nanotons) if week_pot_row is not None else 0.0
    await message.answer(_format_top(week_rows, week_pot_ton, month_rows, month_pot_ton))


def _revote_keyboard(round_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⭐ {settings.revote_stars} Stars", callback_data=f"paystars:{round_id}")],
            [InlineKeyboardButton(text=f"💎 {settings.revote_ton:g} Gram", callback_data=f"payton:{round_id}")],
        ]
    )


async def _revote_status(user) -> tuple[str, int | None]:
    """(текст, round_id). round_id не None только если день открыт И путь выбран."""
    async with SessionLocal() as session:
        player = await upsert_player(session, user)
        round_row = await get_active_round(session)
        if round_row is None or round_row.status != RoundStatus.OPEN:
            return f"{warn_mark('revote-closed')} День закрыт — идёт подсчёт. Менять путь поздно.", None
        vote = await get_vote(session, round_row.id, player.id)
        if vote is None:
            return f"{hint_mark('revote-free')} Ты ещё не выбрал путь сегодня — первый выбор бесплатный.", None
        return (
            f"{path_mark('care', str(player.id))} Сегодня твой путь: {POSITIONS[vote.card_position]}. "
            "Оплати смену и нажми другую карту. Грант действует до закрытия дня.",
            round_row.id,
        )


@router.message(Command("change"))
async def cmd_change(message: Message) -> None:
    if not settings.revote_enabled:
        await message.answer(f"{warn_mark('revote-off')} Смена выбора сейчас недоступна.")
        return
    status, round_id = await _revote_status(message.from_user)
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(status)
        if round_id is not None:
            await message.answer("Выбери способ оплаты:", reply_markup=_revote_keyboard(round_id))
        return
    # В группе деталей не даём: только приватная кнопка — как у /score и /wallet.
    await message.answer(
        "Смена пути видна только тебе — нажми кнопку.",
        reply_markup=_personal_keyboard("change:view", "Сменить выбор"),
    )


@router.callback_query(F.data == "change:view")
async def on_change_view(callback: CallbackQuery) -> None:
    status, _round_id = await _revote_status(callback.from_user)
    await callback.answer(f"{status} Открой бота в личке: /change"[:200], show_alert=True)


@router.callback_query(F.data.startswith("paystars:"))
async def on_paystars(callback: CallbackQuery) -> None:
    raw = callback.data.split(":")[1]
    if not raw.isdigit() or callback.message is None or callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Оплата — только в личке у бота (/change).", show_alert=True)
        return
    status, active_id = await _revote_status(callback.from_user)
    round_id = int(raw)
    if active_id != round_id:
        await callback.answer(status[:200], show_alert=True)
        return
    await callback.bot.send_invoice(
        chat_id=callback.message.chat.id,
        title="Смена пути",
        description="Разовое право изменить выбор в сегодняшнем дне. Действует до закрытия дня.",
        payload=build_revote_payload(round_id),
        currency="XTR",
        prices=[LabeledPrice(label="Смена пути", amount=settings.revote_stars)],
    )
    await callback.answer()


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    if parse_revote_payload(query.invoice_payload) is None:
        await query.answer(ok=False, error_message="Счёт устарел. Вызови /change заново.")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message) -> None:
    payment = message.successful_payment
    if payment is None:
        return
    round_id = parse_revote_payload(payment.invoice_payload)
    async with SessionLocal() as session:
        player = await upsert_player(session, message.from_user)
        duplicate = await session.execute(
            select(RevoteGrant.id).where(RevoteGrant.unit_ref == payment.telegram_payment_charge_id)
        )
        if duplicate.scalar_one_or_none() is not None:
            await message.answer("Эта оплата уже учтена.")
            return
        valid = False
        if round_id is not None:
            round_row = await get_round(session, round_id)
            valid = round_row is not None and round_row.status == RoundStatus.OPEN
        session.add(
            RevoteGrant(
                round_id=round_id if valid else None,
                player_id=player.id,
                source="stars",
                unit_ref=payment.telegram_payment_charge_id,
            )
        )
        # Ledger доходов: звёзды оседают на балансе бота — фиксируем сумму.
        session.add(
            Income(
                kind="stars",
                amount_stars=payment.total_amount,
                round_id=round_id if valid else None,
                player_id=player.id,
                unit_ref=payment.telegram_payment_charge_id,
                note="revote",
            )
        )
        await session.commit()
    if valid:
        await message.answer(f"{ok_mark(str(round_id))} Оплачено ⭐ Нажми теперь на другую карту — выбор обновится.")
    else:
        await message.answer(
            f"{warn_mark('late-pay')} Оплата прошла, но день уже закрылся — грант сохранён. "
            "Напиши хранителю игры для возврата."
        )



@router.message(F.refunded_payment)
async def on_refunded_payment(message: Message) -> None:
    """Возврат Stars через Telegram: грант отзывается, ledger помечается.

    Если грант ещё не потрачен — он больше не даст сменить путь; если уже
    потрачен, смена пути остаётся, а хранитель получает алерт о расхождении.
    """
    from datetime import datetime, timezone as _tz

    from app.ops import notify_admins

    payment = message.refunded_payment
    if payment is None:
        return
    charge_id = payment.telegram_payment_charge_id
    spent_at_refund = False
    player_ref = message.from_user.id if message.from_user else 0
    async with SessionLocal() as session:
        grant = (
            await session.execute(select(RevoteGrant).where(RevoteGrant.unit_ref == charge_id))
        ).scalar_one_or_none()
        if grant is not None:
            if grant.status == "granted":
                grant.status = "refunded"
            else:
                spent_at_refund = True
        income = (
            await session.execute(select(Income).where(Income.unit_ref == charge_id))
        ).scalar_one_or_none()
        if income is not None:
            stamp = int(datetime.now(_tz.utc).timestamp())
            tail = f"refunded:{stamp}"
            income.note = (f"{income.note} | {tail}" if income.note else tail)[:200]
        await session.commit()
    try:
        await message.answer(
            "↩️ Возврат звёзд проведён. Грант смены пути отозван."
            if not spent_at_refund
            else "↩️ Возврат проведён, но смена пути уже была использована — напишу хранителю."
        )
    except Exception:
        pass
    bot = getattr(message, "bot", None)
    await notify_admins(
        bot,
        f"↩️ Stars refund: charge `{charge_id}` от игрока {player_ref}"
        + (" — грант был УЖЕ потрачен." if spent_at_refund else "."),
    )

@router.callback_query(F.data.startswith("payton:"))
async def on_payton(callback: CallbackQuery) -> None:
    raw = callback.data.split(":")[1]
    if callback.message is None or callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Оплата — только в личке у бота (/change).", show_alert=True)
        return
    if not settings.ton_enabled or not settings.active_treasury_address:
        await callback.answer("Приём Gram ещё не включён. Используй Stars.", show_alert=True)
        return
    if not raw.isdigit():
        await callback.answer("Некорректный счёт.", show_alert=True)
        return
    address = settings.active_treasury_address
    await callback.message.answer(
        f"{money_mark(raw)} Переведи {settings.revote_ton:g} Gram (или больше) на адрес казначея:\n"
        f"<code>{address}</code>\n\n"
        f"Обязательно с комментарием (memo):\n<code>{revote_memo(int(raw))}</code>\n\n"
        "Кошелёк должен быть привязан: /wallet. Грант придёт в течение минуты. "
        "Неиспользованный до конца дня грант сгорает.",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.my_chat_member()
async def track_chat(event: ChatMemberUpdated) -> None:
    """Запоминаем чаты, где бот состоит (в идеале — администратором)."""
    chat = event.chat
    if chat.type == ChatType.PRIVATE:
        return
    status = event.new_chat_member.status
    active = status in _ACTIVE_STATUSES
    async with SessionLocal() as session:
        row = await session.get(Chat, chat.id)
        if row is None:
            session.add(
                Chat(
                    id=chat.id,
                    title=chat.title or chat.username,
                    type=chat.type,
                    active=active,
                )
            )
        else:
            row.title = chat.title or chat.username or row.title
            row.active = active
        await session.commit()
    logger.info("Чат %s (%s): статус бота %s, active=%s", chat.id, chat.type, status, active)


def _patch_prepared_safe(session, round_row) -> None:
    """Фаза 2 прегенерации для /advance: итог дня вплетается в заготовку.

    Падение не роняет команду — день откроется заготовкой как есть."""
    try:
        patch_prepared_day(session, round_row)
    except Exception:
        logger.exception("Патч заготовки итогом дня %s не удался", round_row.day_index)


@router.message(Command("advance"))
async def cmd_advance(message: Message) -> None:
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    closed_here = False
    claimed = False
    async with SessionLocal() as session:
        round_row = await get_active_round(session)
        if round_row is None:
            round_row = await ensure_current_round(session)
            if await claim_announcement(session, round_row):
                await announce_new_day(message.bot, round_row)
                await message.answer(f"Открыт день {round_row.day_index}.")
            else:
                await message.answer(f"День {round_row.day_index} уже объявлен.")
            return
        if round_row.status.value == "open":
            await close_voting(session, round_row)
            round_row, closed_here = await finish_tally(session, round_row)
            if closed_here:
                await award_points(session, round_row)
                await write_epilogue(session, round_row)
                await _patch_prepared_safe(session, round_row)
            nxt, created = await create_next_round_detailed(session)
        elif round_row.status.value == "tallying":
            round_row, closed_here = await finish_tally(session, round_row)
            if closed_here:
                await award_points(session, round_row)
                await write_epilogue(session, round_row)
                await _patch_prepared_safe(session, round_row)
            nxt, created = await create_next_round_detailed(session)
        else:
            return
        if created:
            claimed = await claim_announcement(session, nxt)
    if not created or not claimed:
        # День уже создан/объявлен планировщиком — второй пост не нужен.
        await message.answer(f"День {nxt.day_index} уже объявлен.")
        return
    delivered = await announce_new_day(message.bot, nxt, round_row if closed_here else None)
    # Заглушки картинок дорисовываются фоном — как и в автопереходе.
    import asyncio as _asyncio

    from app.scheduler import _image_upgrade_job

    _asyncio.create_task(_image_upgrade_job(nxt.day_index))
    if delivered:
        await message.answer(f"День {nxt.day_index} объявлен в {len(delivered)} чат(ах).")
    else:
        # Ни одного подписанного чата — покажем всё прямо здесь.
        await message.answer(await results_message(round_row))
        media = day_media_group(nxt)
        if media:
            await message.answer_media_group(media)
        await message.answer(status_text(nxt), reply_markup=cards_keyboard(nxt.id, remember=await _remember_flag(nxt.day_index)))


@router.message(Command("resetgame"))
async def cmd_resetgame(message: Message) -> None:
    """Сброс игры — только для хранителя. Два режима:
    /resetgame confirm — всё с нуля, включая канон истории;
    /resetgame confirm keepstory — счёты чисты, но мир помнит прошлое."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    words = (message.text or "").lower().split()
    if "confirm" not in words:
        await message.answer(
            f"{warn_mark('reset')} Это сотрёт все дни, голоса, ставки, выплаты и очки игроков.\n"
            "Кошельки, чаты и копилка месяца останутся.\n"
            "<code>/resetgame confirm</code> — полный сброс вместе с каноном истории.\n"
            "<code>/resetgame confirm keepstory</code> — сброс счётов, "
            "но мир и эхо прошлого сохраняются.",
            parse_mode=ParseMode.HTML,
        )
        return
    keep_story = "keepstory" in words
    async with SessionLocal() as session:
        owed = await pending_payout_count(session)
        if owed:
            await message.answer(
                f"{warn_mark('queue')} Сброс отложен: в очереди {owed} неотправленных переводов.\n"
                "Сначала дай выплатам уйти (автоцикл) или разбери зависшие вручную —\n"
                "обнуление стёрло бы чужие деньги."
            )
            return
        new_round = await reset_game(session, keep_story=keep_story)
        first = await claim_announcement(session, new_round)
    if not first:
        await message.answer(f"День {new_round.day_index} только что объявил другой процесс бота.")
        return
    await announce_new_day(message.bot, new_round)
    mode = "Канон истории сохранён." if keep_story else "История стёрта полностью."
    await message.answer(
        f"{ok_mark('reset')} Игра обнулена. День {new_round.day_index} объявлен в чатах. "
        f"{mode} Голосование до {new_round.voting_ends_at:%H:%M} UTC."
    )


@router.message(Command("payouts"))
async def cmd_payouts(message: Message) -> None:
    """Очередь выплат для хранителя: что не ушло и почему."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Payout)
                    .where(Payout.status.notin_(["sent", "dismissed"]))
                    .order_by(Payout.id.asc())
                    .limit(30)
                )
            )
            .scalars()
            .all()
        )
    if not rows:
        await message.answer(f"{ok_mark('queue')} Долгов нет: все выплаты ушли или разобраны.")
        return
    lines = ["Неотправленные выплаты:"]
    for row in rows:
        reason = getattr(row, "last_error", None)
        tail = f" · {reason[:110]}" if reason else ""
        lines.append(
            f"#{row.id} · {row.kind} · {from_nano(row.amount_nanotons):.4f} Gram · "
            f"{row.status} · попыток {row.attempts} · …{row.dest_address[-8:]}{tail}"
        )
    lines.append("")
    lines.append(
        "Спам (пыль с рекламой): /payout <id> spam\n"
        "Настоящий долг, отправить снова: /payout <id> retry"
    )
    await message.answer("\n".join(lines))


@router.message(Command("payout"))
async def cmd_payout(message: Message) -> None:
    """Ручной разбор одной выплаты: /payout <id> spam|retry."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    parts = (message.text or "").lower().split()
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in {"spam", "retry"}:
        await message.answer(
            "Формат: <code>/payout &lt;id&gt; spam</code> — пометить спамом, "
            "<code>/payout &lt;id&gt; retry</code> — вернуть в очередь.",
            parse_mode=ParseMode.HTML,
        )
        return
    payout_id, action = int(parts[1]), parts[2]
    from app.ton_pay import resolve_dead_payout

    async with SessionLocal() as session:
        new_status = await resolve_dead_payout(session, payout_id, action)
    if new_status == "dismissed":
        await message.answer(f"{ok_mark(str(payout_id))} Выплата #{payout_id} помечена как спам: из очереди ушла, сбросу больше не мешает.")
    elif new_status == "pending":
        await message.answer(f"{ok_mark(str(payout_id))} Выплата #{payout_id} вернулась в очередь с нулевым счётом попыток.")
    else:
        await message.answer(f"{warn_mark('nopay')} Выплата #{payout_id} не найдена или уже отправлена.")


@router.message(Command("treasury"))
async def cmd_treasury(message: Message) -> None:
    """Здоровье казначея одним сообщением: адрес, мнемоника, баланс,
    сверка пары мнемоника/адрес, очередь выплат. Только для хранителя."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    from app.ton_pay import treasury_diagnostics

    try:
        await message.answer(await treasury_diagnostics(), parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("Отчёт /treasury не собран")
        await message.answer(f"Отчёт не собрался: {exc}")


# ---------- Пульт хранителя (/panel) ----------

_PANEL_FOOTER = (
    "\n\n🕹 <b>Управление</b> (в личке):\n"
    "/advance — закрыть день досрочно и открыть следующий\n"
    "/today — превью поста игрока · /lore — канон\n"
    "/payouts — очередь выплат (причина у каждой строки)\n"
    "/payout &lt;id&gt; retry|spam — ручной разбор долга\n"
    "/treasury — казначей: баланс и пара ключей\n"
    "/revenue — касса (Stars/Gram) · /health — снимок JSON\n"
    "/resetgame confirm [keepstory] — полный сброс ⚠️\n"
    "Картинки-заглушки дорисовываются сами через 15 мин после анонса."
)


async def _admin_panel_text(session=None) -> str:
    """Сводка состояния игры + подсказки по командам, одним сообщением."""
    from app.ops import snapshot
    from app.season import act_line_short, get_cached_anchor, run_position

    snap = await snapshot()
    lines = ["🎛 <b>ПУЛЬТ ХРАНИТЕЛЯ</b>"]
    rnd = snap.get("round") or {}
    closing = str(rnd.get("voting_ends_at", ""))[11:16]
    lines.append(
        f"День {rnd.get('day_index')} · {rnd.get('status')} · закрытие {closing} UTC"
    )
    if settings.ton_enabled:
        from app.rounds import get_cached_pot

        nano, bets = get_cached_pot(int(rnd.get("day_index", 0)))
        lines.append(f"💰 Банк дня: {nano / 1e9:.2f} Gram · ставок {bets}")
        try:
            anchor = get_cached_anchor()
            run_day, total = run_position(
                anchor,
                datetime.fromisoformat(str(rnd.get("voting_ends_at")))
                if rnd.get("voting_ends_at")
                else datetime.now(timezone.utc),
            )
            lines.append(act_line_short(run_day, total))
        except Exception:
            pass
    queue = snap.get("payout_queue")
    oldest = snap.get("oldest_payout_age")
    dead = snap.get("dead_letter_payouts")
    oldest_note = f", старейшая {int(oldest // 60)} мин" if oldest else ""
    lines.append(f"💸 Выплаты: в очереди {queue}{oldest_note} · failed {dead}")
    if settings.ton_enabled:
        lines.append(
            f"👀 Watcher: {snap.get('watcher_source') or '—'}, "
            f"пульс {int(snap.get('watcher_beat_age') or 0)} с"
        )
    # Топ неотправленного — прямо сюда, чтобы не ходить в /payouts за мелочами.
    try:
        rows = (
            await session.execute(
                select(Payout)
                .where(Payout.status.notin_(["sent", "dismissed"]))
                .order_by(Payout.id.asc())
                .limit(3)
            )
        ).scalars().all() if session is not None else []
        for row in rows:
            reason = f" — {row.last_error[:60]}" if getattr(row, "last_error", None) else ""
            lines.append(
                f"  #{row.id} {row.kind} {from_nano(row.amount_nanotons):.2f} G "
                f"{row.status}{reason}"
            )
    except Exception:
        pass
    tick_age = snap.get("last_tick_age")
    if tick_age is not None and tick_age > 120:
        lines.append(f"⚠️ Тик отстаёт: {int(tick_age)} с — проверь логи.")
    return "\n".join(lines) + _PANEL_FOOTER


def _panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔄 Обновить", callback_data="panel:view")]]
    )


@router.message(Command("panel"))
async def cmd_panel(message: Message) -> None:
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Пульт только для хранителя игры.")
        return
    async with SessionLocal() as session:
        text = await _admin_panel_text(session)
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=_panel_keyboard())


@router.callback_query(F.data == "panel:view")
async def on_panel_view(callback: CallbackQuery) -> None:
    if callback.from_user.id not in settings.admin_id_set:
        await callback.answer("Пульт только для хранителя.", show_alert=True)
        return
    try:
        async with SessionLocal() as session:
            text = await _admin_panel_text(session)
        if callback.message is not None:
            await callback.message.edit_text(
                text, parse_mode=ParseMode.HTML, reply_markup=_panel_keyboard()
            )
        await callback.answer("Обновлено.")
    except Exception as exc:
        await callback.answer(f"Не обновилось: {exc}", show_alert=True)


@router.message(Command("revenue"))
async def cmd_revenue(message: Message) -> None:
    """Касса игры для хранителя: ledger доходов из Income.

    Звёзды сверяются с балансом бота во Fragment, Gram — с историей казны.
    """
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def _block(title: str, data) -> str:
        parts = []
        for kind, count, stars, nanotons in data:
            if kind == "stars":
                parts.append(f"⭐ {stars} ({count} оплат)")
            else:
                parts.append(f"💎 {from_nano(nanotons):.4f} Gram ({count} переводов)")
        return f"{title}: " + ("; ".join(parts) if parts else "пусто")

    async with SessionLocal() as session:
        month_rows = (
            await session.execute(
                select(
                    Income.kind,
                    func.count(),
                    func.coalesce(func.sum(Income.amount_stars), 0),
                    func.coalesce(func.sum(Income.amount_nanotons), 0),
                )
                .where(Income.created_at >= month_start)
                .group_by(Income.kind)
            )
        ).all()
        total_rows = (
            await session.execute(
                select(
                    Income.kind,
                    func.count(),
                    func.coalesce(func.sum(Income.amount_stars), 0),
                    func.coalesce(func.sum(Income.amount_nanotons), 0),
                ).group_by(Income.kind)
            )
        ).all()
    await message.answer(
        f"{money_mark('revenue')} Касса игры\n{_block('Месяц', month_rows)}\n{_block('Всего', total_rows)}"
    )


@router.message(F.chat.type == ChatType.PRIVATE)
async def on_private_fallback(message: Message) -> None:
    """Диалог привязки кошелька: следующее сообщение игрока — это адрес.

    Регистрируется последним, поэтому команды перехватываются своими
    обработчиками раньше. Для всех остальных сообщений молчит.
    """
    uid = message.from_user.id if message.from_user else 0
    if not await _dialog_open(uid):
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer(f"{hint_mark('retry')} Пришли адрес текстом (UQ…/EQ…) или напиши «отмена».")
        return
    if text.lower() in {"отмена", "cancel"}:
        await _dialog_close(uid)
        await message.answer(f"{ok_mark('cancel')} Отменено. Когда будешь готов: /wallet")
        return
    if text.startswith("/"):
        # Любая другая команда закрывает режим ожидания без лишнего шума.
        await _dialog_close(uid)
        return
    await _bind_wallet(message, text)


def build_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    _register_error_handler(dispatcher)
    return dispatcher


def _register_error_handler(dispatcher: Dispatcher) -> None:
    """Глобальный обработчик сбоев: без него aiogram глотает исключение и
    отвечает вебхуку 200 — Telegram не перезашлёт апдейт, и действие игрока
    (голос, оплата) теряется молча. Логируем стек и говорим игроку честное
    «не получилось»: повторный клик обычно проходит. Кнопке снимаем спиннер
    (иначе он висит до клиентского таймаута), а причину сбоя хранитель
    получает в личку (троттлинг раз в час) — диагноз не требует логов."""

    @dispatcher.error()
    async def on_error(event: ErrorEvent) -> bool:
        await handle_update_error(event.bot, event)
        return True


# Троттлинг личных алертов о сбоях апдейтов: раз в час по стеночному времени.
# monotonic() здесь нельзя: на свежезагруженной машине/раннере он меньше часа,
# и «now - 0 >= 3600» ложно — алерты молча переставали уходить.
_LAST_UPDATE_ERROR_ALERT = {"ts": 0.0}
_UPDATE_ERROR_ALERT_COOLDOWN = 3600.0

_PLAYER_ERROR_TEXT = (
    "⚠️ Сеть мира дрогнула — шаг не засчитан. Повтори ещё раз; "
    "если повторится, напиши хранителю."
)


async def handle_update_error(bot: Bot | None, event) -> None:
    """Единая реакция на упавший апдейт: игроку, кнопке и хранителю."""
    import time as _time

    logger.error("Ошибка обработки апдейта", exc_info=event.exception)
    update = event.update
    callback = update.callback_query
    chat_id = None
    if update.message is not None:
        chat_id = update.message.chat.id
    elif callback is not None and getattr(callback, "message", None) is not None:
        chat_id = callback.message.chat.id
    # Кнопка не должна крутиться до клиентского таймаута.
    if callback is not None:
        try:
            await callback.answer("Сеть мира дрогнула — попробуй ещё раз.", show_alert=True)
        except Exception:
            pass
    if chat_id is not None and bot is not None:
        try:
            await bot.send_message(chat_id, _PLAYER_ERROR_TEXT)
        except Exception:
            pass
    now = _time.time()
    if (
        bot is not None
        and settings.admin_id_set
        and now - _LAST_UPDATE_ERROR_ALERT["ts"] >= _UPDATE_ERROR_ALERT_COOLDOWN
    ):
        _LAST_UPDATE_ERROR_ALERT["ts"] = now
        kind = (
            "callback"
            if callback is not None
            else ("message" if update.message is not None else "update")
        )
        summary = f"{type(event.exception).__name__}: {event.exception}"[:350]
        from app.ops import notify_admins

        try:
            await notify_admins(
                bot,
                f"⚠️ Сбой обработки апдейта ({kind}): {summary}\n"
                "Полный стек — в логах сервиса по строке «Ошибка обработки апдейта».",
            )
        except Exception:
            pass


async def create_bot() -> Bot:
    if not settings.bot_token or settings.bot_token.endswith("replace-me"):
        raise RuntimeError("Впиши BOT_TOKEN в .env или переменные окружения @BotFather.")
    return Bot(settings.bot_token)
