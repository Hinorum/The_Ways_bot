"""Идемпотентность журнала доходов watcher'а (Income.unit_ref + claim-маркер).

Окно перекрытия курсора перечитывает одни и те же транзакции, а два
инстанса на один перевод дерутся по check-then-insert: Income.unit_ref
уникален, но проигравший поймал бы IntegrityError и ушёл в stuck-список
ложным «не обработано». Метка ledger:{tx_hash} в той же транзакции, что и
строка, даёт at-most-once без падений и откатывается вместе с записью.
"""

from types import SimpleNamespace

from sqlalchemy import func, select

from app import ton_watch
from app.db import SessionLocal
from app.models import Income


def _transfer(tx_hash: str) -> SimpleNamespace:
    return SimpleNamespace(
        tx_hash=tx_hash,
        value_nanotons=123_000_000,
        source="0:" + "ab" * 32,
        utime=1_700_000_000,
        comment="",
    )


async def _income_count(tx_hash: str) -> int:
    async with SessionLocal() as session:
        return (
            await session.execute(
                select(func.count()).select_from(Income).where(Income.unit_ref == tx_hash)
            )
        ).scalar_one()


async def test_incoming_double_pass_keeps_single_row() -> None:
    tx = "aa" * 32
    transfer = _transfer(tx)
    async with SessionLocal() as session:
        await ton_watch._ledger_incoming(session, transfer, None, None, "stake")
    async with SessionLocal() as session:
        await ton_watch._ledger_incoming(session, transfer, None, None, "stake")
    assert await _income_count(tx) == 1


async def test_incoming_loses_external_claim_without_row() -> None:
    """Маркер уже поставлен другой копией — наша копия ничего не пишет (и не падает)."""
    tx = "bb" * 32
    async with SessionLocal() as session:
        await ton_watch.claim_once(session, f"ledger:{tx}")
        await session.commit()
    async with SessionLocal() as session:
        await ton_watch._ledger_incoming(session, _transfer(tx), None, None, "stake")
    assert await _income_count(tx) == 0


async def test_stuck_double_pass_keeps_single_row() -> None:
    tx = "cc" * 32
    transfer = _transfer(tx)
    async with SessionLocal() as session:
        await ton_watch._ledger_stuck_incoming(session, transfer, None, "refund:dust")
    async with SessionLocal() as session:
        await ton_watch._ledger_stuck_incoming(session, transfer, None, "refund:dust")
    assert await _income_count(tx) == 1