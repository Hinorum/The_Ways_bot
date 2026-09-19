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
from app.ton_utils import from_nano, to_nano
from app.models import LeaderboardClaim, Player, RoundStatus
from app.rounds import get_active_round, get_latest_round
from app.style import (
    day_mark,
    hint_mark,
    ok_mark,
    path_mark,
    result_mark,
    strip_html,
    warn_mark,
)
from app.voting import cast_vote, change_vote, get_vote, upsert_player

from .common import _DYOR_TEXT, _ensure_round, _personal_keyboard, router

logger = logging.getLogger(__name__)


def _commands_help() -> list[str]:
    """Справочный блок команд — общий для /start и /help."""
    lines = [
        "<b>Команды Стаи</b>",
        "/start — как играть: вход в стаю и пульт",
        "/menu — пульт LOST HOWL: всё по кнопкам",
        "/today — карты и тропы дня",
        "/score — твои Следы · /rank — место среди стаи",
    ]
    if settings.revote_enabled:
        lines.append(
            "/change — сменить тропу (⭐ или Gram)"
            if settings.ton_enabled
            else f"/change — сменить тропу (⭐ {settings.revote_stars})"
        )
    if settings.ton_enabled:
        lines.append("/wallet — кошелёк · /stake — как поставить Gram")
        lines.append("/top — копилки и лидеры")
        lines.append("/fund — Фонд Стаи: баланс и журнал")
        pool_pct = int(
            100
            - settings.owner_rake_pct
            - settings.leaderboard_rake_pct
            - settings.weekly_pot_pct
            - settings.pack_fund_pct
            - settings.referral_pct
        )
        lines.append(
            f"\n💰 Фонд дня: {pool_pct}% — поставившим на верную тропу; остальное — "
            "Фонд Стаи, копилки недели и месяца (/top), хранителю и пригласившим "
            f"({settings.referral_pct:.0f}%, см. /referral). Подробности: /stake."
        )
    lines += [
        "/invite — позвать в стаю по личной ссылке",
        "/referral — твоя реферальная награда",
        "/help — эта памятка",
    ]
    return lines


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Памятка команд с пультом вместо слепого меню."""
    lines = [f"{day_mark(str(message.from_user.id))} <b>{settings.world_name}</b>", ""]
    lines.extend(_commands_help())
    label = (
        await _dm_toggle_label(message.from_user.id)
        if message.from_user is not None
        else "🔔 Итоги в личку: ВКЛ"
    )
    await message.answer(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=_menu_keyboard(label),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    """Пульт LOST HOWL: все действия дня по кнопкам."""
    uid = str(message.from_user.id) if message.from_user else "0"
    label = (
        await _dm_toggle_label(message.from_user.id)
        if message.from_user is not None
        else "🔔 Итоги в личку: ВКЛ"
    )
    await message.answer(
        f"{day_mark(uid)} <b>Пульт {settings.world_name}</b>\n\n"
        "Кнопки вместо команд: день, счёт, кошелёк и стая — одним нажатием. "
        "Путь голосования — всегда кнопкой под картой дня.",
        parse_mode=ParseMode.HTML,
        reply_markup=_menu_keyboard(label),
    )


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
        f"Приведено всего: {count}.\n"
        "🏅 С каждой подтверждённой ставки приведённых тебе копится награда — "
        f"смотри баланс: /referral."
    )


@router.message(Command("referral"))
async def cmd_referral(message: Message) -> None:
    """Реферальная награда игрока: ссылка, приведённые и накопленный баланс."""
    if message.chat.type != ChatType.PRIVATE:
        return
    caller = message.from_user
    if caller is None:
        return
    from app.referrals import (
        invited_count,
        referral_link,
        referral_pot_balance,
        resolve_bot_username,
    )

    username = await resolve_bot_username(getattr(message, "bot", None))
    link = referral_link(caller.id, username)
    if not link:
        await message.answer("🧭 Приглашения в стаю пока не открыты — приходи чуть позже.")
        return
    count = await invited_count(caller.id)
    balance = await referral_pot_balance(caller.id)
    threshold_nano = to_nano(settings.referral_min_payout_gram)
    if balance <= 0:
        status = "Пока копилка пуста: награда капает с подтверждённых ставок приведённых."
    elif balance >= threshold_nano:
        status = (
            "🎁 Порог выплаты пройден — придёт на подтверждённый кошелёк "
            "автоматически в ближайшей финализации дня."
        )
    else:
        need = from_nano(threshold_nano - balance)
        status = f"🌸 До выплаты не хватает {need:g} Gram — копилка докапает с новых ставок."
    earned = from_nano(balance)
    await message.answer(
        f"🏅 <b>Твоя реферальная награда</b>\n\n"
        f"Ссылка:\n{link}\n\n"
        f"Приведено: {count}\n"
        f"В копилке: {earned:g} Gram "
        f"(автовыплата от {settings.referral_min_payout_gram:g} Gram)\n\n"
        f"{status}",
        parse_mode=ParseMode.HTML,
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
        "Перед тобой не мир, а видеомагнитофон LOST HOWL, собранный",
        "из хлама. Раз в месяц хранитель вставляет в лоток кассету —",
        "фанфик по «Lost Dogs: The Way»: стая псов ищет дом в разбитом городе.",
        "",
        "Каждый день — одна сцена. Ты голосуешь один из трёх черновиков",
        "кадра; закон дня (большинство, меньшинство или середина) считает",
        "движок честной энтропией. Уцелевший черновик едет в сценарий дальше.",
        "",
        "Проигравшие черновики не исчезают — они остаются шрамами на плёнке",
        "и отзываются в следующих сценах месяца.",
    ]
    if settings.ton_enabled:
        lines.append(
            "🐾 Как поставить Gram на кадр — /stake. Голос без ставки тоже ведёт "
            "тебя: он строит лидерборд недели и месяца."
        )
    else:
        lines.append("🐾 Кадр выбирают кнопкой под картой дня — до конца дня.")
    lines += [
        "",
        "Итог дня и новый кадр придут сразу после закрытия.",
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


def _menu_keyboard(toggle_label: str) -> InlineKeyboardMarkup:
    """Пульт LOST HOWL: кнопки-действия вместо вызова команд слепым меню.

    Сами действия — уже существующие колбэки, где их хватает (счёт, место,
    ставка — с приватным окном в группе), или короткие menu:* сценарии.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="▶️ Сегодня", callback_data="menu:today"),
            InlineKeyboardButton(text="⭐ Счёт", callback_data="score:view"),
            InlineKeyboardButton(text="🐺 Место", callback_data="rank:view"),
        ],
        [
            InlineKeyboardButton(text="💰 Кошелёк", callback_data="menu:wallet"),
            InlineKeyboardButton(text="💸 Ставка", callback_data="stake:view"),
        ],
        [
            InlineKeyboardButton(text="🏆 Копилки", callback_data="menu:top"),
            InlineKeyboardButton(text="🐾 Фонд", callback_data="menu:fund"),
        ],
        [
            InlineKeyboardButton(text=toggle_label, callback_data="dm:toggle"),
            InlineKeyboardButton(text="❓ Помощь", callback_data="menu:help"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _dm_toggle_label(uid: int) -> str:
    """Подпись кнопки личных рассылок по состоянию игрока."""
    async with SessionLocal() as session:
        player = await session.get(Player, uid)
    subscribed = bool(getattr(player, "dm_subscribed", True))
    return (
        "🔔 Итоги в личку: ВКЛ"
        if subscribed
        else "🔕 Итоги в личку: ВЫКЛ"
    )


async def _start_keyboard(session, player) -> InlineKeyboardMarkup:
    """Личное меню /start: пульт + претензии на места лидерборда.

    Кнопки Claim появляются только у игроков, попавших в ничью за призовые
    места закрытого периода, пока окно заявок открыто (приз ещё не роздан).
    Остальным кнопка не видна: заявлять нечего.
    """
    subscribed = bool(getattr(player, "dm_subscribed", True))
    label = (
        "🔔 Итоги в личку: ВКЛ"
        if subscribed
        else "🔕 Итоги в личку: ВЫКЛ"
    )
    markup = _menu_keyboard(label)
    buttons: list[InlineKeyboardButton] = []
    if settings.leaderboard_claim_enabled:
        from app.leaderboard import _claim_window_players

        for kind, text, data in (
            ("week", "🗓 Заявить приз недели", "claim:week"),
            ("month", "🗓 Заявить приз месяца", "claim:month"),
        ):
            tied_players, _period = await _claim_window_players(session, kind)
            if player.id in tied_players:
                buttons.append(InlineKeyboardButton(text=text, callback_data=data))
        if buttons:
            markup.inline_keyboard.append(buttons)
    return markup


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

    Кнопка видна только tied-игрокам, пока окно Claim открыто (приз не
    распределён из-за ничьей). Дублирующий check здесь — защита от hand-craft
    callback: период берётся из окна, а не из текущей даты. Кошелёк обязателен,
    иначе приз физически некуда отправить. Идемпотентно:
    unique(player_id, kind, period) — повторный тап не заводит вторую запись.
    """
    if callback.from_user is None:
        await callback.answer()
        return
    from app.leaderboard import _claim_window_players

    async with SessionLocal() as session:
        tied_players, period = await _claim_window_players(session, kind)
        player = await upsert_player(session, callback.from_user)
        if not period or not tied_players or player.id not in tied_players:
            await callback.answer(
                "Сейчас нет открытых заявок: приз этого периода уже распределён "
                "или ничьей среди лидеров нет.",
                show_alert=True,
            )
            return
        if not player.wallet_address:
            await callback.answer(
                "Сначала привяжи кошелёк — без него приз не уйдёт.", show_alert=True
            )
            return
        human = _human_claim_period(kind, period)
        confirm = (
            f"Заявка на копилку недели ({human}) принята: при равенстве верных "
            "путей и ставок Gram ты выше тех, кто заявился позже (или не заявился вовсе)."
            if kind == "week"
            else f"Заявка на копилку месяца ({human}) принята: при равенстве верных "
            "путей и ставок Gram ты выше тех, кто заявился позже (или не заявился вовсе)."
        )
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
        await status_text(round_row, show_title=True),
        parse_mode=ParseMode.HTML,
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
        await message.answer(text, parse_mode=ParseMode.HTML)
        return
    # В группе личные цифры не показываем: только кнопка с приватным окном.
    await message.answer(
        "Твой счёт увидишь только ты — нажми кнопку.",
        reply_markup=_personal_keyboard("score:view", "Мой счёт"),
    )


@router.callback_query(F.data == "score:view")
async def on_score_view(callback: CallbackQuery) -> None:
    if callback.message is not None and callback.message.chat.type == ChatType.PRIVATE:
        await callback.message.answer(
            await _score_text(callback.from_user), parse_mode=ParseMode.HTML
        )
        await callback.answer()
        return
    # Лимит окна — 200 символов, счёт компактный и помещается. Окно колбэка
    # не рендерит HTML — теги титула убираем, иначе их было бы видно сырыми.
    text = await _score_text(callback.from_user)
    await callback.answer(strip_html(text)[:200], show_alert=True)


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
        await message.answer(text, parse_mode=ParseMode.HTML)
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


@router.callback_query(F.data.startswith("menu:"))
async def on_menu(callback: CallbackQuery) -> None:
    """Пульт LOST HOWL: сценарии кнопок, не покрытые готовыми колбэками."""
    action = callback.data.split(":", 1)[1] if ":" in callback.data else ""
    handler = {
        "today": _menu_today,
        "wallet": _menu_wallet,
        "top": _menu_top,
        "fund": _menu_fund,
        "help": _menu_help,
    }.get(action)
    if handler is None:
        await callback.answer()
        return
    try:
        await handler(callback)
    except Exception:
        logger.exception("Кнопка меню %s упала", action)
        await callback.answer("Что-то щёлкнуло — попробуй ещё раз.", show_alert=True)


async def _menu_today(callback: CallbackQuery) -> None:
    """▶️ Сегодня — повтор дневного поста с кнопками голосования (где угодно)."""
    if callback.message is None:
        await callback.answer()
        return
    round_row = await _ensure_round()
    await callback.message.answer(
        await status_text(round_row, show_title=True),
        parse_mode=ParseMode.HTML,
        reply_markup=cards_keyboard(
            round_row.id, remember=False, day_index=round_row.day_index
        ),
    )
    await callback.answer()


async def _menu_wallet(callback: CallbackQuery) -> None:
    """💰 Кошелёк: в личке открывает диалог привязки, в группе — направляет."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer(
            "Кошелёк — личное: открой профиль бота, нажми Start — там кнопка в пульте.",
            show_alert=True,
        )
        return
    from app.handlers.wallet import _wallet_bind_prompt, _wallet_view_safe
    from .common import _dialog_start

    async with SessionLocal() as session:
        player = await upsert_player(session, callback.from_user)
        if not player.wallet_address:
            await _dialog_start(callback.from_user.id)
            await callback.message.answer(
                _wallet_bind_prompt(), parse_mode=ParseMode.HTML
            )
            await callback.answer()
            return
    await callback.message.answer(
        await _wallet_view_safe(callback.from_user), parse_mode=ParseMode.HTML
    )
    await callback.answer()


async def _menu_top(callback: CallbackQuery) -> None:
    """🏆 Копилки недели и месяца — публичный пост."""
    if callback.message is None:
        await callback.answer()
        return
    from app.handlers.wallet import _top_text

    await callback.message.answer(await _top_text())
    await callback.answer()


async def _menu_fund(callback: CallbackQuery) -> None:
    """🐾 Фонд Стаи — публичный пост с журналом."""
    if callback.message is None:
        await callback.answer()
        return
    from app.handlers.wallet import _fund_text

    await callback.message.answer(await _fund_text(), parse_mode=ParseMode.HTML)
    await callback.answer()


async def _menu_help(callback: CallbackQuery) -> None:
    """❓ Помощь — памятка с пультом."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return
    lines = [f"{day_mark(str(callback.from_user.id))} <b>{settings.world_name}</b>", ""]
    lines.extend(_commands_help())
    label = await _dm_toggle_label(callback.from_user.id)
    await callback.message.answer(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=_menu_keyboard(label),
    )
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
                f"Грант списан. Тропа изменена на {POSITIONS[position]}.", show_alert=True
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