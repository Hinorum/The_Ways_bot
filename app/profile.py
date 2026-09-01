"""Единое оформление публичного лица бота: обо мне, приветствие, меню команд.

Накатывается через Bot API при каждом старте (apply_profile) — BotFather
не нужен. Те же тексты продублированы в README для ручных полей (аватар,
картинка описания), которых у API нет.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
)

from app.config import settings

# «О боте» в профиле — не больше 120 символов.
BOT_ABOUT = (
    "The Way's — своя версия Пути: три тропы, один канон. "
    "Голосуй — а в последний день месяца лабиринт взвесит суд."
)

# Приветственный экран пустого чата («Что умеет этот бот?»), лимит 512.
def _bot_description() -> str:
    base = (
        "Ты — голос стаи в лабиринте.\n"
        "С тобой Баркод, Стежка, Вектор, Пиксель и Безымянная.\n"
        "Архивариус выносит три тропы и объявляет закон дня.\n"
        "Победивший путь впечатается в мир.\n"
        "Крыса — память кругов. Анубис — судья месяца.\n"
    )
    hints = []
    if settings.revote_enabled:
        hints.append("Передумал — смени путь (/change).")
    if settings.ton_enabled:
        hints.append("Веришь в расклад — ставь Gram (/wallet).")
    tail = " ".join(hints)
    if tail:
        base += tail + "\n"
    base += "Нажми START: Первый Лай уже ждёт."
    return base


BOT_DESCRIPTION = _bot_description()


def _build_commands() -> tuple[list[BotCommand], list[BotCommand]]:
    """Меню отражает включённые механики: без ставок — без кошелька и /top."""
    private = [
        BotCommand(command="start", description="Открыть Эхо Стаи"),
        BotCommand(command="today", description="Карты дня"),
        BotCommand(command="lore", description="Канон прошлых дней"),
        BotCommand(command="score", description="Твои Следы"),
        BotCommand(command="calling", description="Призвание твоей собаки"),
        BotCommand(command="best", description="Бестиарий Лабиринта"),
    ]
    if settings.revote_enabled:
        private.append(BotCommand(command="change", description="Сменить путь (⭐ или Gram)"))
    if settings.ton_enabled:
        private += [
            BotCommand(command="stake", description="Как поставить Gram на путь"),
            BotCommand(command="wallet", description="Привязать кошелёк Gram"),
            BotCommand(command="top", description="Копилки и лидеры"),
            BotCommand(command="fund", description="Фонд Стаи: баланс и журнал"),
        ]
    group = [
        BotCommand(command="today", description="Карты дня"),
        BotCommand(command="lore", description="Канон прошлых дней"),
    ]
    if settings.ton_enabled:
        group.append(BotCommand(command="stake", description="Как поставить Gram"))
    return private, group


# Личный чат: полный список. /advance хранителя сюда не попадает.
PRIVATE_COMMANDS, GROUP_COMMANDS = _build_commands()


async def apply_profile(bot: Bot) -> None:
    """Идемпотентно оформляет бота; сбои сети не мешают запуску."""
    log = logging.getLogger(__name__)
    try:
        await bot.set_my_short_description(BOT_ABOUT)
        await bot.set_my_description(BOT_DESCRIPTION)
        await bot.set_my_commands(
            PRIVATE_COMMANDS,
            scope=BotCommandScopeAllPrivateChats(),
        )
        await bot.set_my_commands(
            GROUP_COMMANDS,
            scope=BotCommandScopeAllGroupChats(),
        )
        log.info("Профиль бота обновлён: описание, обо мне, команды")
    except Exception as exc:
        log.warning("Не удалось обновить профиль бота: %s", exc)
