# Переиспользуемая инфраструктура хендлеров: роутер, защита диалога привязки,
# стоп-кран с объявлением, режим денег дня, флаг эха, общие клавиатуры.
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db import SessionLocal
from app.models import RoundStatus, WalletDialog
from app.rounds import ensure_current_round, get_active_round

logger = logging.getLogger(__name__)

router = Router()


async def _ensure_round():
    async with SessionLocal() as session:
        return await ensure_current_round(session)


async def _remember_flag(day_index: int) -> bool:
    """Кнопка памяти живёт только в дни с реально всплывшим эхом."""
    from app.echoes import surfaced_echoes_for_round

    try:
        async with SessionLocal() as session:
            return bool(await surfaced_echoes_for_round(session, day_index))
    except Exception:
        logger.warning("Флаг памяти дня %s не проверен", day_index, exc_info=True)
        return False


def _personal_keyboard(action: str, label: str) -> InlineKeyboardMarkup:
    """Кнопка личных данных: окно по нажатию видит только тот, кто нажал."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=action)]])


_DYOR_TEXT = (
    "Игра, а не вклад: бот и хранитель не отвечают за утраченные средства. "
    "Ты сам решаешь, на что ставить, и сам отвечаешь за ставки. DYOR."
)


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
