from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.broadcast import (
    POSITIONS,
    announce_new_day,
    build_day_post,
    cards_keyboard,
    results_message,
    status_text,
)
from app.config import settings
from app.db import SessionLocal
from app.models import (
    Chat,
    Income,
    LeaderboardClaim,
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
from app.ton_utils import from_nano, friendly_address, is_valid_ton_address, normalize_address, to_nano
from app.voting import cast_vote, change_vote, get_vote, upsert_player


logger = logging.getLogger(__name__)

router = Router()

_ACTIVE_STATUSES = {"member", "administrator", "creator"}


async def _ensure_round():
    async with SessionLocal() as session:
        return await ensure_current_round(session)


def _commands_help() -> list[str]:
    """Справочный блок команд — общий для /start и /help."""
    lines = [
        "<b>Команды каравана</b>",
        "/today — карты дня · /lore — Архив Начала и канон прожитых троп",
        "/score — твои Следы и хроника · /calling — призвание",
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


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    async with SessionLocal() as session:
        player = await upsert_player(session, message.from_user)
        keyboard = await _start_keyboard(session, player)
    uid = str(message.from_user.id) if message.from_user else "0"
    lines = [
        f"{day_mark(uid)} <b>{settings.world_name}</b>",
        "",
        "Ты — голос стаи потерянных собак, идущей сквозь сеть глючных миров.",
        "Каждое утро Архивариус выносит три тропы и объявляет Закон дня:",
        "большинство, меньшинство или середина — чей зов станет явью.",
        "",
        "Один выбор на всех. Победивший путь впечатается в мир,",
        "и завтрашняя глава вырастет из него.",
        "",
        "🐾 Сутки Стаи: голосование идёт до закрытия дня — итоги и новая",
        "развилка приходят сами, сразу после него.",
    ]
    lines.extend(_commands_help())
    lines.append(
        "\n⚠️ Игра, а не вклад: бот и хранитель не отвечают за утраченные "
        "средства. Ты сам решаешь, на что ставишь."
    )
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
    confirm = (
        f"Заявка на копилку недели {period} принята: при равенстве верных путей "
        "и ставок Gram ты выше тех, кто заявился позже (или не заявился вовсе)."
        if kind == "week"
        else f"Заявка на копилку месяца {period} принята: при равенстве верных "
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
        text = f"{hint_mark('lore-cut')} Ранние дни растворились в шуме порталов.\n\n" + text
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
        f"{choice}\n{result_mark(f'score:{user.id}')} "
        f"Очки: {player.score} · Угаданных законов: {player.correct_picks}\n"
        f"🧠 Память сети: {memory_hits} · ✨ Второй нюх: {player.inspiration}"
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
    await callback.message.answer("🧠 Архивариус прищуривается: «Откуда этот след?»", reply_markup=keyboard)
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
        "closed": f"{warn_mark('closed')} Голосование закрыто — итоги рядом.",
        "invalid": f"{warn_mark('invalid')} Такого пути нет на карте Стаи.",
    }
    await callback.answer(texts.get(result, "Неизвестный ответ."), show_alert=True)


def _economy_text() -> str:
    """Распределение фонда дня — по живым настройкам, без дублей."""
    pcts = "/".join(part.strip() for part in settings.weekly_prize_pcts.split(",") if part.strip())
    m_pcts = "/".join(
        part.strip() for part in settings.monthly_prize_weights.split(",") if part.strip()
    )
    pool_pct = int(
        100
        - settings.owner_rake_pct
        - settings.leaderboard_rake_pct
        - settings.weekly_pot_pct
        - settings.pack_fund_pct
    )

    def pct(value: float) -> str:
        # Русская типографика: дробный процент через запятую (0,5%),
        # целый (1.0 → «1») без хвоста «,0».
        text = str(value)
        if text.endswith(".0"):
            text = text[:-2]
        return text.replace(".", ",")

    return (
        "\n\nРаспределение фонда дня:\n"
        f"• {pool_pct}% — поставившим на верный путь, пропорционально ставкам "
        f"(газ сети ~{settings.payout_fee_gram:g} Gram за перевод вычитается из пула заранее)\n"
        f"• {pct(settings.pack_fund_pct)}% — Фонд Стаи: накопительный, разыгрывается хранителем\n"
        f"• {pct(settings.weekly_pot_pct)}% — копилка недели: в понедельник топ-3 по верным путям "
        f"делит её ({pcts}%: сильнейший — больше); нужны кошелёк, {settings.weekly_min_days}+ дней "
        f"голосования и ставка за неделю; ничья — больший вклад Gram, затем первый Claim\n"
        f"• {pct(settings.leaderboard_rake_pct)}% — копилка месяца: топ-3 лидеров /top делят её "
        f"({m_pcts}%), нужна ставка в месяце; ничья — вклад Gram, затем первый Claim\n"
        f"• {pct(settings.owner_rake_pct)}% — налог «Децентрализованному Богу»\n"
        "\nЕсли на верный путь не поставил никто — все ставки возвращаются целиком."
    )


_DYOR_TEXT = (
    "Игра, а не вклад: бот и хранитель не отвечают за утраченные средства. "
    "Ты сам решаешь, на что ставить, и сам отвечаешь за ставки. DYOR."
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
    f"{warn_mark('wallet-view')} Раздел кошелька временно не отвечает.\n"
    "Привязать или сменить адрес можно прямо сейчас: отправь одной строкой\n"
    "<code>/wallet UQ…</code> (или EQ…).\n"
    "Привязанный раньше кошелёк никуда не делся — переводы с него засчитываются."
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
    """Формула выигрыша по текущим процентам + короткие примеры с цифрами.

    Примеры считаются после вычета газа — как в реальной выплате.
    """
    pool_pct = (
        100
        - settings.owner_rake_pct
        - settings.leaderboard_rake_pct
        - settings.weekly_pot_pct
        - settings.pack_fund_pct
    )
    fee = settings.payout_fee_gram
    # Пример 1: банк 10 G, на верный путь 6 G двумя игроками (4 G и 2 G).
    pool1 = 10 * pool_pct / 100 - fee * 2
    # Пример 2: банк 5 G, верный путь собрал 0.5 G одного игрока.
    pool2 = 5 * pool_pct / 100 - fee
    coef2 = pool2 / 0.5
    return (
        "\n\n🧮 Как считается выигрыш\n"
        f"Пул призов = {pool_pct:g}% фонда дня минус газ сети "
        f"(~{fee:g} G за перевод); он делится между поставившими "
        "на верный путь пропорционально ставкам — каждый получает чистыми.\n"
        f"Пример: банк 10 G, на верный путь поставили двое (4 G и 2 G) → пул "
        f"{pool1:.2f} G делится как {pool1 * 4 / 6:.2f} G и {pool1 * 2 / 6:.2f} G.\n"
        f"Одинокий верный игрок забирает весь пул: банк 5 G при ставке 0.5 G → "
        f"выигрыш {pool2:.2f} G (×{coef2:.1f}).\n"
        f"Доля меньше {settings.min_payout_gram:g} G не станет переводом — она капнет "
        "в копилку недели. На верный путь не поставил никто — все ставки возвращаются целиком."
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
        return f"{body}{_economy_text()}\n\n{_DYOR_TEXT}"


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
        # Сначала проверяем, не привязан ли адрес к другому игроку — уникальность
        # в БД, но необработанный IntegrityError превращался бы в «краш» хендлера.
        already = (
            await session.execute(
                select(Player.id).where(
                    Player.wallet_address == normalize_address(address),
                    Player.id != player.id,
                )
            )
        ).scalar_one_or_none()
        if already is not None:
            await session.rollback()
            await _dialog_close(uid)
            await message.answer(
                f"{warn_mark('dupwallet')} Этот кошелёк уже привязан к другому игроку. "
                "С одного адреса ставки считает только один участник."
            )
            return True
        player.wallet_address = normalize_address(address)
        player.wallet_linked_at = datetime.now(timezone.utc)
        try:
            await session.commit()
        except IntegrityError:
            # Гонка с другой привязкой того же адреса: перехватываем барьер
            # уникальности и даём вежливо-определённый ответ вместо падения.
            await session.rollback()
            await _dialog_close(uid)
            await message.answer(
                f"{warn_mark('dupwallet')} Этот кошелёк уже привязан к другому игроку."
            )
            return True
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
    "2. Переведи от {min:g} Gram (потолка нет) со СВОЕГО привязанного кошелька — "
    "подойдёт любой TON-кошелёк (Tonkeeper, Tonhub, MyTonWallet…):\n"
    "<code>{treasury}</code>\n"
    "Кнопки ниже: открыть кошелёк с готовым получателем или скопировать адрес. "
    "Memo не нужен: перевод найдётся по отправителю.\n"
    "3. Нажми кнопку с картой пути — когда угодно до закрытия голосования.\n\n"
    "Порядок не важен: голос и перевод засчитываются в любой последовательности, "
    "важно успеть до дедлайна «Голосование до». Одна ставка на игрока в день. "
    "Перевод, не ставший ставкой (нет кошелька, ставка уже есть, день закрылся), "
    "вернётся автоматически. Исключение — зона платы за смену пути "
    "({revote:g}…{min:g} Gram): если игрок уже выбрал путь сегодня, перевод "
    "зачтётся как оплата смены и без мемо."
)


async def _stake_view_text(user) -> str:
    if not settings.ton_enabled:
        return "Приём ставок сейчас выключен. Игра бесплатна: просто выбирай путь кнопкой."
    # Версия без ставок (день в снимке режима): приём ставок закрыт для игроков.
    if await _active_round_money_mode() is False:
        return "Игра идёт в версии без ставок: приём ставок выключен. Просто выбирай путь кнопкой."
    if await _active_round_money_mode() is None:
        from app.ops import money_mode_enabled as _pending_mode

        async with SessionLocal() as session:
            if not await _pending_mode(session):
                return "Игра идёт в версии без ставок: приём ставок выключен. Просто выбирай путь кнопкой."
    head = _STAKE_HOWTO.format(
        mark=money_mark(str(user.id)),
        min=settings.stake_min_ton,
        revote=settings.revote_ton,
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
    return f"{head}{status}{_economy_text()}{_win_calc_text()}\n\n{_DYOR_TEXT}"


def _stake_pay_keyboard() -> InlineKeyboardMarkup | None:
    """Кнопки оплаты ставки: несколько кошельков + копирование адреса.

    Универсальная ссылка Tonkeeper осталась, но добавлены Tonhub и кнопка
    «Скопировать адрес» — не у всех Tonkeeper, а адрес нужен любому
    TON-кошельку. Memo не требуется: watcher ищет перевод по отправителю.
    """
    if not settings.ton_enabled or not settings.active_treasury_address:
        return None
    addr = friendly_address(settings.active_treasury_address, testnet=settings.is_testnet)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💸 Tonkeeper", url=f"https://app.tonkeeper.com/transfer/{addr}"
                ),
                InlineKeyboardButton(
                    text="🪙 Tonhub", url=f"https://tonhub.com/transfer/{addr}"
                ),
            ],
            [InlineKeyboardButton(text="📋 Скопировать адрес", callback_data="stake:copy")],
        ]
    )


