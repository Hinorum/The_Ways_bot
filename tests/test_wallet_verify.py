"""Подтверждение владения кошельком (защита от сквата чужих адресов).

Привязка без доказательства владения позволяла присваивать публичный адрес
чужого кошелька и ловить на него чужие ставки и призы. Когда деньги включены,
кошелёк доверяется только после микро-перевода с него с мемо bv:<код>: код
знает владелец телеграм-аккаунта, перевести с адреса может только владелец
кошелька. Бесплатная версия подтверждения не требует — тянуть нечего.

Тестовая БД глобальная и переживает прогоны: адреса для привязок генерируются
случайно на каждый прогон, а своих игроков сбрасываем перед созданием — иначе
прошлый прогон занимал бы адрес или ID.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.db import SessionLocal
from app.handlers import cmd_wallet
from app.models import Payout, Player, Round, RoundStatus, Stake, WinRule
from app.payments import parse_verify_memo
from app.stakes import register_stake
from app.ton_utils import friendly_address, normalize_address, to_nano
from app.ton_watch import Transfer, process_transfer


_uid = 920_000


def next_uid() -> int:
    global _uid
    _uid += 1
    return _uid


def make_user(uid: int) -> SimpleNamespace:
    return SimpleNamespace(id=uid, username=f"u{uid}", first_name="Тест")


def make_message(uid: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=make_user(uid),
        text=text,
        answer=AsyncMock(),
        answer_media_group=AsyncMock(),
    )


def _raw(seed: int) -> str:
    return f"0:{seed:064x}"


def _fresh_address() -> str:
    """Случайный валидный адрес на каждый прогон (см. пояснение в шапке)."""
    return friendly_address(_raw(secrets.randbits(64)))


class _RecorderBot:
    def __init__(self):
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))


@pytest.fixture()
def ton_on(monkeypatch):
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "stake_confirm_seconds", 10_000)


async def _reset_player(uid: int) -> None:
    async with SessionLocal() as session:
        await session.execute(delete(Player).where(Player.id == uid))
        await session.commit()


async def _open_round(day_index: int) -> None:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        # Идемпотентно: общая БД переживает прогоны, день открываем заново.
        await session.execute(delete(Round).where(Round.day_index == day_index))
        session.add(
            Round(
                day_index=day_index,
                status=RoundStatus.OPEN,
                win_rule=WinRule.MAJORITY,
                rule_commitment="c",
                chapter_title="t",
                chapter_text="x",
                lore_summary="l",
                opens_at=now,
                voting_ends_at=now + timedelta(hours=20),
                tally_ends_at=now + timedelta(hours=21),
            )
        )
        await session.commit()


def test_parse_verify_memo_variants() -> None:
    assert parse_verify_memo("bv:ABC123") == "ABC123"
    assert parse_verify_memo("перевод bv: abc123 потом") == "ABC123"
    assert parse_verify_memo("bv:\u200bA1b2C3") == "A1B2C3"
    assert parse_verify_memo("без кода") is None
    assert parse_verify_memo("") is None
    assert parse_verify_memo(None) is None
    # rv-мемо не превращается в verify (разные контуры).
    assert parse_verify_memo("rv:42") is None


async def test_bind_under_money_requires_verification(ton_on) -> None:
    uid = next_uid()
    await _reset_player(uid)
    address = _fresh_address()
    message = make_message(uid, f"/wallet {address}")
    await cmd_wallet(message)
    text = message.answer.call_args.args[0]
    async with SessionLocal() as session:
        player = await session.get(Player, uid)
        assert player.wallet_address == normalize_address(address)
        assert player.wallet_verified is False
        assert player.wallet_verify_code
    assert "bv:" in text


async def test_bind_free_version_is_instant_verified() -> None:
    uid = next_uid()
    await _reset_player(uid)
    address = _fresh_address()
    message = make_message(uid, f"/wallet {address}")
    await cmd_wallet(message)
    text = message.answer.call_args.args[0]
    async with SessionLocal() as session:
        player = await session.get(Player, uid)
        assert player.wallet_address == normalize_address(address)
        assert player.wallet_verified is True
        assert player.wallet_verify_code is None
    assert "привязан" in text


async def test_watcher_verifies_and_refunds(ton_on) -> None:
    uid = next_uid()
    await _reset_player(uid)
    raw = _raw(0xCAFE)
    async with SessionLocal() as session:
        session.add(
            Player(
                id=uid,
                username="v",
                wallet_address=raw,
                wallet_verified=False,
                wallet_verify_code="ABC123",
            )
        )
        await session.commit()

    bot = _RecorderBot()
    status = await process_transfer(
        Transfer(
            tx_hash=f"wv-ok-{uid}",
            source=raw,
            value_nanotons=to_nano(0.2),
            comment="bv:abc123",
            utime=int(datetime.now(timezone.utc).timestamp()),
        ),
        bot=bot,
    )
    assert status.startswith("walletverify_")
    async with SessionLocal() as session:
        player = await session.get(Player, uid)
        assert player.wallet_verified is True
        assert player.wallet_verify_code is None
        refund = (
            await session.execute(
                select(Payout).where(Payout.tx_hash == f"wv-ok-{uid}")
            )
        ).scalar_one_or_none()
        assert refund is not None and refund.kind == "refund"
        assert refund.dest_address == raw
    assert [(m[0], "подтверждён" in m[1]) for m in bot.messages] == [(uid, True)]


async def test_watcher_ignores_wrong_code(ton_on) -> None:
    uid = next_uid()
    await _reset_player(uid)
    raw = _raw(0xC0FF)
    async with SessionLocal() as session:
        session.add(
            Player(
                id=uid,
                username="w",
                wallet_address=raw,
                wallet_verified=False,
                wallet_verify_code="ABC123",
            )
        )
        await session.commit()

    # Чужой код с правильного адреса: код секретен — недобор кода = не владелец.
    status = await process_transfer(
        Transfer(
            tx_hash=f"wv-wrong-{uid}",
            source=raw,
            value_nanotons=to_nano(0.2),
            comment="bv:XXXXXX",
            utime=int(datetime.now(timezone.utc).timestamp()),
        )
    )
    assert status == "refund_queued"
    async with SessionLocal() as session:
        player = await session.get(Player, uid)
        assert player.wallet_verified is False
        assert player.wallet_verify_code == "ABC123"


async def test_watcher_ignores_verify_from_foreign_address(ton_on) -> None:
    uid = next_uid()
    await _reset_player(uid)
    async with SessionLocal() as session:
        session.add(
            Player(
                id=uid,
                username="f",
                wallet_address=_raw(0xDEAD),
                wallet_verified=False,
                wallet_verify_code="ABC123",
            )
        )
        await session.commit()

    # Чужой адрес с верным кодом (перехватчик кода) — совпадения нет, возврат.
    status = await process_transfer(
        Transfer(
            tx_hash=f"wv-foreign-{uid}",
            source=_raw(0xBEEF),
            value_nanotons=to_nano(0.2),
            comment="bv:ABC123",
            utime=int(datetime.now(timezone.utc).timestamp()),
        )
    )
    assert status == "refund_queued"
    async with SessionLocal() as session:
        player = await session.get(Player, uid)
        assert player.wallet_verified is False


async def test_stake_blocked_while_verification_pending(ton_on) -> None:
    await _open_round(906)
    uid = next_uid()
    await _reset_player(uid)
    async with SessionLocal() as session:
        player = Player(
            id=uid,
            username="pending",
            wallet_address=_raw(0xAAA),
            wallet_verified=False,
            wallet_verify_code="ABC123",
        )
        session.add(player)
        await session.commit()
        round_row = (
            await session.execute(select(Round).where(Round.day_index == 906))
        ).scalar_one()

        status = await register_stake(
            session, round_row, player, to_nano(0.5), f"unver-tx-{uid}"
        )
        assert status == "wallet_unverified"


async def test_stake_accepted_after_verification(ton_on) -> None:
    uid = next_uid()
    await _reset_player(uid)
    async with SessionLocal() as session:
        player = Player(
            id=uid,
            username="verified",
            wallet_address=_raw(0xBBB),
            wallet_verified=True,
            wallet_verify_code=None,
        )
        session.add(player)
        await session.commit()
        round_row = (
            await session.execute(select(Round).where(Round.day_index == 907))
        ).scalar_one_or_none()
        if round_row is None:
            await _open_round(907)
            round_row = (
                await session.execute(select(Round).where(Round.day_index == 907))
            ).scalar_one()

        status = await register_stake(
            session, round_row, player, to_nano(0.5), f"ver-tx-{uid}"
        )
        assert status == "ok"


async def test_process_transfer_returns_stake_until_verified(ton_on) -> None:
    """Скват-сценарий: чужой адрес привязан, но код не подтверждён — ставка
    с реального владельца кошелька возвращается, а не засчитывается агрессору."""
    uid = next_uid()
    await _reset_player(uid)
    raw = _raw(0xCCC)
    async with SessionLocal() as session:
        session.add(
            Player(
                id=uid,
                username="squatter",
                wallet_address=raw,
                wallet_verified=False,
                wallet_verify_code="ABC123",
            )
        )
        await session.commit()

    status = await process_transfer(
        Transfer(
            tx_hash=f"sq-tx-{uid}",
            source=raw,
            value_nanotons=to_nano(0.5),
            comment="",
            utime=int(datetime.now(timezone.utc).timestamp()),
        )
    )
    assert status == "wallet_unverified"
    async with SessionLocal() as session:
        stake = (
            await session.execute(select(Stake).where(Stake.tx_hash == f"sq-tx-{uid}"))
        ).scalar_one_or_none()
        assert stake is None