"""Guard-тесты командного контура: только админ рушит игру, вебхук — с секретом."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import main as main_module
from app.config import settings
from app.handlers import cmd_advance, cmd_resetgame


def _message(user_id: int | None) -> SimpleNamespace:
    from_user = None if user_id is None else SimpleNamespace(id=user_id)
    return SimpleNamespace(from_user=from_user, answer=AsyncMock())


@pytest.mark.parametrize("command", [cmd_resetgame, cmd_advance])
async def test_nonadmin_cannot_reset_or_advance(
    command, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "admin_ids", "")
    message = _message(777_777)
    await command(message)
    message.answer.assert_awaited_once()
    assert "хранителя" in message.answer.await_args.args[0]


@pytest.mark.parametrize("command", [cmd_resetgame, cmd_advance])
async def test_anonymous_channel_post_cannot_reset(
    command, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Канальные посты/анонимы приходят без from_user — доступ закрыт."""
    monkeypatch.setattr(settings, "admin_ids", "42")
    message = _message(None)
    await command(message)
    message.answer.assert_awaited_once()


def test_webhook_without_secret_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "webhook_base_url", "https://example.onrender.com")
    monkeypatch.setattr(settings, "webhook_secret", "")
    with pytest.raises(RuntimeError) as excinfo:
        main_module.ensure_webhook_secret()
    assert "WEBHOOK_SECRET" in str(excinfo.value)


def test_webhook_with_secret_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "webhook_base_url", "https://example.onrender.com")
    monkeypatch.setattr(settings, "webhook_secret", "s3cret")
    main_module.ensure_webhook_secret()


def test_polling_mode_needs_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "webhook_base_url", "")
    monkeypatch.setattr(settings, "render_external_url", "")
    monkeypatch.setattr(settings, "render_external_hostname", "")
    monkeypatch.setattr(settings, "webhook_secret", "")
    main_module.ensure_webhook_secret()
