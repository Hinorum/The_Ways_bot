# Пульт хранителя /panel: сводка дня, очереди выплат и кнопки действий.
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from aiogram import F
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.models import Payout, Round, RoundStatus
from app.ton_utils import from_nano

from .admin import (
    _ADJ_CONFIRM_WINDOW,
    _ADJ_PENDING,
    _adjust_keyboard,
    _adjust_menu_text,
    cmd_advance,
)
from .common import _set_paused_and_broadcast, router
from .payout import (
    _payouts_text,
    _refunds_panel_text,
    _revenue_text,
    _stakes_panel_text,
)

logger = logging.getLogger(__name__)


_PANEL_FOOTER = (
    "\n\n🕹 <b>Управление</b> (в личке):\n"
    "/advance — закрыть день досрочно и открыть следующий\n"
    "/today — превью поста игрока · /lore — канон\n"
    "/incoming — журнал входящих переводов казначея\n"
    "/stakes — ставки дня · /payouts — очередь выплат (причина у каждой строки)\n"
    "/payout &lt;id&gt; retry|spam — ручной разбор долга\n"
    "/return &lt;id&gt; — ручной возврат ставки\n"
    "/treasury — казначей: баланс и пара ключей\n"
    "/adjust — сверка казны: ручной вывод или пропажа средств ⚖️\n"
    "/fundout &lt;Gram&gt; &lt;причина&gt; — раздача Фонда Стаи\n"
    "/disputes — список открытых споров\n"
    "/dispute — жалоба на итог / разбор спора\n"
    "/finalize — ручная финализация застрявших дней\n"
    "/refinalize — принудительная перефинализация\n"
    "/pause … /resume — стоп-кран игры (техработы) ⏸\n"
    "/revenue — касса (Stars/Gram)\n"
    "/resetgame confirm [keepstory] — полный сброс ⚠️\n"
    "Картинки-заглушки дорисовываются сами через 15 мин после анонса."
)


async def _admin_panel_text(session=None) -> str:
    """Сводка состояния игры + подсказки по командам, одним сообщением."""
    _own_session = session is None
    if _own_session:
        session = SessionLocal()
        await session.__aenter__()
    try:
        return await _build_panel_text(session)
    finally:
        if _own_session:
            await session.__aexit__(None, None, None)


async def _build_panel_text(session) -> str:
    """Внутренняя логика сборки текста пульта."""
    from app.ops import snapshot
    from app.ops import is_game_paused as _paused_flag
    from app.ops import paused_reason as _pause_reason
    from app.season import act_line_short, get_cached_anchor, run_position

    snap = await snapshot()
    lines = ["🎛 <b>ПУЛЬТ ХРАНИТЕЛЯ</b>"]
    try:
        if await _paused_flag(session):
            lines.append(
                f"⏸ ИГРА НА ПАУЗЕ ({await _pause_reason(session) or 'техработы'}) "
                "— снять: /resume. Входящие переводы возвращаются автоматически."
            )
    except Exception:
        pass
    try:
        from app.ops import money_mode_enabled

        money_on = await money_mode_enabled(session)
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
    # Призы, ждущие кошелька игрока: финализация не знала адреса, диспетчер
    # оживит строку сам, как только игрок привяжет /wallet (retry не нужен).
    try:
        no_wallet_count = (
            await session.execute(
                select(func.count()).select_from(Payout).where(
                    Payout.dest_address == "",
                    Payout.kind.in_(["prize", "refund"]),
                    Payout.player_id.isnot(None),
                    Payout.status.notin_(["sent", "dismissed"]),
                )
            )
        ).scalar_one()
        if no_wallet_count:
            lines.append(
                f"🪙 Призов без кошелька: {no_wallet_count} — уйдут сами, "
                "когда игрок привяжет адрес. Разбор: /payouts."
            )
    except Exception:
        pass
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
    from app.ops import is_game_paused, money_mode_enabled

    async with SessionLocal() as session:
        paused = await is_game_paused(session)
        money_on = await money_mode_enabled(session)
    pause_button = (
        InlineKeyboardButton(text="▶️ Возобновить игру", callback_data="panel:resume")
        if paused
        else InlineKeyboardButton(text="⏸ Пауза игры", callback_data="panel:pause")
    )
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
                        "Переключить в версию БЕЗ ставок? Ставки и платная смена "
                        "выбора закроются со следующего дня; входящие переводы уйдут "
                        "обратно. Нажми кнопку ещё раз для подтверждения."
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
            if callback.message is not None:
                async with SessionLocal() as session:
                    text = await _admin_panel_text(session)
                try:
                    await callback.message.edit_text(
                        f"💰 Версия «{state_name}» включена со СЛЕДУЮЩЕГО дня.\n"
                        "Текущий день живёт по своему режиму.\n\n"
                        + text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=await _panel_keyboard(),
                    )
                except TelegramBadRequest as exc:
                    if "message is not modified" in str(exc).lower():
                        pass
                    else:
                        raise
            await callback.answer("Готово.")
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
            status_msg = "⏸ Игра остановлена." if want_paused else "▶️ Игра возобновляется."
            if callback.message is not None:
                async with SessionLocal() as session:
                    text = await _admin_panel_text(session)
                try:
                    await callback.message.edit_text(
                        status_msg + chats + "\n\n" + text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=await _panel_keyboard(),
                    )
                except TelegramBadRequest as exc:
                    if "message is not modified" in str(exc).lower():
                        pass
                    else:
                        raise
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
                async with SessionLocal() as session:
                    text = await _admin_panel_text(session)
                try:
                    await callback.message.edit_text(
                        f"⏩ {summary}\n\n" + text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=await _panel_keyboard(),
                    )
                except TelegramBadRequest as exc:
                    if "message is not modified" in str(exc).lower():
                        pass
                    else:
                        raise
            await callback.answer("День переключён.")
            return
        await callback.answer("Неизвестное действие.", show_alert=True)
    except Exception as exc:
        logger.exception("Действие пульта %s не удалось", action)
        await callback.answer(f"Не получилось: {exc}", show_alert=True)
