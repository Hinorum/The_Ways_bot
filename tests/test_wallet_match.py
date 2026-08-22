"""Регрессия: watcher сопоставляет отправителя транзакции с привязкой кошелька.

TonAPI отдаёт source в raw-hex, а игроки присылают user-friendly адрес.
Фикс: при привязке храним канонический raw (normalize_address), watcher
нормализует источник; старые UQ/EQ-записи разово мигрирует _migrate_wallet_formats.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from app.config import settings
from app.db import SessionLocal
from app.models import Payout, Player, Round, RoundStatus, WatcherState, WinRule
from app.ton_utils import is_valid_ton_address, normalize_address
from app.ton_watch import WALLET_NORM_KEY, Transfer, _migrate_wallet_formats, process_transfer


# Адреса уникальны в рамках прогона: players.wallet_address имеет UNIQUE,
# а глобальная тестовая БД общая для всех модулей.
NEW_STYLE = "UQpfcexKrlNjGFPF44W9am1o75Z6fs_QBdwVNzuhHVX2L4oo"
OLD_STYLE = "UQRDxMsYZjk2LA8Qm0yyQVAXTH3wHNKISLhiibo2dhVgDCOW"
RAW = normalize_address(NEW_STYLE)


@pytest.fixture()
def ton_on(monkeypatch):
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "stake_confirm_seconds", 10_000)


async def _open_round(day_index: int) -> None:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
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


async def test_new_style_binding_matches_raw_source(ton_on) -> None:
    assert is_valid_ton_address(NEW_STYLE)
    await _open_round(901)
    async with SessionLocal() as session:
        # Как теперь делает _bind_wallet: в БД лежит нормализованный raw.
        session.add(Player(id=910_001, username="bind", first_name="B", wallet_address=RAW))
        await session.commit()

    status = await process_transfer(
        Transfer(tx_hash="raw-match-1", source=RAW, value_nanotons=500_000_000, comment="", utime=1)
    )
    assert status == "ok"


async def test_old_friendly_row_would_miss_and_migration_fixes_it(ton_on) -> None:
    assert is_valid_ton_address(OLD_STYLE)
    old_raw = normalize_address(OLD_STYLE)
    assert old_raw != RAW
    assert old_raw != normalize_address(NEW_STYLE)
    async with SessionLocal() as session:
        # Старая запись: дружественная строка как есть (до фикса и миграции).
        session.add(Player(id=910_002, username="old", first_name="O", wallet_address=OLD_STYLE))
        await session.commit()

    # До миграции перевод не находит хозяина и уходит в возвраты — так выглядел баг.
    await _open_round(902)
    status_before = await process_transfer(
        Transfer(tx_hash="mig-before-1", source=old_raw, value_nanotons=300_000_000, comment="", utime=2)
    )
    assert status_before == "refund_queued"

    # Миграция переписывает запись в raw и ставит флаг. Флаг мог остаться
    # от предыдущих тестов в общей БД — имитируем свежий деплой сбросом.
    async with SessionLocal() as session:
        await session.execute(delete(WatcherState).where(WatcherState.key == WALLET_NORM_KEY))
        await session.commit()
        await _migrate_wallet_formats(session)
        player = await session.get(Player, 910_002)
        assert player.wallet_address == old_raw
        assert await session.get(WatcherState, WALLET_NORM_KEY) is not None

    # Повторный запуск — идемпотентный. Заодно гигиена общей БД: возврат
    # из «до-миграционного» промаха не должен остаться в очереди выплат.
    async with SessionLocal() as session:
        await _migrate_wallet_formats(session)
        await session.execute(delete(Payout).where(Payout.tx_hash == "mig-before-1"))
        await session.commit()
        stray = await session.execute(
            delete(Payout).where(Payout.tx_hash == "mig-before-1")
        )
        assert stray.rowcount == 0
