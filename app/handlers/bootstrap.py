# Сборка диспетчера и обработчики глобальных сбоев (всё, что вне роутинга).
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.enums import ChatType
from aiogram.types import ErrorEvent, Message

from app.config import settings

from .common import _dialog_close, router

logger = logging.getLogger(__name__)


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


_LAST_UPDATE_ERROR_ALERT = {"ts": 0.0}


_UPDATE_ERROR_ALERT_COOLDOWN = 3600.0


_PLAYER_ERROR_TEXT = (
    "⚠️ Лабиринт дрогнул — шаг не засчитан. Повтори ещё раз; "
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
            await callback.answer("Лабиринт дрогнул — попробуй ещё раз.", show_alert=True)
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