@router.callback_query(F.data == "stake:copy")
async def on_stake_copy(callback: CallbackQuery) -> None:
    """Адрес казначея отдельным сообщением: тап по <code> копирует его."""
    if not settings.ton_enabled or not settings.active_treasury_address:
        await callback.answer("Приём ставок сейчас выключен.", show_alert=True)
        return
    addr = friendly_address(
        settings.active_treasury_address, testnet=settings.is_testnet
    )
    try:
        await callback.message.answer(
            f"🏛 Адрес Фонда игры для перевода:\n<code>{addr}</code>\n\n"
            "Нажми на адрес — он скопируется. Переводи со СВОЕГО привязанного "
            "кошелька (/wallet), memo не нужен: перевод найдётся по отправителю.",
            parse_mode=ParseMode.HTML,
        )
        await callback.answer("Адрес ниже 👇")
    except Exception as exc:
        logger.warning("Адрес казначея не отправлен: %s", exc)
        await callback.answer("Не получилось — адрес в /stake.", show_alert=True)


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
    hint = f"Ставка: переведи от {settings.stake_min_ton:g} Gram казначею со своего привязанного кошелька (/wallet), потом жми карту. Подробности: /stake в личке."
    try:
        async with SessionLocal() as session:
            player = await upsert_player(session, callback.from_user)
            if not settings.ton_enabled:
                hint = "Приём ставок сейчас выключен. Игра бесплатна: просто выбирай путь кнопкой."
            elif await _active_round_money_mode() is False:
                hint = "Игра идёт в версии без ставок: приём ставок выключен. Просто выбирай путь кнопкой."
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
    month_rows: list[tuple[str, int, bool]],
    month_pot_nanotons: float,
) -> str:
    from app.style import money_mark

    pcts = "/".join(part.strip() for part in settings.weekly_prize_pcts.split(",") if part.strip())
    m_pcts = "/".join(
        part.strip() for part in settings.monthly_prize_weights.split(",") if part.strip()
    )
    lines = [f"{money_mark('week')} Копилка недели: {week_pot_nanotons:g} Gram · места: {pcts}%"]
    if not week_rows:
        lines.append("Верных путей на этой неделе ещё нет — всё впереди.")
    else:
        lines.append("Лидеры недели:")
        for place, (name, count, eligible) in enumerate(week_rows, 1):
            medal = ("🥇", "🥈", "🥉")[place - 1] if place <= 3 else f"{place}."
            ticket = "🎟" if eligible else "🔒"
            lines.append(f"{medal} {ticket} {name} — {count}")
    lines.append(
        f"🎟 призовое место · 🔒 нужен кошелёк, {max(1, settings.weekly_min_days)} дней "
        "голосования и ставка за неделю · топ-3 делят копилку (ничья — больший вклад "
        "Gram, затем первый Claim) · выплата в понедельник"
    )
    lines.append("")
    lines.append(f"{money_mark('top')} Копилка месяца: {month_pot_nanotons:g} Gram · места: {m_pcts}%")
    if not month_rows:
        lines.append("Верных путей в этом месяце ещё нет.")
    else:
        lines.append("Лидеры месяца по верным путям:")
        for place, (name, count, eligible) in enumerate(month_rows, 1):
            ticket = "🎟" if eligible else "🔒"
            lines.append(f"{place}. {ticket} {name} — {count}")
    lines.append(
        "🎟 призовое место · 🔒 нужны кошелёк и ставка в этом месяце · топ-3 делят "
        "копилку (ничья — вклад Gram, затем первый Claim) · выплата 1-го"
    )
    return "\n".join(lines)


