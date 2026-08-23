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
from app.rounds import claim_announcement, close_voting, create_next_round_detailed, ensure_current_round, finish_tally, get_active_round, get_latest_round, get_round, reset_game, write_epilogue
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
            "2% — угадавшим без ставки поровну, 0,5% — копилка месяца (/top), "
            "0,5% — на поддержку Стаи."
        )
    lines.append(
        f"\n{settings.world_name} — игра, а не вклад: бот и хранитель не отвечают за "
        "утраченные средства. Ты сам решаешь, на что ставить, и сам за это отвечаешь."
    )
    await message.answer("\n".join(lines))
    await cmd_today(message)


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    round_row = await _ensure_round()
    media = day_media_group(round_row)
    if media:
        await message.answer_media_group(media)
    await message.answer(status_text(round_row), reply_markup=cards_keyboard(round_row.id))


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


async def _score_text(user) -> str:
    async with SessionLocal() as session:
        player = await upsert_player(session, user)
        round_row = await get_active_round(session) or await get_latest_round(session)
        vote = await get_vote(session, round_row.id, player.id) if round_row else None
    if vote is None:
        choice = f"{hint_mark(str(user.id))} Сегодня ты ещё не выбрал путь."
    elif round_row.status in (RoundStatus.OPEN, RoundStatus.TALLYING):
        choice = f"{path_mark('care', str(user.id))} Сегодня твой путь: {POSITIONS[vote.card_position]}."
    else:
        choice = f"В прошлом дне ты выбрал путь {POSITIONS[vote.card_position]}."
    return (
        f"{choice}\n{result_mark(f'score:{user.id}')} Очки: {player.score}\n"
        f"Угаданных законов: {player.correct_picks}"
    )


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
    "• 2% — угадавшим путь без ставки, поровну (нужен привязанный кошелёк)\n"
    "• 0,5% — копилка месяца: в конце месяца её забирают лидеры /top\n"
    "• 0,5% — на поддержку Стаи\n"
    "\nЕсли на верный путь не поставил никто — все ставки возвращаются целиком."
)

_DYOR_TEXT = (
    "Игра, а не вклад: бот и хранитель не отвечают за утраченные "
    "средства по любой причине. Ты сам решаешь, на что ставить, "
    "и сам отвечаешь за свои ставки. DYOR."
)


