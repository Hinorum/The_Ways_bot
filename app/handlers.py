from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
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
    LeaderboardPot,
    Player,
    RevoteGrant,
    Round,
    RoundStatus,
    Stake,
    StoryBeat,
    Vote,
)
from app.payments import build_revote_payload, parse_revote_payload, revote_memo
from app.rounds import claim_announcement, close_voting, create_next_round_detailed, ensure_current_round, finish_tally, get_active_round, get_latest_round, get_round, reset_game, write_epilogue
from app.tally import award_points
from app.ton_utils import from_nano, is_valid_ton_address
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
        f"Это «{settings.world_name}» — фанатская игра по мотивам Lost Dogs.",
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
        lines.append("/change — сменить выбор (⭐ Stars или TON)")
    if settings.ton_enabled:
        lines.append("/wallet — кошелёк для ставок")
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
        await message.answer("Канон ещё пуст: первый След появится после итогов дня.")
        return
    text, truncated = _canon_text(beats)
    if truncated:
        text = "Ранние дни растворились в шуме порталов.\n\n" + text
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
        choice = "Сегодня ты ещё не выбрал путь."
    elif round_row.status in (RoundStatus.OPEN, RoundStatus.TALLYING):
        choice = f"Сегодня твой путь: {POSITIONS[vote.card_position]}."
    else:
        choice = f"В прошлом дне ты выбрал путь {POSITIONS[vote.card_position]}."
    return f"{choice}\nОчки: {player.score}\nУгаданных законов: {player.correct_picks}"


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
        "ok": f"Путь {POSITIONS[position]} принят. Счёт скрыт до итогов.",
        "already": "Ты уже оставил След сегодня.",
        "closed": "Голосование закрыто, идёт подсчёт.",
        "invalid": "Такого пути нет на карте Стаи.",
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
            return (
                f"Привязанный кошелёк: {player.wallet_address[:6]}…{player.wallet_address[-6:]}\n"
                "Чтобы перепривязать: /wallet <адрес>"
                f"{_ECONOMY_TEXT}\n\n{_DYOR_TEXT}"
            )
    return (
        "Кошелёк не привязан.\n"
        "Привяжи TON-кошелёк: /wallet <твой адрес>\n"
        "Он нужен для ставок на путь, бонуса угадавшим и выигрышей.\n"
        f"{_ECONOMY_TEXT}\n\n{_DYOR_TEXT}"
    )


@router.message(Command("wallet"))
async def cmd_wallet(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) == 1:
        text = await _wallet_view_text(message.from_user)
        if message.chat.type == ChatType.PRIVATE:
            await message.answer(text)
        else:
            await message.answer(
                "Информация о кошельке видна только тебе — нажми кнопку.",
                reply_markup=_personal_keyboard("wallet:view", "Мой кошелёк"),
            )
        return
    address = parts[1]
    if not is_valid_ton_address(address):
        await message.answer("Это не похоже на адрес TON. Проверь и пришли снова: /wallet <адрес>")
        return
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
            await message.answer("У тебя ставка в игре — кошелёк закреплён до итогов дня.")
            return
        player.wallet_address = address.strip()
        player.wallet_linked_at = datetime.now(timezone.utc)
        await session.commit()
    confirmation = "Кошелёк привязан. Теперь переводы с него будут считаться твоими ставками."
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(confirmation)
    else:
        # Без деталей: сам адрес уже засветился в сообщении группы.
        await message.answer("Кошелёк привязан (детали — в личке у бота).")


@router.callback_query(F.data == "wallet:view")
async def on_wallet_view(callback: CallbackQuery) -> None:
    if callback.message is not None and callback.message.chat.type == ChatType.PRIVATE:
        await callback.message.answer(await _wallet_view_text(callback.from_user))
        await callback.answer()
        return
    text = await _wallet_view_text(callback.from_user)
    await callback.answer(text[:200], show_alert=True)