@router.message(Command("fund"))
async def cmd_fund(message: Message) -> None:
    """Прозрачность Фонда Стаи: баланс и последние движения журнала."""
    from app.models import PackFund as _Fund
    from app.models import PackFundLedger as _Ledger

    async with SessionLocal() as session:
        fund_nano = (
            await session.execute(select(func.coalesce(func.sum(_Fund.nanotons), 0)))
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    select(_Ledger).order_by(_Ledger.id.desc()).limit(12)
                )
            )
            .scalars()
            .all()
        )
    lines = [f"🐾 Фонд Стаи: <b>{fund_nano / 1e9:.2f} Gram</b>", ""]
    lines.append("Прозрачный журнал (последние движения):")
    if not rows:
        lines.append("  — пока пусто —")
    for row in reversed(rows):
        sign = "+" if row.entry_type == "in" else "−"
        day = row.round_id if row.round_id is not None else "—"
        when = (
            row.created_at.strftime("%d.%m")
            if row.created_at.tzinfo
            else row.created_at.replace(tzinfo=timezone.utc).strftime("%d.%m")
        )
        lines.append(
            f"  {when} {sign}{row.amount_nanotons / 1e9:.4g} Gram · день {day} · {row.note}"
        )
    lines.append("")
    lines.append("1% банка дня копится сюда и не раздаётся сам. Хранитель распоряжается вручную — каждая раздача видна в этом журнале.")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    from app.leaderboard import _players_with_stake, _rank_window
    from app.models import WeeklyPot
    from app.weeks import iso_week_key, week_bounds
    from datetime import timedelta

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

        week_raw = await _rank_window(session, week_start, week_end, by="opens_at", limit=10)
        week_names = await named(week_raw)
        wallets: set[int] = set()
        if week_raw:
            wallet_rows = (
                await session.execute(
                    select(Player.id).where(
                        Player.id.in_([pid for pid, _c, _d, _g in week_raw]),
                        Player.wallet_address.is_not(None),
                    )
                )
            ).scalars().all()
            wallets = set(wallet_rows)
        week_staked = await _players_with_stake(session, week_start, week_end, by="opens_at")
        week_rows = [
            (
                week_names.get(pid, f"игрок {pid}"),
                correct,
                pid in wallets
                and pid in week_staked
                and days >= max(1, settings.weekly_min_days),
            )
            for pid, correct, days, _gram in week_raw
        ]

        next_month = (month_start + timedelta(days=35)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        month_raw = await _rank_window(
            session, month_start, next_month, by="tally_ends_at", limit=10
        )
        month_names = await named(month_raw)
        month_pids = [pid for pid, _c, _d, _g in month_raw]
        month_wallets: set[int] = set()
        if month_pids:
            month_wallet_rows = (
                await session.execute(
                    select(Player.id).where(
                        Player.id.in_(month_pids),
                        Player.wallet_address.is_not(None),
                    )
                )
            ).scalars().all()
            month_wallets = set(month_wallet_rows)
        month_staked = await _players_with_stake(
            session, month_start, next_month, by="tally_ends_at"
        )
        month_rows = [
            (
                month_names.get(pid, f"игрок {pid}"),
                count,
                pid in month_wallets and pid in month_staked,
            )
            for pid, count, _days, _gram in month_raw
        ]

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


def _revote_gram_ceiling() -> float:
    """Верх вилки Gram-оплаты смены пути: строго ниже минимальной ставки.

    Приём revote-переводов идёт по вилке [revote_ton, stake_min_ton) — «до
    X» в тексте обязано быть ровно на один доцентный шаг ниже минимума,
    иначе при смене настроек название врёт проверке конфига.
    """
    return round(settings.stake_min_ton - 0.01, 4)


def _revote_keyboard(round_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"⭐ {settings.revote_stars} Stars", callback_data=f"paystars:{round_id}")]
    ]
    if settings.ton_enabled:
        rows.append(
            [InlineKeyboardButton(text=f"💎 {settings.revote_ton:g} Gram", callback_data=f"payton:{round_id}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _revote_status(user) -> tuple[str, int | None]:
    """(текст, round_id). round_id не None только если день открыт И путь выбран."""
    async with SessionLocal() as session:
        player = await upsert_player(session, user)
        round_row = await get_active_round(session)
        if round_row is None or round_row.status != RoundStatus.OPEN:
            return f"{warn_mark('revote-closed')} День закрыт — путь уже не сменить.", None
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
    # Версия без ставок: смена выбора за валюту/звёзды выключена целиком.
    if await _active_round_money_mode() is False:
        await message.answer(
            f"{warn_mark('revote-off')} Игра идёт в версии без ставок: "
            "смена выбора недоступна — первый выбор и есть твой выбор."
        )
        return
    status, round_id = await _revote_status(message.from_user)
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(status)
        if round_id is not None:
            await message.answer(
                "Выбери способ оплаты:\n\n"
                "⭐ <b>Stars</b> — надёжно и мгновенно (кнопка оплаты в Telegram).\n"
                "💎 <b>Gram</b> — перевод казначею: от "
                f"{settings.revote_ton:g} до {_revote_gram_ceiling():g} Gram (строго меньше минимума ставки "
                f"{settings.stake_min_ton:g} Gram). Мемо не обязателен: сумма из вилки "
                "зачтётся сама как оплата смены. Если приложишь комментарий — "
                "бот найдёт `rv:день` в любой части текста, даже с подписью кошелька.",
                parse_mode=ParseMode.HTML,
                reply_markup=_revote_keyboard(round_id),
            )
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
    # Стоп-кран: во время техработ платные механики не продаются,
    # чтобы игрок не платил за смену пути в замороженной игре.
    if await _game_paused_now():
        await callback.answer(
            "⏸ Идут технические работы — оплата временно недоступна. Попробуй позже.",
            show_alert=True,
        )
        return
    # Версия без ставок: платная смена выбора не продаётся вовсе.
    if await _active_round_money_mode() is False:
        await callback.answer(
            "Игра идёт в версии без ставок: смена выбора недоступна.",
            show_alert=True,
        )
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
    # Второй рубеж стоп-крана: счёт мог быть выставлен до паузы —
    # отклоняем оплату ДО списания Stars.
    if await _game_paused_now():
        await query.answer(
            ok=False,
            error_message="⏸ Идут технические работы — оплата приостановлена. Попробуй позже.",
        )
        return
    # Версия без ставок: даже выставленный до переключения счёт не списываем.
    if await _active_round_money_mode() is False:
        await query.answer(
            ok=False,
            error_message="Игра перешла в версию без ставок — смена выбора отключена.",
        )
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
        f"{money_mark(raw)} Переведи от {settings.revote_ton:g} до {_revote_gram_ceiling():g} Gram "
        f"(строго меньше минимума ставки {settings.stake_min_ton:g} Gram) на адрес казначея:\n"
        f"<code>{address}</code>\n\n"
        f"Комментарий (memo) не обязателен — сумма из вилки зачтётся автоматически.\n"
        f"Можно приложить (бот распознает `rv:день` в любой части текста):\n<code>{revote_memo(int(raw))}</code>\n\n"
        "Эту сумму ставкой быть не может — потолок ниже минимума ставки. "
        "Ровно 0.5 Gram не подойдёт: это уже минимальная ставка, а не оплата смены пути "
        "(бот примет её как ставку дня). Если кошелёк приложит мемо — оплата привяжется "
        "мгновенно и любой суммой из вилки. Без мемо сумма из той же вилки зачтётся "
        "автоматически. Грант придёт в течение минуты.\n\n"
        "💡 Надёжнее и без кошелька — Stars: кнопка оплаты прямо в Telegram.\n"
        "Кошелёк должен быть привязан: /wallet. Неиспользованный до конца дня грант сгорает.",
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


async def _patch_prepared_safe(session, round_row) -> None:
    """Фаза 2 прегенерации для /advance: итог дня вплетается в заготовку.

    Падение не роняет команду — день откроется заготовкой как есть.
    Латентный баг истории: функция была sync и звала async patch_prepared_day
    без await, а вызывали её через await — каждый ручной /advance открытого
    дня падал TypeError'ом уже ПОСЛЕ подсчёта, не открывая следующий день."""
    try:
        await patch_prepared_day(session, round_row)
    except Exception:
        logger.exception("Патч заготовки итогом дня %s не удался", round_row.day_index)


@router.message(Command("advance"))
async def cmd_advance(message: Message) -> None:
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    # Стоп-кран не спорит с хранителем: явный /advance снимает паузу сам
    # (инцидент: отказ «сначала /resume» выглядел как поломка кнопок).
    changed, _delivered = await _set_paused_and_broadcast(message.bot, False)
    if changed:
        await message.answer(f"{ok_mark('go')} Пауза снята автоматически: выполняю /advance.")
    closed_here = False
    claimed = False
    async with SessionLocal() as session:
        # Сначала дочитываем дни, застрявшие позади актуального (инцидент:
        # сбой анонса оставлял день в TALLYING навсегда).
        try:
            from app.rounds import heal_stale_rounds

            await heal_stale_rounds(session)
        except Exception:
            logger.warning("Лечение застрявших дней перед /advance не удалось", exc_info=True)
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
        media, story_in_caption = build_day_post(nxt)
        if len(media) >= 2:
            await message.answer_media_group(media)
        elif media:
            await message.answer_photo(photo=media[0].media, caption=media[0].caption)
        await message.answer(
            status_text(
                nxt,
                show_title=not story_in_caption,
                include_story=not story_in_caption,
            ),
            reply_markup=cards_keyboard(nxt.id, remember=await _remember_flag(nxt.day_index), day_index=nxt.day_index),
        )


@router.message(Command("resetgame"))
async def cmd_resetgame(message: Message) -> None:
    """Сброс игры — только для хранителя. Два режима:
    /resetgame confirm — всё с нуля, включая канон истории;
    /resetgame confirm keepstory — счёты чисты, но мир помнит прошлое."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    # Явный confirm снимает паузу сам: сброс под стоп-краном — осознанное
    # действие хранителя, а не ошибка, которую надо блокировать.
    changed, _delivered = await _set_paused_and_broadcast(message.bot, False)
    if changed:
        await message.answer(f"{ok_mark('go')} Пауза снята автоматически: выполняю сброс.")
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
        # Вторая линия защиты (инцидент: ставки зависшего дня стёрлись сбросом,
        # а монеты остались в казнее): дни без финализации со ставками —
        # неразыгранные обязательства. Сначала дай им доиграть.
        stuck_rows = (
            await session.execute(
                select(Round.day_index)
                .join(Stake, Stake.round_id == Round.id)
                .where(Round.payouts_finalized.is_(False))
                .distinct()
            )
        ).scalars().all()
        if stuck_days := sorted(stuck_rows):
            days_list = ", ".join(str(day) for day in stuck_days[:10])
            await message.answer(
                f"{warn_mark('queue')} Сброс отложен: у дня(ей) {days_list} есть "
                "неразыгранные ставки — деньги игроков ещё в казнее.\n"
                "Дай дням доиграть (/advance или автоцикл): подсчёт сам создаст "
                "возвраты и призы, после отправки сброс пройдёт."
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


async def _payouts_text() -> str:
    """Список неотправленных выплат (для /payouts и кнопки пульта)."""
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
        return f"{ok_mark('queue')} Долгов нет: все выплаты ушли или разобраны."
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
    return "\n".join(lines)


async def _stakes_panel_text() -> str:
    """Ставки для пульта: необработанные (pending) по всем дням + сводка."""
    from app.ops import snapshot

    snap = await snapshot()
    pending_total = snap.get("pending_stakes") or 0
    lines = [
        "🎲 <b>СТАВКИ ХРАНИТЕЛЮ</b>",
        f"⏳ Необработанных переводов-ставок: {pending_total}",
        "",
    ]
    async with SessionLocal() as session:
        days = (
            (await session.execute(select(Round).order_by(Round.day_index.desc()).limit(3)))
            .scalars()
            .all()
        )
        shown = 0
        for day in days:
            rows = (
                await session.execute(
                    select(Stake, Player.username, Player.first_name)
                    .join(Player, Player.id == Stake.player_id)
                    .where(Stake.round_id == day.id)
                    .order_by(Stake.id.asc())
                    .limit(20)
                )
            ).all()
            if not rows:
                continue
            shown += 1
            lines.append(f"День {day.day_index} ({day.status.value}):")
            for stake, username, first_name in rows:
                who = username or first_name or f"игрок {stake.player_id}"
                state = {"confirmed": "✅", "pending": "⏳", "rejected": "↩️"}.get(
                    stake.status, stake.status
                )
                lines.append(
                    f"  {who}: {from_nano(stake.amount_nanotons):g} Gram {state}"
                )
    if not shown:
        lines.append("Ставок за последние дни нет.")
    return "\n".join(lines)


async def _refunds_panel_text() -> str:
    """Ставки для ручного возврата: «не засчитанные» (pending/rejected) с
    кошельком и без уже созданного возврата. Действие — /return <id>."""
    from app.stakes import refundable_stakes

    async with SessionLocal() as session:
        rows = await refundable_stakes(session)
    lines = [
        "↩️ <b>РУЧНОЙ ВОЗВРАТ СТАВОК</b>",
        "Ставки, не получившие «засчитано» и ещё не возвращённые. Подтверждённые "
        "сюда не попадают — они разберутся при итогах дня сами.",
        "",
    ]
    if not rows:
        lines.append("Нет кандидатов для ручного возврата. Долгов нет.")
        return "\n".join(lines)
    for stake, player, round_row in rows:
        who = (
            (player.username or player.first_name or f"игрок {player.id}")
            if player
            else f"игрок {stake.player_id}"
        )
        state = "⏳ не подтверждена" if stake.status == "pending" else "↩️ отклонена"
        round_label = f"день {round_row.day_index}" if round_row else f"раунд {stake.round_id}"
        lines.append(
            f"#{stake.id} {who} · {from_nano(stake.amount_nanotons):g} Gram · "
            f"{state} · {round_label}\n   ↔️ /return {stake.id}"
        )
    lines.append("")
    lines.append("/return &lt;id&gt; — вернуть ставку, деньги уйдут очередью выплат.")
    return "\n".join(lines)


# Инцидент-регрессия: декоратор висел на _payout_text, которая ВОЗВРАЩАЕТ
# строку. Aiogram автоотправляет результат хендлера только если это
# TelegramMethod — голый str выбрасывался, и команда /payouts молчала.
@router.message(Command("payouts"))
async def cmd_payouts(message: Message) -> None:
    """Очередь выплат для хранителя: что не ушло и почему."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    await message.answer(await _payouts_text())


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


@router.message(Command("return"))
async def cmd_return(message: Message) -> None:
    """Ручной возврат «не засчитанной» ставки хранителем: /return <id>."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer(
            "Формат: <code>/return &lt;id&gt;</code> — id ставки из «↩️ Вернуть ставку» в /panel."
        )
        return
    from app.stakes import create_manual_refund

    async with SessionLocal() as session:
        result = await create_manual_refund(session, int(parts[1]))
    if result.startswith("возврат"):
        from app.ton_pay import dispatch_pending_payouts

        try:
            await dispatch_pending_payouts()
        except Exception:
            logger.exception("Кик диспетчера после ручного возврата не удался")
        await message.answer(f"{ok_mark(parts[1])} {result}.")
    else:
        await message.answer(f"{warn_mark('return')} {result}")


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


@router.message(Command("fundout"))
async def cmd_fundout(message: Message) -> None:
    """Записать ручную раздачу Фонда Стаи в журнал (аудит, не двигает деньги).

    Реальный перевод делает обычная очередь выплат/ручной вывод с казначея;
    здесь фиксируется <сумма> и <зачем>, а баланс фонда уменьшается, чтобы
    прозрачный журнал и цифра фонда оставались честными. Только для хранителя.
    """
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Формат: /fundout <Gram> <причина. Что купили/разыграли>")
        return
    try:
        amount_gram = float(args[1].replace(",", "."))
    except ValueError:
        await message.answer("Сумма должна быть числом в Gram.")
        return
    if amount_gram <= 0:
        await message.answer("Сумма должна быть положительной.")
        return
    note = " ".join(args[2:])[:180]

    from app.stakes import record_fund_dispense
    from app.ton_utils import to_nano

    async with SessionLocal() as session:
        result = await record_fund_dispense(session, to_nano(amount_gram), note)
    if result.startswith("сумма") or result.startswith("в фонде"):
        await message.answer(result)
        return
    await message.answer(f"✅ {result}. Реальный перевод — с казначея.")


# ---------- Каркас разрешения споров (/dispute, /disputes) ----------


async def _resolve_player_arg(session, raw: str) -> int | None:
    """Игровой id или @ник → player_id (None, если не нашли)."""
    raw = raw.strip().lstrip("@")
    from app.models import Player as _Player

    if raw.isdigit():
        return int(raw)
    row = (
        await session.execute(select(_Player).where(_Player.username == raw).limit(1))
    ).scalar_one_or_none()
    return row.id if row is not None else None


@router.message(Command("dispute"))
async def cmd_dispute(message: Message) -> None:
    """Жалоба на итог дня. Хранителю доступны open/resolve/reject/compensate."""
    from app import disputes as dispute_mod

    parts = message.text.split()
    verb = parts[1].lower() if len(parts) > 1 else ""
    if verb in ("resolve", "reject", "compensate", "open"):
        if message.from_user is None or message.from_user.id not in settings.admin_id_set:
            await message.answer("Разрешение и компенсация споров — только для хранителя.")
            return
        async with SessionLocal() as session:
            if verb == "open":
                if len(parts) < 4:
                    await message.answer("Формат: /dispute open <день> <id или @ник> <причина>")
                    return
                try:
                    round_id = int(parts[2])
                except ValueError:
                    await message.answer("Номер дня должен быть целым числом.")
                    return
                pid = await _resolve_player_arg(session, parts[3])
                if pid is None:
                    await message.answer("Игрок не найден (id или @ник).")
                    return
                reason = " ".join(parts[4:]) if len(parts) > 4 else ""
                reply = await dispute_mod.open_dispute(session, round_id, pid, reason)
            elif verb == "resolve" or verb == "reject":
                if len(parts) < 2:
                    await message.answer("Формат: /dispute resolve <id> [примечание]")
                    return
                try:
                    did = int(parts[2])
                except ValueError:
                    await message.answer("Номер спора должен быть целым числом.")
                    return
                note = " ".join(parts[3:]) if len(parts) > 3 else ""
                fn = dispute_mod.resolve_dispute if verb == "resolve" else dispute_mod.reject_dispute
                reply = await fn(session, did, note)
            else:  # compensate
                if len(parts) < 4:
                    await message.answer("Формат: /dispute compensate <id> <Gram> [примечание]")
                    return
                try:
                    did = int(parts[2])
                except ValueError:
                    await message.answer("Номер спора должен быть целым числом.")
                    return
                note = " ".join(parts[4:]) if len(parts) > 4 else ""
                reply = await dispute_mod.compensate_dispute(session, did, parts[3], note)
        await message.answer(reply)
        return
    # Публичная само-подача: жалоба игрока на его последний сыгранный день.
    if message.from_user is None:
        return
    from app.models import Vote as _Vote
    from app.models import Round as _Round

    reason = message.text.split(maxsplit=1)[1] if " " in message.text else ""
    async with SessionLocal() as session:
        player = await upsert_player(session, message.from_user)
        latest = (
            await session.execute(
                select(_Round.id)
                .join(_Vote, _Vote.round_id == _Round.id)
                .where(_Vote.player_id == player.id)
                .order_by(_Round.day_index.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        reply = await dispute_mod.open_dispute(session, latest, player.id, reason)
    await message.answer(reply)


@router.message(Command("disputes"))
async def cmd_disputes(message: Message) -> None:
    """Список открытых споров. Только для хранителя."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Только для хранителя игры.")
        return
    from app import disputes as dispute_mod

    async with SessionLocal() as session:
        rows = await dispute_mod.open_disputes(session)
    if not rows:
        await message.answer("Открытых споров нет.")
        return
    lines = ["⚖ Споры в рассмотрении:"]
    for d in rows:
        lines.append(
            f"  #{d.id} · день {d.round_id if d.round_id is not None else '—'} · "
            f"игрок {d.player_id} · {d.reason[:90]}"
        )
    lines.append("")
    lines.append("Разбор: /dispute resolve <id> [заметка] · отказ: /dispute reject <id> · компенсация: /dispute compensate <id> <Gram>")
    await message.answer("\n".join(lines))


# ---------- Сверка казны (/adjust) и стоп-кран игры (/pause, /resume) ----------

async def _game_paused_now() -> bool:
    """Быстрая проверка стоп-крана без привязки к чужой сессии."""
    from app.ops import is_game_paused

    async with SessionLocal() as session:
        return await is_game_paused(session)


async def _active_round_money_mode() -> bool | None:
    """Режим открытого дня: True = со ставками/платной сменой, None = дня нет.

    День снимает режим на своё открытие (Round.money_mode), поэтому даже если
    хранитель переключил рубильник посреди дня — текущий день живёт по своему
    снимку (новый режим вступает со следующего дня).
    """
    async with SessionLocal() as session:
        round_row = await get_active_round(session)
        if round_row is None or round_row.status != RoundStatus.OPEN:
            return None
        return getattr(round_row, "money_mode", True) is not False


async def _set_paused_and_broadcast(bot, paused: bool, reason: str = "") -> tuple[bool, int]:
    """Стоп-кран игры + объявление в чатах. Возвращает (изменилось, чатов)."""
    from app.ops import set_game_paused

    async with SessionLocal() as session:
        changed = await set_game_paused(session, paused, reason)
    if not changed:
        return False, 0
    from app.broadcast import whisper_to_chats

    text = (
        "⏸ Игра приостановлена: идут технические работы. Переводы на адрес фонда "
        "будут возвращены отправителям."
        if paused
        else "▶️ Технические работы завершены — игра возобновляется. Новый день откроется сам в течение минуты."
    )
    try:
        delivered = await whisper_to_chats(bot, text)
    except Exception:
        logger.warning("Объявление о паузе/возобновлении не разослано", exc_info=True)
        delivered = 0
    return True, delivered


async def _adjust_menu_text() -> str:
    """Меню сверки: что видно по расхождению и какие есть кнопки."""
    from app.ops import is_game_paused, paused_reason, treasury_expected_state

    lines = ["⚖️ <b>Сверка казны с БД</b>"]
    async with SessionLocal() as session:
        state = await treasury_expected_state(session)
        paused = await is_game_paused(session)
        reason = await paused_reason(session)
    if state is None:
        lines.append("Баланс казначея недоступен (оба индексатора молчат) — повтори позже.")
        return "\n".join(lines)
    drift = state.drift_nanotons
    lines.append(
        f"Баланс цепочки: {state.balance_nanotons / 1e9:.4f} Gram\n"
        f"Ожидания БД: ~{state.expected_nanotons / 1e9:.4f} Gram "
        f"(допуск ±{state.tolerance_nanotons / 1e9:.4f})"
    )
    if not state.beyond_tolerance:
        lines.append(f"\n{ok_mark('clean')} Всё сходится в допуске — корректировка не нужна.")
    elif drift > 0:
        lines.append(
            f"\n⚠️ На цепи меньше ожиданий на <b>{drift / 1e9:.4f} Gram</b>. Что это было?\n"
            "✋ <b>Ручной вывод</b> — ты сам выводил/переводил эти деньги: сумма уйдёт "
            "в леджер, алерт сверки замолчит.\n"
            "🕳 <b>Пропажа средств</b> — то же самое плюс стоп-кран: игра встанет на паузу, "
            "а все входящие переводы будут автоматически возвращаться отправителям "
            "с комментарием о техработах."
        )
    else:
        lines.append(
            f"\n⚠️ На цепи больше ожиданий на <b>{-drift / 1e9:.4f} Gram</b> — "
            "похоже, было пополнение мимо учёта. Кнопка «Пополнение» запомнит его."
        )
    if paused:
        lines.append(
            f"\n⏸ Игра сейчас на паузе ({reason or 'техработы'}). Снять: /resume или кнопка пульта."
        )
    lines.append("\nТочная сумма: <code>/adjust &lt;сумма&gt; out|in|loss [комментарий]</code>")
    return "\n".join(lines)


def _adjust_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✋ Это был ручной вывод", callback_data="adj:out")],
            [InlineKeyboardButton(text="🕳 Пропажа средств (пауза игры)", callback_data="adj:loss")],
            [InlineKeyboardButton(text="💰 Ручное пополнение казны", callback_data="adj:in")],
        ]
    )


