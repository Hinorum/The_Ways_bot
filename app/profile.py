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

# «О боте» в профиле — не больше 120 символов.
BOT_ABOUT = (
    "Ведущий «Эха Стаи»: каждый день — три пути и один канон. "
    "Голосуй, ставь — история помнит всё."
)

# Приветственный экран пустого чата («Что умеет этот бот?»), лимит 512.
BOT_DESCRIPTION = (
    "Ты — голос стаи потерянных собак в сети глючных порталов.\n\n"
    "Каждое утро объявляется закон дня, нейросеть пишет главу — "
    "и стая выбирает один из трёх путей. Победивший путь становится каноном: "
    "завтрашняя глава вырастет из этого выбора.\n\n"
    "Передумал — смени путь платно (/change). Веришь в расклад — ставь Gram (/wallet).\n"
    "Нажми START: Первый Лай уже ждёт."
)

# Личный чат: полный список. /advance хранителя сюда не попадает.
PRIVATE_COMMANDS = [
    BotCommand(command="start", description="Открыть Эхо Стаи"),
    BotCommand(command="today", description="Карты дня"),
    BotCommand(command="lore", description="Канон прошлых дней"),
    BotCommand(command="score", description="Твои Следы"),
    BotCommand(command="calling", description="Призвание твоей собаки"),
    BotCommand(command="best", description="Бестиарий Сети"),
    BotCommand(command="stake", description="Как поставить Gram на путь"),
    BotCommand(command="wallet", description="Привязать кошелёк Gram"),
    BotCommand(command="top", description="Копилка месяца и лидеры"),
    BotCommand(command="change", description="Сменить путь (⭐ или Gram)"),
]

# Группы и каналы: только то, что там уместно.
GROUP_COMMANDS = [
    BotCommand(command="today", description="Карты дня"),
    BotCommand(command="lore", description="Канон прошлых дней"),
    BotCommand(command="stake", description="Как поставить Gram на путь"),
]


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
