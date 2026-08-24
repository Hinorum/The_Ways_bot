"""Диагностика казначея (/treasury): баланс, сверка пары, очередь.

Цель — инцидент «выплаты не уходят» разбирается одним сообщением бота
без раскрытия мнемоники и без логов.
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pytoniq_core.crypto.keys import mnemonic_new, mnemonic_to_private_key, private_key_to_public_key

from app import ton_pay
from app.config import settings
from app.handlers import cmd_treasury


def _derived_address(version: str, mnemonic: list[str], network_global_id: int) -> str:
    _, private_key = mnemonic_to_private_key(mnemonic)
    public_key = private_key_to_public_key(private_key)
    return ton_pay._wallet_address(version, public_key, network_global_id)


async def test_diagnostics_reports_balance_and_pair_match(monkeypatch) -> None:
    words = mnemonic_new(24)
    address = _derived_address("v4r2", words, -3)  # тестнет-глобаль
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_mnemonic", " ".join(words))
    monkeypatch.setattr(settings, "treasury_testnet_address", address)
    monkeypatch.setattr(settings, "owner_wallet_address", "")

    async def fake_state():
        return 4_213_000_000, "active", "tonapi"

    monkeypatch.setattr(ton_pay, "fetch_account_state", fake_state)
    text = await ton_pay.treasury_diagnostics()
    assert "Казначей (testnet)" in text
    assert "4.2130 Gram" in text
    assert "v4r2 ✓" in text  # пара мнемоника/адрес сходится
    assert "не задан" in text and "OWNER_WALLET_ADDRESS" in text


async def test_diagnostics_flags_pair_mismatch(monkeypatch) -> None:
    words = mnemonic_new(24)
    stranger = "0:" + os.urandom(32).hex()
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_mnemonic", " ".join(words))
    monkeypatch.setattr(settings, "treasury_testnet_address", stranger)

    async def silent():
        return None, None, "none"

    monkeypatch.setattr(ton_pay, "fetch_account_state", silent)
    text = await ton_pay.treasury_diagnostics()
    assert "не дают настроенный адрес" in text
    assert "Баланс: недоступен" in text


async def test_diagnostics_warns_on_empty_balance(monkeypatch) -> None:
    words = mnemonic_new(24)
    address = _derived_address("v5r1", words, -3)
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_mnemonic", " ".join(words))
    monkeypatch.setattr(settings, "treasury_testnet_address", address)
    monkeypatch.setattr(settings, "owner_wallet_address", address)

    async def empty_balance():
        return 0, "uninit", "tonapi"

    monkeypatch.setattr(ton_pay, "fetch_account_state", empty_balance)
    text = await ton_pay.treasury_diagnostics()
    assert "@testgiver_ton_bot" in text


async def test_cmd_treasury_is_admin_only(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", "4242")

    outsider = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
    )
    await cmd_treasury(outsider)
    assert "только для хранителя" in outsider.answer.call_args.args[0]

    admin = SimpleNamespace(
        from_user=SimpleNamespace(id=4242),
        answer=AsyncMock(),
    )
    async def fake_diag() -> str:
        return "🏛 Казначей (testnet)"

    monkeypatch.setattr("app.ton_pay.treasury_diagnostics", fake_diag)
    await cmd_treasury(admin)
    admin.answer.assert_awaited_once()


def test_pair_check_detects_invalid_mnemonic(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "ton_network", "testnet")
    monkeypatch.setattr(settings, "treasury_testnet_mnemonic", "один два три")
    monkeypatch.setattr(settings, "treasury_testnet_address", "0:" + os.urandom(32).hex())
    assert "неполная" in ton_pay.treasury_pair_check_text()