def _adjust_confirm_text(action: str) -> str:
    return {
        "out": (
            "Записать текущее расхождение как ручной вывод? Ожидания БД скорректируются, "
            "алерт замолчит. Нажми кнопку ещё раз для подтверждения."
        ),
        "in": (
            "Записать излишек как ручное пополнение казны? "
            "Нажми кнопку ещё раз для подтверждения."
        ),
        "loss": (
            "⚠️ Пропажа средств: расхождение уйдёт в леджер, а игра ВСТАНЕТ НА ПАУЗУ — "
            "входящие переводы будут возвращаться с пометкой о техработах. "
            "Нажми кнопку ещё раз для подтверждения."
        ),
    }.get(action, "")


# Подтверждение опасных кнопок сверки: admin_id+действие → момент первого тапа.
_ADJ_PENDING: dict[tuple[int, str], float] = {}
_ADJ_CONFIRM_WINDOW = 120.0


async def _apply_adjustment(bot, direction: str, amount_nanotons: int | None, note: str = "") -> str:
    """Записывает корректировку казны; «пропажа» дополнительно гасит игру."""
    from app.ops import (
        MANUAL_IN_KIND,
        MANUAL_OUT_KIND,
        record_manual_adjustment,
        treasury_expected_state,
    )

    kind_by_direction = {
        "out": MANUAL_OUT_KIND,
        "loss": MANUAL_OUT_KIND,
        "in": MANUAL_IN_KIND,
    }
    if direction not in kind_by_direction:
        return "Неизвестное действие сверки."
    async with SessionLocal() as session:
        if amount_nanotons is None:
            state = await treasury_expected_state(session)
            if state is None:
                return "Баланс казначея недоступен (индексаторы молчат) — попробуй позже."
            drift = state.drift_nanotons
            if abs(drift) <= state.tolerance_nanotons:
                return (
                    f"{ok_mark('clean')} Расхождений нет (в допуске "
                    f"±{state.tolerance_nanotons / 1e9:.4f} Gram) — корректировка не нужна."
                )
            amount_nanotons = abs(int(drift))
        row = await record_manual_adjustment(
            session, kind_by_direction[direction], int(amount_nanotons), note
        )
    labels = {
        "out": "Ручной вывод",
        "in": "Ручное пополнение",
        "loss": "Пропажа средств",
    }
    result = (
        f"{money_mark('adj')} <b>{labels[direction]}</b>: "
        f"{from_nano(row.amount_nanotons):.4f} Gram записано в леджер казны. "
        "Ожидания БД скорректированы — алерт сверки замолчит."
    )
    if direction == "loss":
        changed, delivered = await _set_paused_and_broadcast(bot, True, note or "пропажа средств")
        if changed:
            chats = f" Анонс ушёл в {delivered} чат(ов)." if delivered else ""
            result += (
                f"\n⏸ Игра остановлена.{chats} Входящие переводы теперь возвращаются "
                "отправителям с пометкой о техработах. Снять паузу: /resume"
            )
        else:
            result += "\n⏸ Игра уже стояла на паузе."
    return result