async def _wallet_view_text(user) -> str:
    async with SessionLocal() as session:
        player = await upsert_player(session, user)
        if player.wallet_address:
            shown = friendly_address(player.wallet_address, testnet=settings.is_testnet)
            return (
                f"{money_mark(str(user.id))} Привязанный кошелёк:\n<code>{shown}</code>\n"
                "Переводы считаются твоими, если отправлены именно с этого кошелька.\n"
                "Чтобы перепривязать: /wallet <адрес>\n"
                "Как поставить на путь: /stake"
                f"{_ECONOMY_TEXT}\n\n{_DYOR_TEXT}"
            )
    return (
        f"{money_mark('none')} Кошелёк не привязан.\n"
        "Напиши /wallet — бот сам попросит адрес следующим сообщением.\n"
        "Он нужен для ставок на путь, бонуса угадавшим и выигрышей.\n"
        f"{_ECONOMY_TEXT}\n\n{_DYOR_TEXT}"
    )


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
    """Общая привязка для «/wallet <адрес>» и диалогового режима.

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
                await _dialog_start(message.from_user.id)
                await message.answer(
                    f"{hint_mark('wallet-dialog')} Пришли следующим сообщением адрес своего Gram-кошелька (бывший TON) — привяжу автоматически.\n"
                    "Он начинается с UQ или EQ и выглядит примерно так:\n"
                    "<code>UQD5…длинный набор букв и цифр</code>\n\n"
                    "Отменить: напиши <b>отмена</b>.",
                    parse_mode=ParseMode.HTML,
                )
                return
            await message.answer(await _wallet_view_text(message.from_user), parse_mode=ParseMode.HTML)
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
        await callback.message.answer(await _wallet_view_text(callback.from_user), parse_mode=ParseMode.HTML)
        await callback.answer()
        return
    text = await _wallet_view_text(callback.from_user)
    await callback.answer(text[:200], show_alert=True)


_STAKE_HOWTO = (
    "{mark} Ставка на путь — три шага:\n"
    "1. Привяжи кошелёк: /wallet (один раз и навсегда).\n"
    "2. Переведи от {min:g} до {max:g} Gram казначею со СВОЕГО привязанного кошелька:\n"
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
        max=settings.stake_max_ton,
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
                    state = "подтверждена ✅" if stake.status == "confirmed" else "ждёт подтверждения сети ⏳"
                    status = (
                        f"\n\n{money_mark(str(round_row.id))} Твоя ставка сегодня: {from_nano(stake.amount_nanotons):g} Gram ({state}).\n"
                        "Выигрыш придёт, если твой голос совпадёт с победившим путём."
                    )
                elif await get_vote(session, round_row.id, player.id) is not None:
                    status = f"\n\n{hint_mark('vote-first')} Ставки нет, но голос уже оставлен. Перевод засчитается в этот же день, если успеет до закрытия."
    return f"{head}{status}{_ECONOMY_TEXT}\n\n{_DYOR_TEXT}"


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
        await _stake_view_text(message.from_user),
        parse_mode=ParseMode.HTML,
        reply_markup=_stake_pay_keyboard(),
    )


@router.callback_query(F.data == "stake:view")
async def on_stake_view(callback: CallbackQuery) -> None:
    if callback.message is not None and callback.message.chat.type == ChatType.PRIVATE:
        await callback.message.answer(
            await _stake_view_text(callback.from_user),
            parse_mode=ParseMode.HTML,
            reply_markup=_stake_pay_keyboard(),
        )
        await callback.answer()
        return
    hint = (
        f"Ставка: переведи {settings.stake_min_ton:g}-{settings.stake_max_ton:g} Gram казначею "
        "со своего привязанного кошелька (/wallet), потом жми карту. Подробности: /stake в личке."
    )
    await callback.answer(hint[:200], show_alert=True)


def _format_top(rows: list[tuple[str, int]], pot_nanotons: float) -> str:
    from app.style import money_mark

    lines = [f"{money_mark('top')} Копилка месяца: {pot_nanotons:g} Gram"]
    if not rows:
        lines.append("Верных путей в этом месяце ещё нет — всё впереди.")
    else:
        lines.append("Лидеры месяца по верным путям:")
        for place, (name, count) in enumerate(rows, 1):
            lines.append(f"{place}. {name} — {count}")
    return "\n".join(lines)


@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    async with SessionLocal() as session:
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
        rows_raw = [(pid, count) for pid, count in result.all()]
        names: dict[int, str] = {}
        if rows_raw:
            pids = [pid for pid, _ in rows_raw]
            players = (
                await session.execute(select(Player).where(Player.id.in_(pids)))
            ).scalars().all()
            names = {
                player.id: player.username or player.first_name or f"игрок {player.id}"
                for player in players
            }
        month = now.strftime("%Y-%m")
        pot_row = (
            await session.execute(select(LeaderboardPot).where(LeaderboardPot.month == month))
        ).scalar_one_or_none()
    rows = [(names.get(pid, f"игрок {pid}"), count) for pid, count in rows_raw]
    pot_ton = from_nano(pot_row.nanotons) if pot_row is not None else 0.0
    await message.answer(_format_top(rows, pot_ton))


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
            nxt, created = await create_next_round_detailed(session)
        elif round_row.status.value == "tallying":
            round_row, closed_here = await finish_tally(session, round_row)
            if closed_here:
                await award_points(session, round_row)
                await write_epilogue(session, round_row)
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
    if delivered:
        await message.answer(f"День {nxt.day_index} объявлен в {len(delivered)} чат(ах).")
    else:
        # Ни одного подписанного чата — покажем всё прямо здесь.
        await message.answer(await results_message(round_row))
        media = day_media_group(nxt)
        if media:
            await message.answer_media_group(media)
        await message.answer(status_text(nxt), reply_markup=cards_keyboard(nxt.id))


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
    return dispatcher


async def create_bot() -> Bot:
    if not settings.bot_token or settings.bot_token.endswith("replace-me"):
        raise RuntimeError("Укажи BOT_TOKEN в .env — токен бесплатный у @BotFather.")
    return Bot(settings.bot_token)
