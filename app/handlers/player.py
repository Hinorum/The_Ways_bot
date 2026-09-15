# Онбординг и ежедневная игра: старт, карты дня, счёт и голосование.
# Сюжетные команды (/lore, /calling, /best, нюх, квиз памяти) сняты вместе
# со слоем сюжета — ядро: голосование, ставки и выплаты.
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import F
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.broadcast import POSITIONS, cards_keyboard, status_text
from app.config import settings
from app.db import SessionLocal
from app.models import LeaderboardClaim, RoundStatus
from app.rounds import get_active_round, get_latest_round
from app.style import day_mark, hint_mark, ok_mark, path_mark, result_mark, warn_mark
from app.voting import cast_vote, change_vote, get_vote, upsert_player

from .common import _DYOR_TEXT, _ensure_round, _personal_keyboard, router

logger = logging.getLogger(__name__)


def _commands_help() -> list[str]:
    """Справочный блок команд — общий для /start и /help."""
    lines = [
        "<b>Команды каравана</b>",
        "/today — карты дня",
        "/score — твои Следы · /rank — место среди стаи",
        "/invite — позвать в стаю по личной ссылке",
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
    await message.answer(
        status_text(round_row, show_title=True, include_story=True),
        reply_markup=cards_keyboard(round_row.id, remember=False, day_index=round_row.day_index),
    )


async def _score_text(user) -> str:
    async with SessionLocal() as session:
        player = await upsert_player(session, user)
        round_row = await get_active_round(session) or await get_latest_round(session)
        vote = await get_vote(session, round_row.id, player.id) if round_row else None
    if vote is None:
        choice = f"{hint_mark(str(user.id))} Сегодня ты ещё не выбрал тропу."
    elif round_row.status in (RoundStatus.OPEN, RoundStatus.TALLYING):
        choice = f"{path_mark('care', str(user.id))} Твоя тропа сегодня: {POSITIONS[vote.card_position]}."
    else:
        choice = f"Вчера ты шёл тропой {POSITIONS[vote.card_position]}."

    from app.streaks import streak_text

    streak_info = streak_text(player)
    text = (
        f"{choice}\n{result_mark(f'score:{user.id}')} "
        f"Следы: {player.score} · Верных путей: {player.correct_picks}\n\n"
        f"{streak_info}"
    )
    return text


@router.message(Command("score"))
async def cmd_score(message: Message) -> None:
    text = await _score_text(message.from_user)
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(text)
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


@router.callback_query(F.data == "noop")
async def on_noop(callback: CallbackQuery) -> None:
    await callback.answer()


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