@router.message(Command("adjust"))
async def cmd_adjust(message: Message) -> None:
    """Сверка казны: меню разбора расхождения баланса с ожиданиями БД.

    Кнопки закрывают расхождение целиком; точная сумма — аргументами:
    /adjust 1.3569 out «вывод на биржу».
    """
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    parts = (message.text or "").split()
    if len(parts) >= 3 and parts[2].lower() in {"out", "in", "loss"}:
        try:
            amount = float(parts[1].replace(",", "."))
        except ValueError:
            await message.answer(
                "Сумма не разобралась. Формат: <code>/adjust &lt;сумма&gt; out|in|loss [комментарий]</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        if amount <= 0:
            await message.answer("Сумма должна быть положительной.")
            return
        note = " ".join(parts[3:]) if len(parts) > 3 else ""
        result = await _apply_adjustment(message.bot, parts[2].lower(), to_nano(amount), note)
        await message.answer(result, parse_mode=ParseMode.HTML)
        return
    await message.answer(
        await _adjust_menu_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=_adjust_keyboard(),
    )


@router.callback_query(F.data.startswith("adj:"))
async def on_adjust_action(callback: CallbackQuery) -> None:
    """Кнопки сверки казны: первый тап предупреждает, второй — делает."""
    if callback.from_user.id not in settings.admin_id_set:
        await callback.answer("Сверка только для хранителя.", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    if action not in {"out", "in", "loss"}:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return
    key = (callback.from_user.id, action)
    now = time.monotonic()
    pending_at = _ADJ_PENDING.get(key)
    if pending_at is None or now - pending_at > _ADJ_CONFIRM_WINDOW:
        _ADJ_PENDING[key] = now
        await callback.answer(_adjust_confirm_text(action), show_alert=True)
        return
    _ADJ_PENDING.pop(key, None)
    try:
        result = await _apply_adjustment(callback.bot, action, None)
    except Exception as exc:
        logger.exception("Корректировка казны %s не удалась", action)
        await callback.answer(f"Не получилось: {exc}", show_alert=True)
        return
    if callback.message is not None:
        await callback.message.answer(result, parse_mode=ParseMode.HTML, reply_markup=None)
    await callback.answer("Записано.")


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    """Стоп-кран: дни замирают, входящие переводы автоматически возвращаются."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    words = (message.text or "").split(maxsplit=1)
    reason = words[1].strip()[:200] if len(words) > 1 else "идут технические работы"
    changed, delivered = await _set_paused_and_broadcast(message.bot, True, reason)
    if not changed:
        await message.answer(f"{warn_mark('pause')} Игра уже на паузе. Снять: /resume")
        return
    chats = f" Анонс ушёл в {delivered} чат(ов)." if delivered else ""
    await message.answer(
        f"{ok_mark('pause')} Игра остановлена: {reason}.{chats}\n"
        "Дни не открываются, watcher каждый входящий перевод возвращает отправителю "
        "(в комментарии — «техработы»). Очередь выплат продолжает разгребаться.\n"
        "Снять паузу: /resume"
    )


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    """Снимает стоп-кран: следующий тик откроет новый день сам."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    changed, delivered = await _set_paused_and_broadcast(message.bot, False)
    if not changed:
        await message.answer("Игра и так идёт.")
        return
    chats = f" Анонс ушёл в {delivered} чат(ов)." if delivered else ""
    await message.answer(
        f"{ok_mark('go')} Пауза снята.{chats} Новый день откроется в ближайший тик (до минуты)."
    )


# ---------- Пульт хранителя (/panel) ----------

_PANEL_FOOTER = (
    "\n\n🕹 <b>Управление</b> (в личке):\n"
    "/advance — закрыть день досрочно и открыть следующий\n"
    "/today — превью поста игрока · /lore — канон\n"
    "/incoming — журнал входящих переводов казначея\n"
    "/stakes — ставки дня · /payouts — очередь выплат (причина у каждой строки)\n"
    "/payout &lt;id&gt; retry|spam — ручной разбор долга\n"
    "/treasury — казначей: баланс и пара ключей\n"
    "/adjust — сверка казны: ручной вывод или пропажа средств ⚖️\n"
    "/pause … /resume — стоп-кран игры (техработы) ⏸\n"
    "/revenue — касса (Stars/Gram)\n"
    "/resetgame confirm [keepstory] — полный сброс ⚠️\n"
    "Картинки-заглушки дорисовываются сами через 15 мин после анонса."
)


async def _admin_panel_text(session=None) -> str:
    """Сводка состояния игры + подсказки по командам, одним сообщением."""
    from app.ops import snapshot
    from app.ops import is_game_paused as _paused_flag
    from app.ops import paused_reason as _pause_reason
    from app.season import act_line_short, get_cached_anchor, run_position

    snap = await snapshot()
    lines = ["🎛 <b>ПУЛЬТ ХРАНИТЕЛЯ</b>"]
    try:
        async with SessionLocal() as pause_session:
            if await _paused_flag(pause_session):
                lines.append(
                    f"⏸ ИГРА НА ПАУЗЕ ({await _pause_reason(pause_session) or 'техработы'}) "
                    "— снять: /resume. Входящие переводы возвращаются автоматически."
                )
    except Exception:
        pass
    try:
        from app.ops import money_mode_enabled

        async with SessionLocal() as _mm_session:
            money_on = await money_mode_enabled(_mm_session)
        lines.append(
            "💰 Версия: <b>со ставками</b> и платной сменой выбора."
            if money_on
            else "🔰 Версия: <b>без ставок</b> (игра бесплатна, смена выбора закрыта)."
        )
    except Exception:
        pass
    rnd = snap.get("round") or {}
    closing = str(rnd.get("voting_ends_at", ""))[11:16]
    lines.append(
        f"День {rnd.get('day_index')} · {rnd.get('status')} · закрытие {closing} UTC"
    )
    if settings.ton_enabled:
        from app.rounds import get_cached_pot

        nano, bets = get_cached_pot(int(rnd.get("day_index", 0)))
        lines.append(f"💰 Банк дня: {nano / 1e9:.2f} Gram · ставок {bets}")
        # Фонд Стаи: накопление хранителя, раздача вручную.
        try:
            from app.models import PackFund as _Fund

            fund_nano = (
                await session.execute(
                    select(func.coalesce(func.sum(_Fund.nanotons), 0))
                )
            ).scalar_one()
            lines.append(f"🐾 Фонд Стаи: {fund_nano / 1e9:.2f} Gram")
        except Exception:
            pass
        # Метрики суток: явка вчера, всплывшие эха, оставшиеся заглушки.
        try:
            from app.models import LoreEcho as _LE
            from app.models import Vote as _Vote
            from app.models import WatcherState as _WS

            last_closed = (
                await session.execute(
                    select(Round.id)
                    .where(Round.status == RoundStatus.CLOSED)
                    .order_by(Round.day_index.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            votes_yesterday = 0
            if last_closed is not None:
                votes_yesterday = (
                    await session.execute(
                        select(func.count())
                        .select_from(_Vote)
                        .where(_Vote.round_id == last_closed)
                    )
                ).scalar_one()
            surfaced_recent = (
                await session.execute(
                    select(func.count())
                    .select_from(_LE)
                    .where(
                        _LE.status == "surfaced",
                        _LE.surfaced_day >= (rnd.get("day_index") or 1) - 3,
                    )
                )
            ).scalar_one()
            stubs_left = (
                await session.execute(
                    select(func.count()).select_from(_WS).where(
                        _WS.key.like("img_stubs:%")
                    )
                )
            ).scalar_one()
            lines.append(f"📈 Вчера голосов: {votes_yesterday} · эхов за 3 дня: {surfaced_recent}")
            if stubs_left:
                lines.append(f"🖼 Заглушек картинок в шлифовке: {stubs_left}")
        except Exception:
            logger.warning("Метрики суток для пульта не собраны", exc_info=True)
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
    # Разбивка по типу: сколько игроков ждут приз, сколько — возврат ставки.
    pending_by_kind = snap.get("payout_pending_by_kind") or {}
    dead_by_kind = snap.get("payout_dead_by_kind") or {}
    p_refund = int(pending_by_kind.get("refund", 0))
    p_prize = int(pending_by_kind.get("prize", 0))
    p_other = max(0, int(queue) - p_refund - p_prize)
    d_refund = int(dead_by_kind.get("refund", 0))
    parts = []
    if p_refund:
        parts.append(f"ждёт возвратов {p_refund}")
    if p_prize:
        parts.append(f"ждёт призов {p_prize}")
    if p_other:
        parts.append(f"долей {p_other}")
    if dead and d_refund:
        parts.append(f"failed-возвратов {d_refund}")
    if parts:
        lines.append(f"  · {', '.join(parts)}")
    pending_stakes = snap.get("pending_stakes") or 0
    if settings.ton_enabled:
        stakes_note = f"⏳ Переводов не обработано: {pending_stakes}"
        if not pending_stakes:
            stakes_note += " · всё обработано"
        lines.append(stakes_note)
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


async def _panel_keyboard() -> InlineKeyboardMarkup:
    """Кнопочный пульт хранителя: обновление и безопасные действия.

    Кнопка стоп-крана живёт здесь же: подпись зависит от текущего
    состояния (пауза/работа), поэтому клавиатура пересобирается на каждый показ.
    """
    from app.ops import is_game_paused

    async with SessionLocal() as session:
        paused = await is_game_paused(session)
    pause_button = (
        InlineKeyboardButton(text="▶️ Возобновить игру", callback_data="panel:resume")
        if paused
        else InlineKeyboardButton(text="⏸ Пауза игры", callback_data="panel:pause")
    )
    from app.ops import money_mode_enabled

    async with SessionLocal() as session:
        money_on = await money_mode_enabled(session)
    version_button = InlineKeyboardButton(
        text="🔰 Версия без ставок" if money_on else "💰 Версия со ставками",
        callback_data="panel:now" if money_on else "panel:money",
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="panel:view"),
                InlineKeyboardButton(text="💸 Выплаты", callback_data="panel:payouts"),
                InlineKeyboardButton(text="🎲 Ставки", callback_data="panel:stakes"),
            ],
            [
                InlineKeyboardButton(text="↩️ Вернуть ставку", callback_data="panel:refunds"),
                InlineKeyboardButton(text="🏛 Казначей", callback_data="panel:treasury"),
                InlineKeyboardButton(text="💰 Касса", callback_data="panel:revenue"),
            ],
            [
                InlineKeyboardButton(text="⚖️ Сверка казны", callback_data="panel:adjust"),
                pause_button,
            ],
            [
                version_button,
                InlineKeyboardButton(text="⏩ Завершить день", callback_data="panel:advance"),
            ],
        ]
    )


@router.message(Command("panel"))
async def cmd_panel(message: Message) -> None:
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Пульт только для хранителя игры.")
        return
    async with SessionLocal() as session:
        text = await _admin_panel_text(session)
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=await _panel_keyboard())


@router.callback_query(F.data.startswith("panel:"))
async def on_panel_action(callback: CallbackQuery) -> None:
    """Единая точка кнопок пульта: гейт хранителя + маршрутизация действий."""
    if callback.from_user.id not in settings.admin_id_set:
        await callback.answer("Пульт только для хранителя.", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    try:
        if action in {"view", "refresh"}:
            async with SessionLocal() as session:
                text = await _admin_panel_text(session)
            if callback.message is not None:
                try:
                    await callback.message.edit_text(
                        text, parse_mode=ParseMode.HTML, reply_markup=await _panel_keyboard()
                    )
                except TelegramBadRequest as exc:
                    # Двойной тап «Обновить» — нормальный жест, а не ошибка:
                    # Telegram отклоняет правку без изменений содержимого.
                    if "message is not modified" in str(exc).lower():
                        await callback.answer("Без изменений.")
                        return
                    raise
            await callback.answer("Обновлено.")
            return
        if action == "payouts":
            await callback.message.answer(await _payouts_text())
            await callback.answer("Список ниже.")
            return
        if action == "stakes":
            await callback.message.answer(await _stakes_panel_text())
            await callback.answer("Ставки ниже.")
            return
        if action == "refunds":
            await callback.message.answer(await _refunds_panel_text())
            await callback.answer("Возвраты ниже.")
            return
        if action == "adjust":
            await callback.message.answer(
                await _adjust_menu_text(),
                parse_mode=ParseMode.HTML,
                reply_markup=_adjust_keyboard(),
            )
            await callback.answer()
            return
        if action in {"money", "now"}:
            # Версия игры: со ставками / без. Вступает со СЛЕДУЮЩЕГО дня —
            # текущий день живёт по своему снимку Round.money_mode.
            from app.ops import set_money_mode

            want_money = action == "money"
            confirm_key = ("money", callback.from_user.id)
            now = time.monotonic()
            pending_at = _ADJ_PENDING.get(confirm_key)
            if pending_at is None or now - pending_at > _ADJ_CONFIRM_WINDOW:
                _ADJ_PENDING[confirm_key] = now
                confirm = (
                    (
                        "Переключить в версию БЕЗ ставок?\n"
                        "— приём ставок и платная смена выбора закрываются;\n"
                        "— вступит в силу со следующего дня (текущий живёт как есть);\n"
                        "— проголосовать кнопкой смогут все, входящие переводы пойдут обратно.\n\n"
                        "Нажми кнопку ещё раз для подтверждения."
                    )
                    if not want_money
                    else (
                        "Вернуть версию СО СТАВКАМИ?\n"
                        "— ставки и платная смена выбора снова доступны со следующего дня."
                        "\n\nНажми кнопку ещё раз для подтверждения."
                    )
                )
                await callback.answer(confirm, show_alert=True)
                return
            _ADJ_PENDING.pop(confirm_key, None)
            async with SessionLocal() as session:
                changed = await set_money_mode(session, want_money)
            if not changed:
                await callback.answer(
                    "Версия уже установлена." if not want_money else "Версия уже активна.",
                    show_alert=True,
                )
                return
            state_name = "со ставками и платной сменой выбора" if want_money else "без ставок"
            await callback.message.answer(
                f"💰 Версия «{state_name}» включена со СЛЕДУЮЩЕГО дня.\n"
                "Текущий день живёт по своему режиму.",
                reply_markup=await _panel_keyboard(),
            )
            await callback.answer()
            return
        if action in {"pause", "resume"}:
            # Стоп-кран с последствиями: первый тап предупреждает,
            # повторный тап той же кнопки в течение двух минут — делает.
            want_paused = action == "pause"
            confirm_key = (callback.from_user.id, callback.data or "")
            now = time.monotonic()
            pending_at = _ADJ_PENDING.get(confirm_key)
            if pending_at is None or now - pending_at > _ADJ_CONFIRM_WINDOW:
                _ADJ_PENDING[confirm_key] = now
                confirm = (
                    "Остановить игру: дни замрут, входящие переводы пойдут обратно "
                    "с пометкой о техработах. Нажми кнопку ещё раз для подтверждения."
                    if want_paused
                    else "Возобновить игру? Новый день откроется сам в ближайший тик. "
                    "Нажми кнопку ещё раз для подтверждения."
                )
                await callback.answer(confirm, show_alert=True)
                return
            _ADJ_PENDING.pop(confirm_key, None)
            changed, delivered = await _set_paused_and_broadcast(
                callback.bot, want_paused, "технические работы"
            )
            if not changed:
                await callback.answer(
                    "Игра уже на паузе." if want_paused else "Игра и так идёт.",
                    show_alert=True,
                )
                return
            chats = f" Анонс в {delivered} чат(ах)." if delivered else ""
            if callback.message is not None:
                await callback.message.answer(
                    ("⏸ Игра остановлена." if want_paused else "▶️ Игра возобновляется.")
                    + chats
                    + (" Снять паузу: /resume" if want_paused else "")
                )
            await callback.answer("Готово.")
            return
        if action == "treasury":
            from app.ton_pay import treasury_diagnostics

            await callback.message.answer(
                await treasury_diagnostics(), parse_mode=ParseMode.HTML
            )
            await callback.answer()
            return
        if action == "revenue":
            await callback.message.answer(await _revenue_text())
            await callback.answer()
            return
        if action in {"advance", "advance:go"}:
            if action != "advance:go":
                # Досрочное закрытие — действие с последствиями. Кнопка всегда
                # шлёт один и тот же callback_data, поэтому «нажми ещё раз»
                # фиксируется в памяти: второй тап в окне подтверждает.
                # (Раньше ветка ":go" была недостижима из UI — кнопка не могла
                # завершить день никогда, только просила «ещё раз» вечно.)
                confirm_key = (callback.from_user.id, callback.data or "")
                now = time.monotonic()
                pending_at = _ADJ_PENDING.get(confirm_key)
                if pending_at is None or now - pending_at > _ADJ_CONFIRM_WINDOW:
                    _ADJ_PENDING[confirm_key] = now
                    await callback.answer(
                        "Закрыть голосование досрочно и открыть следующий день? "
                        "Нажми кнопку ещё раз для подтверждения.",
                        show_alert=True,
                    )
                    return
                _ADJ_PENDING.pop(confirm_key, None)
            _answers: list[str] = []

            class _ShimMessage:
                """Лёгкий двойник Message: переиспользуем логику /advance."""

                chat = SimpleNamespace(type=ChatType.PRIVATE)
                text = "/advance"
                bot = callback.bot
                from_user = callback.from_user

                async def answer(self, text, *args, **kwargs):
                    _answers.append(str(text))

            await cmd_advance(_ShimMessage())
            summary = "\n".join(_answers)[:3500] or "Готово."
            if callback.message is not None:
                await callback.message.answer(f"⏩ {summary}")
            await callback.answer("День переключён.")
            return
        await callback.answer("Неизвестное действие.", show_alert=True)
    except Exception as exc:
        logger.exception("Действие пульта %s не удалось", action)
        await callback.answer(f"Не получилось: {exc}", show_alert=True)


async def _revenue_text() -> str:
    """Касса игры: ledger доходов из Income (для /revenue и пульта).

    Корректировки казны (manual_out/manual_in из /adjust) доходом не
    считаются — они видны в /treasury отдельной строкой.
    """
    from app.ops import MANUAL_IN_KIND, MANUAL_OUT_KIND

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    revenue_kinds = Income.kind.notin_([MANUAL_OUT_KIND, MANUAL_IN_KIND])

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
                .where(Income.created_at >= month_start, revenue_kinds)
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
                )
                .where(revenue_kinds)
                .group_by(Income.kind)
            )
        ).all()
    return (
        f"{money_mark('revenue')} Касса игры\n"
        f"{_block('Месяц', month_rows)}\n{_block('Всего', total_rows)}"
    )


