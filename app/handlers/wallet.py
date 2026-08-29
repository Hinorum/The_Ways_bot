# Экономика игрока: кошелёк (просмотр, привязка, верификация владения),
# ставка дня, фонд, топ недели/месяца и общие тексты режима с деньгами.
from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone

from aiogram import F
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import SessionLocal
from app.models import LeaderboardPot, Player, Round, Stake
from app.rounds import get_active_round
from app.style import hint_mark, money_mark, ok_mark, warn_mark
from app.ton_utils import (
    friendly_address,
    from_nano,
    is_valid_ton_address,
    normalize_address,
)
from app.voting import get_vote, upsert_player

from .common import (
    _DYOR_TEXT,
    _active_round_money_mode,
    _dialog_close,
    _dialog_start,
    _personal_keyboard,
    router,
)

logger = logging.getLogger(__name__)


def _pct_text(value: float) -> str:
    """Русская типографика процента: «0,5%» (дробь через запятую), «1%» (без ,0)."""
    text = str(value)
    if text.endswith(".0"):
        text = text[:-2]
    return text.replace(".", ",")


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

    return (
        "\n\nРаспределение фонда дня:\n"
        f"• {pool_pct}% — поставившим на верный путь, пропорционально ставкам "
        f"(газ сети ~{settings.payout_fee_gram:g} Gram за перевод вычитается из пула заранее)\n"
        f"• {_pct_text(settings.pack_fund_pct)}% — Фонд Стаи: накопительный, разыгрывается хранителем\n"
        f"• {_pct_text(settings.weekly_pot_pct)}% — копилка недели: в понедельник топ-3 по верным путям "
        f"делит её ({pcts}%: сильнейший — больше); нужны кошелёк, {settings.weekly_min_days}+ дней "
        f"голосования и ставка за неделю; ничья — больший вклад Gram, затем кто раньше заявил о месте\n"
        f"• {_pct_text(settings.leaderboard_rake_pct)}% — копилка месяца: топ-3 лидеров /top делят её "
        f"({m_pcts}%), нужны кошелёк и ставка в месяце; ничья — вклад Gram, затем кто "
        f"раньше заявил о месте\n"
        f"• {_pct_text(settings.owner_rake_pct)}% — налог «Децентрализованному Богу»\n"
        "\nЕсли на верный путь не поставил никто — все ставки возвращаются целиком."
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
            if not player.wallet_verified and player.wallet_verify_code:
                body += (
                    "\n\n⚠️ Кошелёк ещё не подтверждён — с него не считаются ставки.\n"
                    f"Подтверди владение: отправь с него микро-перевод казначею (/stake) "
                    f"с комментарием <code>bv:{player.wallet_verify_code}</code>. "
                    "Сумма вернётся целиком."
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
        tail = f"\n\n{_DYOR_TEXT}" if settings.ton_enabled else ""
        return f"{body}{_economy_text()}{tail}"


def _wallet_verify_code() -> str:
    """Случайный код подтверждения владения кошельком (мемо bv:<код>)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(6))


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
        if settings.ton_enabled:
            # Деньги включены: привязка не доверяется сразу — иначе любой мог бы
            # присвоить публичный адрес чужого кошелька (их видно в постах дня) и
            # ловить на него чужие ставки и призы. Владелец доказывает контроль
            # микро-переводом с адреса с мемо bv:<код>: код знает только владелец
            # телеграм-аккаунта, а перевести с адреса может только владелец кошелька.
            player.wallet_verified = False
            player.wallet_verify_code = _wallet_verify_code()
            player.wallet_verify_created = datetime.now(timezone.utc)
        else:
            # Бесплатная версия без ставок и призов: доказывать нечего.
            player.wallet_verified = True
            player.wallet_verify_code = None
            player.wallet_verify_created = None
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
    if settings.ton_enabled:
        confirmation = (
            f"{ok_mark(str(uid))} Кошелёк привязан — осталось подтвердить, что он твой.\n"
            f"Отправь с него микро-перевод казначею (адрес: /stake) с комментарием:\n"
            f"<code>bv:{player.wallet_verify_code}</code>\n"
            "Сумму вернём целиком. Играть со ставками можно только после подтверждения — "
            "перевод до него вернётся обратно."
        )
    else:
        confirmation = f"{ok_mark(str(uid))} Кошелёк привязан. Теперь переводы с него будут считаться твоими ставками."
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(confirmation)
    else:
        # Без деталей: сам адрес уже засветился в сообщении группы.
        await message.answer(f"{ok_mark('group')} Кошелёк привязан (детали — в личке у бота).")
    return True


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
            lines.append(f"{medal} {ticket} {name} — {count} верн.")
    lines.append(
        f"🎟 прошёл отбор · 🔒 не хватает требований (кошелёк, "
        f"{max(1, settings.weekly_min_days)} дней голосования и ставка за неделю) · "
        "топ-3 делят копилку (ничья — больший вклад Gram, затем кто первый заявит о месте) · "
        "выплата в понедельник"
    )
    lines.append("")
    lines.append(f"{money_mark('top')} Копилка месяца: {month_pot_nanotons:g} Gram · места: {m_pcts}%")
    if not month_rows:
        lines.append("Верных путей в этом месяце ещё нет.")
    else:
        lines.append("Лидеры месяца по верным путям:")
        for place, (name, count, eligible) in enumerate(month_rows, 1):
            ticket = "🎟" if eligible else "🔒"
            lines.append(f"{place}. {ticket} {name} — {count} верн.")
    lines.append(
        "🎟 прошёл отбор · 🔒 не хватает требований (кошелёк и ставка в месяце) · "
        "топ-3 делят копилку (ничья — вклад Gram, затем кто первый заявит о месте) · "
        "выплата 1-го"
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
    lines.append(
        f"{_pct_text(settings.pack_fund_pct)}% банка дня копится сюда и не раздаётся сам. "
        "Хранитель распоряжается вручную — каждая раздача видна в этом журнале."
    )
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
