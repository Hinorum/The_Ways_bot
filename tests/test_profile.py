"""Публичное лицо бота: лимиты полей, состав меню, применение профиля."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats

from app.profile import (
    BOT_ABOUT,
    BOT_DESCRIPTION,
    GROUP_COMMANDS,
    PRIVATE_COMMANDS,
    apply_profile,
)


def test_profile_texts_respect_telegram_limits() -> None:
    assert len(BOT_ABOUT) <= 120, f"Обо мне длиннее 120: {len(BOT_ABOUT)}"
    assert len(BOT_ABOUT) >= 20
    assert len(BOT_DESCRIPTION) <= 512, f"Описание длиннее 512: {len(BOT_DESCRIPTION)}"


def test_command_menu_is_public_safe() -> None:
    """Меню без служебных команд и с валидными именами."""
    names = [c.command for c in PRIVATE_COMMANDS]
    for name in names:
        assert name == name.lower() and name.isalnum()
    # Хранительский /advance не светится в меню.
    assert "advance" not in names
    assert "advance" not in [c.command for c in GROUP_COMMANDS]
    # Описания команд в пределах лимита API.
    for cmd in [*PRIVATE_COMMANDS, *GROUP_COMMANDS]:
        assert 1 <= len(cmd.description) <= 256


def test_group_menu_is_subset_of_private() -> None:
    private = {c.command for c in PRIVATE_COMMANDS}
    for group_cmd in GROUP_COMMANDS:
        assert group_cmd.command in private


async def test_apply_profile_pushes_everything(monkeypatch) -> None:
    calls = []
    bot = SimpleNamespace(
        set_my_short_description=AsyncMock(side_effect=lambda text: calls.append(("short", text))),
        set_my_description=AsyncMock(side_effect=lambda description: calls.append(("desc", description))),
        set_my_commands=AsyncMock(
            side_effect=lambda commands, scope=None: calls.append(("cmds", scope.__class__.__name__))
        ),
    )
    await apply_profile(bot)
    pushed = [(k, v) for k, v in calls if k in ("short", "desc")]
    assert ("short", BOT_ABOUT) in pushed
    assert ("desc", BOT_DESCRIPTION) in pushed
    scopes = [v for k, v in calls if k == "cmds"]
    assert scopes.count("BotCommandScopeAllPrivateChats") == 1
    assert scopes.count("BotCommandScopeAllGroupChats") == 1

    async def broken(*a, **kw):
        raise RuntimeError("сеть молчит")

    silent_bot = SimpleNamespace(
        set_my_short_description=broken,
        set_my_description=AsyncMock(),
        set_my_commands=AsyncMock(),
    )
    await apply_profile(silent_bot)


def test_scopes_imported_for_signature_only() -> None:
    # Импорты живы и это те самые скоупы, что уходят в API.
    assert BotCommandScopeAllPrivateChats().type == "all_private_chats"
    assert BotCommandScopeAllGroupChats().type == "all_group_chats"