@router.message(Command("incoming"))
async def cmd_incoming(message: Message) -> None:
    """Журнал входящих переводов казначея: откуда, сколько, чем стало.

    Только для хранителя. Источник — Income-леджер, куда watcher пишет
    каждый поступивший перевод (идемпотентно по хешу транзакции).
    """
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    from app.models import Income

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Income, Player.username, Player.first_name)
                .join(Player, Player.id == Income.player_id, isouter=True)
                .where(Income.kind == "ton")
                .order_by(Income.id.desc())
                .limit(15)
            )
        ).all()
    if not rows:
        await message.answer("Входящих переводов в журнале пока нет.")
        return
    lines = ["🧾 Входящие переводы казначея (последние 15):"]
    for income, username, first_name in rows:
        who = (
            (f"@{username}" if username else (first_name or f"id{income.player_id}"))
            if income.player_id
            else "неизвестный кошелёк"
        )
        stamp = income.created_at
        if stamp is not None:
            stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
            when = f"{stamp:%d.%m %H:%M} UTC"
        else:
            when = "—"
        lines.append(
            f"#{income.id} · {when} · {from_nano(income.amount_nanotons):g} Gram · {who}\n"
            f"   {income.note} · tonscan.org/tx/{income.unit_ref}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("stakes"))
async def cmd_stakes(message: Message) -> None:
    """Все ставки текущего и вчерашнего дня: статус каждой."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    async with SessionLocal() as session:
        latest = (
            await session.execute(
                select(Round).order_by(Round.day_index.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if latest is None:
            await message.answer("Дней ещё нет.")
            return
        rows = (
            await session.execute(
                select(Stake, Player.username, Player.first_name)
                .join(Player, Player.id == Stake.player_id)
                .where(Stake.round_id == latest.id)
                .order_by(Stake.id.asc())
                .limit(30)
            )
        ).all()
    if not rows:
        await message.answer(f"Ставок за день {latest.day_index} нет.")
        return
    lines = [f"Ставки дня {latest.day_index} ({latest.status.value}):"]
    for stake, username, first_name in rows:
        who = username or first_name or f"игрок {stake.player_id}"
        state = {"confirmed": "✅", "pending": "⏳", "rejected": "↩️"}.get(
            stake.status, stake.status
        )
        lines.append(
            f"  {who}: {from_nano(stake.amount_nanotons):g} Gram {state}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("revenue"))
async def cmd_revenue(message: Message) -> None:
    """Касса игры для хранителя: ledger доходов из Income.

    Звёзды сверяются с балансом бота во Fragment, Gram — с историей казны.
    """
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    await message.answer(await _revenue_text())


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

    @router.message.outer_middleware()
    async def _close_wallet_dialog_on_command(handler, event: Message, data: dict) -> None:
        """Любая команда, кроме /wallet, закрывает открытый диалог привязки.

        Специфичные хендлеры команд перехватывают апдейт раньше, чем
        on_private_fallback, поэтому там закрыть диалог невозможно — делаем
        это на уровне маршрутизатора: прошёл команду — значит ожидание адреса
        прервано. /wallet не трогаем: он сам продолжает/открывает привязку.
        """
        if isinstance(event, Message) and event.chat.type == ChatType.PRIVATE:
            text = (event.text or "").strip()
            if text.startswith("/") and not text.startswith("/wallet"):
                uid = event.from_user.id if event.from_user else 0
                try:
                    await _dialog_close(uid)
                except Exception:
                    logger.exception("Не удалось закрыть диалог кошелька (uid=%s)", uid)
        return await handler(event, data)

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
