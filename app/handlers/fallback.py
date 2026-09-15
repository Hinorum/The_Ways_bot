# Диалог привязки кошелька: следующее сообщение игрока — это адрес.
# Регистрируется ПОСЛЕДНИМ (импортируется в __init__ последним), чтобы
# команды перехватывались своими обработчиками раньше.
from __future__ import annotations

import logging

from aiogram import F
from aiogram.enums import ChatType
from aiogram.types import Message

from app.style import hint_mark, ok_mark

from .common import _dialog_close, _dialog_open, router
from .wallet import _bind_wallet

logger = logging.getLogger(__name__)


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
