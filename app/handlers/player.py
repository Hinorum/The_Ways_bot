# Онбординг и ежедневная игра: старт, забег дня, лор, память, призы лидеров,
# призвание, острый нос, голосование.
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import F
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.broadcast import POSITIONS, build_day_post, cards_keyboard, status_text
from app.config import settings
from app.db import SessionLocal
from app.models import (
    LeaderboardClaim,
    Round,
    RoundStatus,
    StoryBeat,
    Vote,
)
from app.rounds import get_active_round, get_latest_round
from app.style import day_mark, hint_mark, ok_mark, path_mark, result_mark, warn_mark
from app.voting import cast_vote, change_vote, get_vote, upsert_player

from .common import _DYOR_TEXT, _ensure_round, _personal_keyboard, _remember_flag, router

logger = logging.getLogger(__name__)


def _commands_help() -> list[str]:
    """Справочный блок команд — общий для /start и /help."""
    lines = [
        "<b>Команды каравана</b>",
        "/today — карты дня · /lore — Архив Начала и канон прожитых троп",
        "/score — твои Следы и хроника · /calling — призвание",
        "/invite — позвать в стаю по личной ссылке",
        "/best — бестиарий Сети",
        "/help — эта памятка",
    ]
    if settings.revote_enabled:
        lines.append(
            "/change — сменить тропу (⭐ или Gram)"
            if settings.ton_enabled
            else f"/change — сменить тропу (⭐ {settings.revote_stars})"
        )
    if settings.ton_enabled:
        lines.append("/wallet — привязать кошелёк · /stake — как ставить Gram")
        lines.append("/top — копилки и лидеры")
        pool_pct = int(
            100
            - settings.owner_rake_pct
            - settings.leaderboard_rake_pct
            - settings.weekly_pot_pct
            - settings.pack_fund_pct
        )
        lines.append(
            f"\n💰 Фонд дня: {pool_pct}% — поставившим на верный путь; остальное — "
            "Фонд Стаи, копилки недели и месяца (/top) и хранителю. Подробности: /stake."
        )
    return lines


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Памятка команд без стартового вступления."""
    lines = [f"{day_mark(str(message.from_user.id))} <b>{settings.world_name}</b>", ""]
    lines.extend(_commands_help())
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("invite"))
async def cmd_invite(message: Message) -> None:
    """Личная ссылка приглашения ?start=ref_<id>_<токен> и счётчик приведённых."""
    if message.chat.type != ChatType.PRIVATE:
        return
    caller = message.from_user
    if caller is None:
        return
    from app.referrals import invited_count, referral_link, resolve_bot_username

    username = await resolve_bot_username(getattr(message, "bot", None))
    link = referral_link(caller.id, username)
    if not link:
        await message.answer("🧭 Приглашения в стаю пока не открыты — приходи чуть позже.")
        return
    count = await invited_count(caller.id)
    await message.answer(
        f"🐾 Вот твоя ссылка приглашения:\n{link}\n\n"
        "Кто придёт по ней — тот вошёл в стаю твоим следом. "
        f"Приведено всего: {count}. Награды за приглашения — позже."
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    async with SessionLocal() as session:
        player = await upsert_player(session, message.from_user)
        await _record_start_referral(session, message)
        keyboard = await _start_keyboard(session, player)
    uid = str(message.from_user.id) if message.from_user else "0"
    lines = [
        f"{day_mark(uid)} <b>{settings.world_name}</b>",
        "",
        "Потерянные собаки идут сквозь лабиринт нестабильных коридоров.",
        "Ты — один из них. Каждое утро Старый дневник шепчет три тропы",
        "и объявляет Правило дня: большинство, меньшинство или середина.",
        "Он хранит спорные версии каждого дня.",
        "",
        "Один выбор на всех. Победивший путь впечатается в мир.",
        "Завтрашняя глава вырастет из того, что ты выберешь сейчас.",
        "",
        "🐾 Голосование идёт до закрытия дня.",
        "Итоги и новая развилка придут сразу после.",
    ]
    lines.extend(_commands_help())
    if settings.ton_enabled:
        lines.append(f"\n⚠️ {_DYOR_TEXT}")
    await message.answer(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    # Стартовый кадр мира (генерируется один раз на забег): знакомит
    # новичка глазами, а не только словами. Пропал/не сгенерился — молчим.
    from pathlib import Path as _Path

    intro = _Path(settings.media_dir) / "run_intro.jpg"
    if settings.use_free_images and intro.exists():
        try:
            await message.answer_photo(FSInputFile(intro))
        except Exception:
            logger.info("Стартовый кадр мира не доставлен новичку", exc_info=True)
    await cmd_today(message)


async def _record_start_referral(session, message: Message) -> None:
    """Фиксирует пришедшего по чужой ссылке один раз; всё побочное — молча.

    Порядок: upsert_player уже создал строку, так что приглашающий существует
    в таблице. Отказы (самоссылка, подделка, повтор) не должны мешать /start.
    """
    try:
        from app.referrals import parse_referral_arg, record_referral

        caller = message.from_user
        if caller is None or caller.id <= 0:
            return
        get_args = getattr(message, "get_args", None)
        if callable(get_args):
            arg = (get_args() or "").strip()
        else:
            parts = (message.text or "").split(maxsplit=1)
            arg = parts[1].strip() if len(parts) > 1 else ""
        if not arg:
            return
        referrer_id = parse_referral_arg(arg)
        if referrer_id is None:
            return
        await record_referral(session, referrer_id=referrer_id, referred_id=caller.id)
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Реферальный переход не записан")


async def _start_keyboard(session, player) -> InlineKeyboardMarkup:
    """Личное меню /start: кнопка подписки на личку + претензии на места.

    Кнопки Claim видны только тем, кто может на них претендовать: кошелёк
    привязан и в текущем периоде (неделя/месяц) есть хотя бы одна ставка.
    Претензия решает только ничьи — кто раньше нажал, тот выше.
    """
    subscribed = bool(getattr(player, "dm_subscribed", True))
    label = (
        "🔔 Итоги и анонсы в личку: ВКЛ"
        if subscribed
        else "🔕 Итоги и анонсы в личку: ВЫКЛ"
    )
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=label, callback_data="dm:toggle")]
    ]
    if settings.leaderboard_claim_enabled and player.wallet_address:
        from app.leaderboard import _players_with_stake
        from app.weeks import iso_week_key, week_bounds

        now = datetime.now(timezone.utc)
        week_start, week_end = week_bounds(iso_week_key(now))
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (month_start + timedelta(days=35)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        week_staked = await _players_with_stake(session, week_start, week_end, by="opens_at")
        month_staked = await _players_with_stake(session, month_start, next_month, by="tally_ends_at")
        buttons: list[InlineKeyboardButton] = []
        if player.id in week_staked:
            buttons.append(
                InlineKeyboardButton(text="🗓 Заявить приз недели", callback_data="claim:week")
            )
        if player.id in month_staked:
            buttons.append(
                InlineKeyboardButton(text="🗓 Заявить приз месяца", callback_data="claim:month")
            )
        if buttons:
            rows.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "dm:toggle")
async def on_dm_toggle(callback: CallbackQuery) -> None:
    """Тумблер личной рассылки: единственный параметр — флаг dm_subscribed."""
    if callback.from_user is None:
        await callback.answer()
        return
    async with SessionLocal() as session:
        player = await upsert_player(session, callback.from_user)
        subscribed = not bool(getattr(player, "dm_subscribed", True))
        player.dm_subscribed = subscribed
        await session.commit()
        keyboard = await _start_keyboard(session, player)
    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=keyboard)
        except TelegramBadRequest:
            pass
    await callback.answer(
        "Итоги и анонсы снова приходят в личку." if subscribed
        else "Личные рассылки отключены — играем только в группе.",
        show_alert=True,
    )


@router.callback_query(F.data == "claim:week")
async def on_claim_week(callback: CallbackQuery) -> None:
    await _on_claim(callback, "week")


@router.callback_query(F.data == "claim:month")
async def on_claim_month(callback: CallbackQuery) -> None:
    await _on_claim(callback, "month")


_MONTH_NAMES_RU = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _human_claim_period(kind: str, period: str) -> str:
    """Человекочитаемый период заявки вместо служебного «2026-W35» / «2026-08»."""
    if kind == "week":
        from app.weeks import week_bounds

        start, end = week_bounds(period)
        return f"с {start:%d.%m} по {(end - timedelta(days=1)):%d.%m}"
    year, month = map(int, period.split("-"))
    return f"{_MONTH_NAMES_RU[month - 1]} {year}"


async def _on_claim(callback: CallbackQuery, kind: str) -> None:
    """Претензия на место лидерборда: решает ничьи по времени Claim.

    Кошелёк обязателен, иначе приз физически некуда отправить. Идемпотентно:
    unique(player_id, kind, period) — повторный тап не заводит вторую запись.
    """
    if callback.from_user is None:
        await callback.answer()
        return
    from app.leaderboard import active_claim_period

    period = active_claim_period(kind)
    human = _human_claim_period(kind, period)
    confirm = (
        f"Заявка на копилку недели ({human}) принята: при равенстве верных "
        "путей и ставок Gram ты выше тех, кто заявился позже (или не заявился вовсе)."
        if kind == "week"
        else f"Заявка на копилку месяца ({human}) принята: при равенстве верных "
        "путей и ставок Gram ты выше тех, кто заявился позже (или не заявился вовсе)."
    )
    async with SessionLocal() as session:
        player = await upsert_player(session, callback.from_user)
        if not player.wallet_address:
            await callback.answer(
                "Сначала привяжи кошелёк — без него приз не уйдёт.", show_alert=True
            )
            return
        existing = await session.scalar(
            select(LeaderboardClaim.id).where(
                LeaderboardClaim.player_id == player.id,
                LeaderboardClaim.kind == kind,
                LeaderboardClaim.period == period,
            )
        )
        if existing is not None:
            await callback.answer(
                "Место уже заявлено: твоя претензия учтена (раньше — выше).",
                show_alert=True,
            )
            return
        session.add(
            LeaderboardClaim(
                player_id=player.id,
                kind=kind,
                period=period,
                claimed_at=datetime.now(timezone.utc),
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            await callback.answer(
                "Место уже заявлено: твоя претензия учтена (раньше — выше).",
                show_alert=True,
            )
            return
    await callback.answer(confirm, show_alert=True)


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    round_row = await _ensure_round()
    media, story_in_caption = build_day_post(round_row)
    try:
        if len(media) >= 2:
            await message.answer_media_group(media)
        elif media:
            # Один кадр дня: Telegram не принимает группу из одного вложения.
            await message.answer_photo(photo=media[0].media, caption=media[0].caption)
    except Exception as exc:
        # Картинки — украшение, текст дня обязан дойти даже при сбое Telegram
        # или пропавших файлов: игрок должен видеть развилку и кнопки.
        logger.warning("Медиа дня %s не ушли (%s) — доставляем текст", round_row.day_index, exc)
        story_in_caption = False
    await message.answer(
        status_text(
            round_row,
            show_title=not story_in_caption,
            include_story=not story_in_caption,
        ),
        reply_markup=cards_keyboard(round_row.id, remember=await _remember_flag(round_row.day_index), day_index=round_row.day_index),
    )


@router.message(Command("lore"))
async def cmd_lore(message: Message) -> None:
    from app.lore import ARCHIVE_ORIGIN

    async with SessionLocal() as session:
        beats = (await session.execute(select(StoryBeat).order_by(StoryBeat.day_index))).scalars().all()
    if not beats:
        await message.answer(
            f"{hint_mark('lore-empty')} Канон троп ещё пуст — первый След появится "
            f"после итогов дня.\n\n{ARCHIVE_ORIGIN}"
        )
        return
    text, truncated = _canon_text(beats)
    if truncated:
        text = f"{hint_mark('lore-cut')} Ранние дни растворились в шуме коридоров.\n\n" + text
    await message.answer(f"{ARCHIVE_ORIGIN}\n\n<b>Прожитые тропы</b>\n\n{text}")


def _canon_text(beats) -> tuple[str, bool]:
    """Канон в хронологическом порядке, но окно от свежих дней к старым.

    Возвращает (текст, урезано ли начало). Лимит — 3500 символов сообщения.
    """
    chosen: list[str] = []
    total = 0
    # Запас под «Архив Начала» (+📜 вступление), чтобы всё сообщение канона
    # уместилось в лимит Telegram.
    for beat in reversed(beats):
        chunk = f"День {beat.day_index}. {beat.winning_title}\n{beat.winning_text}"
        if total + len(chunk) + 2 > 3050:
            return "\n\n".join(reversed(chosen)), True
        chosen.append(chunk)
        total += len(chunk) + 2
    return "\n\n".join(reversed(chosen)), False


async def _chronicle(session, player_id: int, limit: int = 7) -> list[str]:
    """Личная хроника сезона: последние дни игрока — путь, исход и последствие.

    Голоса и итоги уже лежат в базе; хроника просто собирает их в биографию.
    """
    from app.models import Card, Round

    rows = (
        await session.execute(
            select(
                Round.day_index, Card.title, Card.tag,
                Vote.card_position == Round.winner_card,
                Round.winner_card,
                Round.chapter_title,
            )
            .join(Vote, Vote.round_id == Round.id)
            .join(Card, (Card.round_id == Round.id) & (Card.position == Vote.card_position))
            .where(Vote.player_id == player_id, Round.status == RoundStatus.CLOSED)
            .order_by(Round.day_index.desc())
            .limit(limit)
        )
    ).all()
    lines = []
    for day, title, tag, won, winner_card, chapter in rows:
        tag_emoji = {"risk": "⚔️", "care": "💚", "cunning": "🦊"}.get(tag or "", "·")
        status = "🏆" if won else ("❌" if winner_card is not None else "·")
        lines.append(f"  {tag_emoji} Д{day} · «{title}» {status}")
    return lines


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
        choice = f"{hint_mark(str(user.id))} Сегодня ты ещё не выбрал тропу."
    elif round_row.status in (RoundStatus.OPEN, RoundStatus.TALLYING):
        choice = f"{path_mark('care', str(user.id))} Твоя тропа сегодня: {POSITIONS[vote.card_position]}."
    else:
        choice = f"Вчера ты шёл тропой {POSITIONS[vote.card_position]}."

    from app.streaks import streak_text, path_legacy

    streak_info = streak_text(player)
    # AI-генерация нарратива стрика
    try:
        from app.streaks import generate_streak_narrative_ai, title_for_streak
        _cur_title = title_for_streak(player.current_streak)
        _ai_narrative = await generate_streak_narrative_ai(_cur_title.key, player.current_streak)
        if _ai_narrative:
            streak_info += f"\n{_ai_narrative}"
    except Exception:
        pass
    legacy = await path_legacy(session, limit=3)

    text = (
        f"{choice}\n{result_mark(f'score:{user.id}')} "
        f"Следы: {player.score} · Верных путей: {player.correct_picks}\n"
        f"🧠 Память лабиринта: {memory_hits} · ✨ Второй нюх: {player.inspiration}\n\n"
        f"{streak_info}"
    )
    if calling is not None:
        text += f"\n{calling.emoji} Призвание: {calling.title}"
    else:
        text += "\nПризвание не выбрано — /calling"
    if stats is not None:
        text += f"\n{trail_line(stats)}"
    if legacy:
        text += "\n\n🌫 Тропы, которые могут вернуться:"
        for item in legacy[:3]:
            tag_emoji = {"risk": "⚔️", "care": "💚", "cunning": "🦊"}.get(item["tag"], "❓")
            text += f"\n  {tag_emoji} День {item['day']}: «{item['title']}»"
    if chronicle:
        text += "\n\n📜 Дневник стаи:\n" + "\n".join(chronicle)
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


@router.message(Command("rank"))
async def cmd_rank(message: Message) -> None:
    """Показывает рейтинг игрока среди стаи."""
    from app.streaks import calc_rank, title_for_streak

    async with SessionLocal() as session:
        player = await upsert_player(session, message.from_user)
        rank = await calc_rank(session, player.id)
        title = title_for_streak(player.current_streak)

    text = (
        f"{title.emoji} <b>{title.name}</b>\n\n"
        f"🐺 Ты среди стаи: #{rank['overall_rank']} из {rank['overall_total']}\n"
        f"📅 На этой неделе: #{rank['week_rank']} ({rank['week_votes']} голосов)\n"
        f"🗓 В этом месяце: {rank['month_votes']} голосов\n\n"
        f"🔥 Серия верных путей: {player.current_streak} · Лучшая: {player.best_streak}"
    )

    if message.chat.type == ChatType.PRIVATE:
        await message.answer(text)
    else:
        await message.answer(
            "Рейтинг — только в личке.",
            reply_markup=_personal_keyboard("rank:view", "Мой рейтинг"),
        )


@router.callback_query(F.data == "rank:view")
async def on_rank_view(callback: CallbackQuery) -> None:
    from app.streaks import calc_rank, title_for_streak

    if callback.message is None:
        await callback.answer()
        return
    async with SessionLocal() as session:
        player = await upsert_player(session, callback.from_user)
        rank = await calc_rank(session, player.id)
        title = title_for_streak(player.current_streak)

    text = (
        f"{title.emoji} Рейтинг\n"
        f"📊 #{rank['overall_rank']} из {rank['overall_total']} | "
        f"📅 Неделя: #{rank['week_rank']} ({rank['week_votes']})"
    )
    await callback.answer(text[:200], show_alert=True)


def _calling_list_text(current, available) -> str:
    from app.callings import CALLINGS

    lines = [
        "🎭 Призвание — кем тебя знает тропа: титул в /score, окраска личных "
        "писем и лёгкое касание в главах. На голоса и деньги не влияет."
    ]
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
    "Вечер в «{place}». Ты поднимаешь морду: где-то за стенами кто-то пересчитывает "
    "стаю заново. {calling_title} не считает — он слышит, когда счёт сбивается.",
    "Короткий отдых в «{place}»: миски остывают, а твой нюх греется о чужое воспоминание. "
    "{calling_title} знает: память — тоже провизия.",
    "Ночь у карт в «{place}». Ты перебираешь запахи дня, как страницы дневника. "
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
        place=place or "кружке коридоров",
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


@router.callback_query(F.data.regexp(r"^remember:\d+:\d+$"))
async def on_remember(callback: CallbackQuery) -> None:
    """«Я помню этот след» — старт квиза: откуда всплывший след?

    Кнопка живёт только в дни с реальным всплытием эха. Варианты — истина
    плюс две приманки из давнего канона; расклад детерминирован парой
    игрок+день, одна попытка на день. Данные: round_id (PK раунда) и
    day_index (день для поиска всплывших эхо) — после /resetgame они разнятся.
    """
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Некорректная метка.", show_alert=True)
        return
    try:
        round_id, day_index = int(parts[1]), int(parts[2])
    except ValueError:
        await callback.answer("Некорректная метка.", show_alert=True)
        return
    from sqlalchemy import select as _select

    from app.echoes import build_memory_quiz, surfaced_echoes_for_round
    from app.models import StoryBeat

    async with SessionLocal() as session:
        player = await upsert_player(session, callback.from_user)
        echoes = await surfaced_echoes_for_round(session, day_index)
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
                [InlineKeyboardButton(text=f"📖 {option[:60]}", callback_data=f"remember:pick:{round_id}:{day_index}:{index}")]
                for index, option in enumerate(quiz["options"])
            ]
        )
    await callback.message.answer("📖 Дневник шепчет: «Откуда этот след?»", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("remember:pick:"))
async def on_remember_pick(callback: CallbackQuery) -> None:
    """Ответ на квиз памяти: верно — отметка и нюх; мимо — архив молчит."""
    parts = callback.data.split(":")
    if len(parts) != 5:
        await callback.answer("Некорректный выбор.", show_alert=True)
        return
    try:
        round_id, day_index, index = int(parts[2]), int(parts[3]), int(parts[4])
    except ValueError:
        await callback.answer("Некорректный выбор.", show_alert=True)
        return
    from app.models import WatcherState
    from app.tally import register_memory_hit
    from app.echoes import build_memory_quiz, correct_memory_choice, surfaced_echoes_for_round

    async with SessionLocal() as session:
        player = await upsert_player(session, callback.from_user)
        marker = f"memquiz:{player.id}:{round_id}"
        if await session.get(WatcherState, marker) is not None:
            await callback.answer("Сегодня архив уже закрыл твой вопрос.", show_alert=True)
            return
        echoes = await surfaced_echoes_for_round(session, day_index)
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
        correct = correct_memory_choice(quiz, index)
        session.add(WatcherState(key=marker, value="1"))
        if correct:
            await register_memory_hit(session, player.id, round_id)
            await callback.answer("Дневник молча ставит галочку. ✨ +1 нюх.", show_alert=True)
            if callback.message is not None:
                try:
                    await callback.message.answer(
                        "📚 «Память лабиринта пополнилась», — шепчет дневник и не объясняет, чью."
                    )
                except Exception:
                    pass
        else:
            await callback.answer("Дневник молчит: «Не тот след». Попробуй завтра.", show_alert=True)
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
        "ok": f"{ok_mark(str(round_id))} Тропа {POSITIONS[position]} принята. Итоги скрыты до закрытия дня.",
        "already": f"{hint_mark('already')} Ты уже оставил свой след сегодня.",
        "closed": f"{warn_mark('closed')} День закрыт — итоги скоро.",
        "invalid": f"{warn_mark('invalid')} Этой тропы нет на карте.",
    }
    await callback.answer(texts.get(result, "Неизвестный ответ."), show_alert=True)
