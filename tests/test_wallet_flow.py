"""Диалог привязки кошелька и инструкция /stake.

/wallet без адреса открывает режим ожидания: следующее сообщение игрока —
адрес, привязка происходит без единой команды. /stake показывает адрес
казначея, шаги и статус ставки; порядок «голос/перевод» не важен.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.db import SessionLocal, init_db
from app.handlers import (
    cmd_stake,
    cmd_wallet,
    on_private_fallback,
    on_stake_view,
)
from app.models import Player, Round, RoundStatus, Stake, WalletDialog, WinRule


# Адреса уникальны в рамках прогона: players.wallet_address имеет UNIQUE,
# а глобальная тестовая БД общая для всех модулей.
DIALOG_ADDRESS = "UQDD39VS5jcptHL8vMjEXrzGaRcCVYto7HUn4bp5gj8ZmdnX"
LOCKED_ADDRESS = "UQED39VS5jcptHL8vMjEXrzGaRcCVYto7HUn4bp5gj8ZmdnE"
RESTART_ADDRESS = "UQFD39VS5jcptHL8vMjEXrzGaRcCVYto7HUn4bp5gj8ZmdnF"

_uid = 800_000


def make_user(uid: int) -> SimpleNamespace:
    return SimpleNamespace(id=uid, username=f"u{uid}", first_name="Тест")


def next_uid() -> int:
    global _uid
    _uid += 1
    return _uid


def make_message(chat_type: str, uid: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type=chat_type),
        from_user=make_user(uid),
        text=text,
        answer=AsyncMock(),
        answer_media_group=AsyncMock(),
    )


@pytest.fixture(scope="module", autouse=True)
async def _handlers_db():
    await init_db()
    yield


async def _dialog_active(uid: int) -> bool:
    async with SessionLocal() as session:
        return await session.get(WalletDialog, uid) is not None


@pytest.fixture(autouse=True)
def _clean_pending():
    yield


@pytest.fixture(autouse=True)
async def _wipe_dialogs_after():
    yield
    async with SessionLocal() as session:
        await session.execute(WalletDialog.__table__.delete())
        await session.commit()


async def start_dialog(uid: int) -> None:
    await cmd_wallet(make_message("private", uid, "/wallet"))


async def test_wallet_without_args_opens_dialog() -> None:
    uid = next_uid()
    message = make_message("private", uid, "/wallet")
    await cmd_wallet(message)
    text = message.answer.call_args.args[0]
    assert "следующим сообщением" in text
    assert await _dialog_active(uid)


async def test_next_message_binds_address() -> None:
    uid = next_uid()
    await start_dialog(uid)
    message = make_message("private", uid, DIALOG_ADDRESS)
    await on_private_fallback(message)
    assert "привязан" in message.answer.call_args.args[0]
    assert not await _dialog_active(uid)


async def test_bad_address_keeps_dialog_open() -> None:
    uid = next_uid()
    await start_dialog(uid)
    message = make_message("private", uid, "не адрес")
    await on_private_fallback(message)
    assert "не похоже" in message.answer.call_args.args[0]
    assert await _dialog_active(uid)


async def test_cancel_word_closes_dialog() -> None:
    uid = next_uid()
    await start_dialog(uid)
    message = make_message("private", uid, "отмена")
    await on_private_fallback(message)
    assert "Отменено" in message.answer.call_args.args[0]
    assert not await _dialog_active(uid)


async def test_other_command_closes_dialog_silently() -> None:
    """Обычные команды перехватываются раньше fallback; если дошло сюда —
    молча выходим из режима ожидания."""
    uid = next_uid()
    await start_dialog(uid)
    message = make_message("private", uid, "/чтоугодно")
    await on_private_fallback(message)
    assert message.answer.call_count == 0
    assert not await _dialog_active(uid)


async def test_dialog_survives_restart() -> None:
    """Диалог живёт в БД: после «рестарта» (новый процесс, та же БД) ожидание
    продолжает работать — перезапуск не теряет игрока посреди привязки."""
    uid = next_uid()
    await start_dialog(uid)
    # Рестарт ничего не стирает: helpers читают ту же таблицу заново.
    message = make_message("private", uid, RESTART_ADDRESS)
    await on_private_fallback(message)
    assert "привязан" in message.answer.call_args.args[0]


async def test_fallback_ignores_strangers() -> None:
    message = make_message("private", next_uid(), "какой-то текст")
    await on_private_fallback(message)
    assert message.answer.call_count == 0


async def test_rebind_locked_while_stake_in_game(monkeypatch) -> None:
    monkeypatch.setattr(settings, "use_free_images", False)
    monkeypatch.setattr(settings, "use_free_story_llm", False)

    uid = next_uid()
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        session.add(Player(id=uid, username="staker", wallet_address=LOCKED_ADDRESS))
        rnd = Round(
            day_index=99,
            status=RoundStatus.OPEN,
            win_rule=WinRule.MAJORITY,
            rule_commitment="c",
            chapter_title="t",
            chapter_text="x",
            lore_summary="l",
            opens_at=now,
            voting_ends_at=now + timedelta(hours=1),
            tally_ends_at=now + timedelta(hours=2),
        )
        session.add(rnd)
        await session.flush()
        session.add(
            Stake(
                round_id=rnd.id,
                player_id=uid,
                amount_nanotons=100_000_000,
                tx_hash="lock-tx",
                status="confirmed",
            )
        )
        await session.commit()

    message = make_message("private", uid, f"/wallet {DIALOG_ADDRESS}")
    await cmd_wallet(message)
    assert "закреплён до итогов дня" in message.answer.call_args.args[0]


async def test_stake_shows_treasury_and_order_note(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "treasury_address", "EQ" + "a" * 46)
    monkeypatch.setattr(settings, "treasury_testnet_address", "")
    message = make_message("private", next_uid(), "/stake")
    await cmd_stake(message)
    text = message.answer.call_args.args[0]
    assert ("EQ" + "a" * 46) in text
    assert "Порядок не важен" in text
    assert "три шага" in text


async def test_stake_in_group_hides_details() -> None:
    message = make_message("supergroup", next_uid(), "/stake")
    await cmd_stake(message)
    kwargs = message.answer.call_args.kwargs
    assert kwargs.get("reply_markup") is not None


async def test_stake_button_alert_short() -> None:
    callback = SimpleNamespace(
        data="stake:view",
        from_user=make_user(next_uid()),
        message=SimpleNamespace(chat=SimpleNamespace(type="supergroup")),
        answer=AsyncMock(),
    )
    await on_stake_view(callback)
    args, kwargs = callback.answer.call_args
    assert len(args[0]) <= 200
    assert kwargs.get("show_alert") is True