def _format_top(rows: list[tuple[str, int]], pot_nanotons: float) -> str:
    lines = [f"Копилка месяца: {pot_nanotons:g} TON"]
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
            [InlineKeyboardButton(text=f"💎 {settings.revote_ton:g} TON", callback_data=f"payton:{round_id}")],
        ]
    )


async def _revote_status(user) -> tuple[str, int | None]:
    """(текст, round_id). round_id не None только если день открыт И путь выбран."""
    async with SessionLocal() as session:
        player = await upsert_player(session, user)
        round_row = await get_active_round(session)
        if round_row is None or round_row.status != RoundStatus.OPEN:
            return "День закрыт — идёт подсчёт. Менять путь поздно.", None
        vote = await get_vote(session, round_row.id, player.id)
        if vote is None:
            return "Ты ещё не выбрал путь сегодня — первый выбор бесплатный.", None
        return (
            f"Сегодня твой путь: {POSITIONS[vote.card_position]}. "
            "Оплати смену и нажми другую карту. Грант действует до закрытия дня.",
            round_row.id,
        )


@router.message(Command("change"))
async def cmd_change(message: Message) -> None:
    if not settings.revote_enabled:
        await message.answer("Смена выбора сейчас недоступна.")
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
        await session.commit()
    if valid:
        await message.answer("Оплачено ⭐ Нажми теперь на другую карту — выбор обновится.")
    else:
        await message.answer(
            "Оплата прошла, но день уже закрылся — грант сохранён. "
            "Напиши хранителю игры для возврата."
        )


@router.callback_query(F.data.startswith("payton:"))
async def on_payton(callback: CallbackQuery) -> None:
    raw = callback.data.split(":")[1]
    if callback.message is None or callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Оплата — только в личке у бота (/change).", show_alert=True)
        return
    if not settings.ton_enabled or not settings.active_treasury_address:
        await callback.answer("Приём TON ещё не включён. Используй Stars.", show_alert=True)
        return
    if not raw.isdigit():
        await callback.answer("Некорректный счёт.", show_alert=True)
        return
    address = settings.active_treasury_address
    await callback.message.answer(
        f"Переведи {settings.revote_ton:g} TON (или больше) на адрес казначея:\n"
        f"<code>{address}</code>\n\n"
        f"Обязательно с комментарием (memo):\n<code>{revote_memo(int(raw))}</code>\n\n"
        "Кошелёк должен быть привязан: /wallet. Грант придёт в течение минуты. "
        "Неиспользованный до конца дня грант сгорает."
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
    if message.from_user.id not in settings.admin_id_set:
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
    if message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    words = (message.text or "").lower().split()
    if "confirm" not in words:
        await message.answer(
            "Это сотрёт все дни, голоса, ставки, выплаты и очки игроков.\n"
            "Кошельки, чаты и копилка месяца останутся.\n"
            "<code>/resetgame confirm</code> — полный сброс вместе с каноном истории.\n"
            "<code>/resetgame confirm keepstory</code> — сброс счётов, "
            "но мир и эхо прошлого сохраняются."
        )
        return
    keep_story = "keepstory" in words
    async with SessionLocal() as session:
        new_round = await reset_game(session, keep_story=keep_story)
        first = await claim_announcement(session, new_round)
    if not first:
        await message.answer(f"День {new_round.day_index} только что объявил другой процесс бота.")
        return
    await announce_new_day(message.bot, new_round)
    mode = "Канон истории сохранён." if keep_story else "История стёрта полностью."
    await message.answer(
        f"Игра обнулена. День {new_round.day_index} объявлен в чатах. "
        f"{mode} Голосование до {new_round.voting_ends_at:%H:%M} UTC."
    )


def build_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher


async def create_bot() -> Bot:
    if not settings.bot_token or settings.bot_token.endswith("replace-me"):
        raise RuntimeError("Укажи BOT_TOKEN в .env — токен бесплатный у @BotFather.")
    return Bot(settings.bot_token)
