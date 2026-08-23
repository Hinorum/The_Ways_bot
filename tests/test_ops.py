"""Тесты операционного контура: ретраи выплат, курсор watcher'а,
автоматическая выплата копилки месяца и стоп-фильтр генераций."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import ton_pay
from app.config import settings
from app.db import SessionLocal
from app.leaderboard import MARKER_KEY, previous_month_key, settle_month_if_due
from app.models import (
    LeaderboardPot,
    Payout,
    Player,
    Round,
    RoundStatus,
    Vote,
    WatcherState,
    WinRule,
)
from app.story import _parse_chapter, generate_epilogue, text_is_clean
from app.ton_utils import to_nano


def _closed_round(day_index: int, tally_at: datetime) -> Round:
    return Round(
        day_index=day_index,
        status=RoundStatus.CLOSED,
        win_rule=WinRule.MAJORITY,
        rule_commitment="c",
        chapter_title="t",
        chapter_text="text",
        lore_summary="lore",
        opens_at=tally_at - timedelta(hours=25),
        voting_ends_at=tally_at - timedelta(hours=1),
        tally_ends_at=tally_at,
        winner_card=1,
        vote_counts_json="{}",
    )


# ---------- Ретраи выплат ----------


async def _mk_payout(db: AsyncSession, **kwargs) -> Payout:
    payout = Payout(
        round_id=None,
        player_id=None,
        kind="prize",
        amount_nanotons=to_nano(0.5),
        dest_address="0:" + os.urandom(16).hex(),
        network="testnet" if settings.is_testnet else "mainnet",
        **kwargs,
    )
    db.add(payout)
    await db.commit()
    return payout


async def test_failed_payout_is_retried_until_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "payout_max_attempts", 3)
    async with SessionLocal() as db:
        payout = await _mk_payout(db, status="failed", attempts=1)
        try:
            assert await ton_pay.dispatch_pending_payouts() == 0
            await db.refresh(payout)
            assert payout.attempts == 2 and payout.status == "pending"

            assert await ton_pay.dispatch_pending_payouts() == 0
            await db.refresh(payout)
            assert payout.attempts == 3 and payout.status == "failed"
            # Лимит исчерпан — больше не ретраится.
            assert await ton_pay.dispatch_pending_payouts() == 0
            await db.refresh(payout)
            assert payout.attempts == 3 and payout.status == "failed"
        finally:
            await db.delete(payout)
            await db.commit()


async def test_successful_send_resets_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)

    async def fake_send(dest, amount, comment):
        return "tx-ok-1"

    monkeypatch.setattr(ton_pay, "send_ton_transfer", fake_send)
    async with SessionLocal() as db:
        payout = await _mk_payout(db, status="failed", attempts=4)
        try:
            assert await ton_pay.dispatch_pending_payouts() >= 1
            await db.refresh(payout)
            assert payout.status == "sent" and payout.tx_hash == "tx-ok-1" and payout.attempts == 0
        finally:
            await db.delete(payout)
            await db.commit()


async def test_stuck_sending_is_revived_and_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Падение сервиса между broadcast и коммитом: зависшая sending доигрывается."""
    monkeypatch.setattr(settings, "ton_enabled", True)

    async def fake_send(dest, amount, comment):
        return "tx-recovered"

    monkeypatch.setattr(ton_pay, "send_ton_transfer", fake_send)
    async with SessionLocal() as db:
        payout = await _mk_payout(db, status="sending", attempts=2)
        try:
            assert await ton_pay.dispatch_pending_payouts() >= 1
            await db.refresh(payout)
            assert payout.status == "sent" and payout.tx_hash == "tx-recovered"
        finally:
            await db.delete(payout)
            await db.commit()


async def test_admin_alerted_once_per_payout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "admin_ids", "42")  # admin_id_set — property
    bot = SimpleNamespace(send_message=AsyncMock())
    async with SessionLocal() as db:
        payout = await _mk_payout(db, status="failed", attempts=99)
        try:
            assert await ton_pay.dispatch_pending_payouts(bot=bot) == 0
            await db.refresh(payout)
            assert payout.status == "failed"
            # Дедуп алертов живёт в БД: флаг выставлен вместе с отправкой.
            assert payout.alerted is True
            assert bot.send_message.await_count == 1
            # Повторный цикл по той же выплате алерт не дублирует.
            assert await ton_pay.dispatch_pending_payouts(bot=bot) == 0
            assert bot.send_message.await_count == 1
        finally:
            await db.delete(payout)
            await db.commit()


# ---------- Курсор watcher'а ----------


async def test_watch_once_advances_cursor_and_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import ton_watch

    monkeypatch.setattr(settings, "ton_enabled", True)
    transfers = [
        ton_watch.Transfer(f"c-{i}", "0:" + os.urandom(32).hex(), to_nano(0.2), "", 1000 + i)
        for i in range(3)
    ]
    monkeypatch.setattr(
        ton_watch, "fetch_recent_transfers", AsyncMock(return_value=(transfers, True))
    )

    # Стартовый курсор раньше всех транзакций (фолбэк «now−12ч» их новее).
    async with SessionLocal() as db:
        db.add(WatcherState(key=ton_watch.CURSOR_KEY, value="999"))
        await db.commit()

    await ton_watch.watch_once()
    async with SessionLocal() as db:
        cursor_row = await db.get(WatcherState, ton_watch.CURSOR_KEY)
        assert cursor_row is not None and int(cursor_row.value) == 1002
        refunds = (
            await db.execute(
                select(Payout).where(Payout.kind == "refund", Payout.tx_hash.in_([t.tx_hash for t in transfers]))
            )
        ).scalars().all()
        assert len(refunds) == 3

    # Повторный прогон по тем же транзакциям: возвраты не задваиваются.
    await ton_watch.watch_once()
    async with SessionLocal() as db:
        refunds = (
            await db.execute(
                select(Payout).where(Payout.kind == "refund", Payout.tx_hash.in_([t.tx_hash for t in transfers]))
            )
        ).scalars().all()
        assert len(refunds) == 3
        await db.execute(WatcherState.__table__.delete().where(WatcherState.key == ton_watch.CURSOR_KEY))
        await db.execute(
            Payout.__table__.delete().where(Payout.kind == "refund").where(Payout.player_id.is_(None))
        )
        await db.commit()


async def test_watch_cursor_stops_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import ton_watch

    monkeypatch.setattr(settings, "ton_enabled", True)
    bad = ton_watch.Transfer("bad-1", "0:" + os.urandom(32).hex(), to_nano(0.2), "", 500)
    good = ton_watch.Transfer("good-1", "0:" + os.urandom(32).hex(), to_nano(0.2), "", 501)
    monkeypatch.setattr(
        ton_watch,
        "fetch_recent_transfers",
        AsyncMock(return_value=([good, bad], True)),
    )  # API отдаёт новые сверху; watch обрабатывает по возрастанию

    async def exploding(transfer):
        if transfer.tx_hash == "bad-1":
            raise RuntimeError("цепь моргнула")
        return "refund_queued"

    monkeypatch.setattr(ton_watch, "process_transfer", exploding)
    try:
        await ton_watch.watch_once()
        async with SessionLocal() as db:
            row = await db.get(WatcherState, ton_watch.CURSOR_KEY)
            assert row is None or int(row.value) < 500  # курсор застрял до сбойного перевода
    finally:
        async with SessionLocal() as db:
            await db.execute(WatcherState.__table__.delete().where(WatcherState.key == ton_watch.CURSOR_KEY))
            await db.commit()


async def test_watch_beats_on_quiet_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Тишина в цепочке — здоровье: сердцебиение ставится и без переводов.

    Именно этот кейс раньше порождал ложный алерт «watcher ещё ни разу не
    отмечал курсор»: курсор двигался только переводами.
    """
    from app import ops
    from app import ton_watch

    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(
        ton_watch, "fetch_recent_transfers", AsyncMock(return_value=([], True))
    )
    try:
        await ton_watch.watch_once()
        async with SessionLocal() as db:
            beat = await db.get(WatcherState, ton_watch.BEAT_KEY)
            assert beat is not None  # цикл прошёл успешно — сердце бьётся
            assert await db.get(WatcherState, ton_watch.CURSOR_KEY) is None
        # Свежее сердцебиение — аномалий нет, алертов нет.
        assert await ops.check_anomalies(bot=None) == []
    finally:
        async with SessionLocal() as db:
            await db.execute(
                WatcherState.__table__.delete().where(
                    WatcherState.key.in_([ton_watch.BEAT_KEY, ton_watch.CURSOR_KEY])
                )
            )
            await db.execute(
                WatcherState.__table__.delete().where(
                    WatcherState.key.in_([ops.ALERT_WATCHER_KEY])
                )
            )
            await db.commit()


async def test_api_outage_does_not_beat_and_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    """TonAPI лежит весь цикл — сердцебиения нет, админ узнает об этом."""
    from app import ops
    from app import ton_watch

    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "admin_ids", "42")
    bot = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr(
        ton_watch, "fetch_recent_transfers", AsyncMock(return_value=([], False))
    )
    try:
        await ton_watch.watch_once()
        async with SessionLocal() as db:
            assert await db.get(WatcherState, ton_watch.BEAT_KEY) is None
        problems = await ops.check_anomalies(bot=bot)
        assert problems and "цикл" in problems[0]
        assert bot.send_message.await_count == 1
    finally:
        async with SessionLocal() as db:
            await db.execute(
                WatcherState.__table__.delete().where(
                    WatcherState.key.in_([ton_watch.BEAT_KEY, ops.ALERT_WATCHER_KEY])
                )
            )
            await db.commit()


async def test_no_watcher_alerts_when_ton_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import ops

    monkeypatch.setattr(settings, "ton_enabled", False)
    assert await ops.check_anomalies(bot=None) == []


# ---------- Токены (джеттоны) против фонда ----------


def _tx_item(**overrides) -> dict:
    item = {
        "hash": "tx-" + os.urandom(8).hex(),
        "utime": 5000,
        "in_msg": {
            "source": {"address": "0:" + os.urandom(32).hex()},
            "value": str(to_nano(0.5)),
            "raw_message": "",
        },
    }
    item.update(overrides)
    return item


def test_native_ton_transaction_parses() -> None:
    from app import ton_watch

    transfer = ton_watch._parse_tx_item(_tx_item(), since_utime=1000)
    assert transfer is not None
    assert transfer.value_nanotons == to_nano(0.5)


def test_jetton_transfer_is_not_a_stake_and_not_refunded() -> None:
    """USDt/др. токены не смешиваются с фондом: уведомление джеттона
    пропускается целиком — ни ставки, ни пыльного авто-возврата."""
    from app import ton_watch

    jetton = _tx_item(
        in_msg={
            "source": {"address": "EQ" + "A" * 46},  # jetton-кошелёк отправителя
            "value": "1000000",  # копейки газа в обёртке, не сумма перевода
            "opcode": "0x7362d09c",
        }
    )
    assert ton_watch._is_jetton_notification(jetton["in_msg"])
    assert ton_watch._parse_tx_item(jetton, since_utime=1000) is None

    # Альтернативная форма сигнала от API — decoded_op.
    decoded = _tx_item(in_msg={"msg_data": {"decoded_op": "transfer_notification"}})
    assert ton_watch._is_jetton_notification(decoded["in_msg"])
    assert ton_watch._parse_tx_item(decoded, since_utime=1000) is None

    # Нативный TON с обычным опкодом джеттоном не считается.
    native = _tx_item(in_msg={**_tx_item()["in_msg"], "opcode": "0x00000000"})
    assert not ton_watch._is_jetton_notification(native["in_msg"])


def test_old_or_empty_transactions_are_skipped() -> None:
    from app import ton_watch

    assert ton_watch._parse_tx_item(_tx_item(utime=999), since_utime=1000) is None
    empty = _tx_item()
    empty["in_msg"]["value"] = "0"
    assert ton_watch._parse_tx_item(empty, since_utime=1000) is None


def test_previous_month_key() -> None:
    assert previous_month_key(datetime(2026, 8, 22, tzinfo=timezone.utc)) == "2026-07"
    assert previous_month_key(datetime(2026, 1, 5, tzinfo=timezone.utc)) == "2025-12"


# ---------- Копилка месяца ----------


async def _seed_leaderboard_month(session: AsyncSession) -> tuple[int, dict]:
    """Два закрытых дня прошлого месяца; p1 угадал дважды, p2 один раз."""
    pid1 = 910_000 + int.from_bytes(os.urandom(2), "big")
    pid2 = pid1 + 1
    wallet1 = "0:" + os.urandom(32).hex()
    session.add_all(
        [
            Player(id=pid1, username=f"u{pid1}", wallet_address=wallet1),
            Player(id=pid2, username=f"u{pid2}"),
        ]
    )
    prev_month = datetime.now(timezone.utc).replace(day=1) - timedelta(days=5)
    round_a = _closed_round(700_001, prev_month - timedelta(days=10))
    round_b = _closed_round(700_002, prev_month - timedelta(days=9))
    session.add_all([round_a, round_b])
    await session.flush()
    session.add_all(
        [
            Vote(round_id=round_a.id, player_id=pid1, card_position=1),
            Vote(round_id=round_b.id, player_id=pid1, card_position=1),
            Vote(round_id=round_b.id, player_id=pid2, card_position=2),
        ]
    )
    pot_old = LeaderboardPot(month=(prev_month - timedelta(days=40)).strftime("%Y-%m"), nanotons=300)
    pot_prev = LeaderboardPot(month=prev_month.strftime("%Y-%m"), nanotons=to_nano(1.7))
    session.add_all([pot_old, pot_prev])
    await session.commit()
    total = pot_old.nanotons + pot_prev.nanotons
    return total, {"p1": pid1, "p2": pid2, "w1": wallet1}


async def test_monthly_pot_paid_to_leader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    monkeypatch.setattr(settings, "admin_ids", "42")
    bot = SimpleNamespace(send_message=AsyncMock())
    async with SessionLocal() as session:
        total, ids = await _seed_leaderboard_month(session)
        pots = (await session.execute(select(LeaderboardPot))).scalars().all()
        months = [p.month for p in pots]
        try:
            assert await settle_month_if_due(bot=bot) is True
            payout = (
                await session.execute(
                    select(Payout).where(Payout.kind == "leaderboard", Payout.player_id == ids["p1"])
                )
            ).scalar_one()
            assert payout.amount_nanotons == total
            assert payout.dest_address == ids["w1"]
            assert payout.round_id is None
            assert (await session.execute(select(LeaderboardPot))).all() == []
            marker = await session.get(WatcherState, MARKER_KEY)
            assert marker is not None and marker.value == previous_month_key()
            assert bot.send_message.await_count == 1

            # Идемпотентность: в том же месяце второй раз не платим.
            assert await settle_month_if_due(bot=bot) is False
            assert bot.send_message.await_count == 1
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "leaderboard"))
            await session.execute(LeaderboardPot.__table__.delete().where(LeaderboardPot.month.in_(months)))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == MARKER_KEY))
            await session.execute(Vote.__table__.delete().where(Vote.player_id.in_([ids["p1"], ids["p2"]])))
            rounds = (
                (await session.execute(select(Round).where(Round.day_index.in_([700_001, 700_002])))).scalars().all()
            )
            for round_row in rounds:
                await session.delete(round_row)
            for pid in (ids["p1"], ids["p2"]):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_monthly_pot_split_between_tied_leaders(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ничья: горш делится поровну между лидерами с кошельками, пыль — меньшему id."""
    monkeypatch.setattr(settings, "ton_enabled", True)
    pid_a = 930_000 + int.from_bytes(os.urandom(2), "big")
    pid_b = pid_a + 1
    wallet_a = "0:" + os.urandom(32).hex()
    wallet_b = "0:" + os.urandom(32).hex()
    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pid_a, username=f"u{pid_a}", wallet_address=wallet_a),
                Player(id=pid_b, username=f"u{pid_b}", wallet_address=wallet_b),
            ]
        )
        prev_month = datetime.now(timezone.utc).replace(day=1) - timedelta(days=5)
        round_row = _closed_round(702_001, prev_month - timedelta(days=2))
        session.add(round_row)
        await session.flush()
        session.add_all(
            [
                Vote(round_id=round_row.id, player_id=pid_a, card_position=1),
                Vote(round_id=round_row.id, player_id=pid_b, card_position=1),
            ]
        )
        pot = LeaderboardPot(month=prev_month.strftime("%Y-%m"), nanotons=to_nano(1))
        month_key = pot.month
        session.add(pot)
        await session.commit()
        try:
            assert await settle_month_if_due(bot=None) is True
            rows = (
                (
                    await session.execute(
                        select(Payout).where(Payout.kind == "leaderboard").order_by(Payout.player_id.asc())
                    )
                )
                .scalars()
                .all()
            )
            assert [(p.player_id, p.dest_address) for p in rows] == [
                (pid_a, wallet_a),
                (pid_b, wallet_b),
            ]
            assert sum(p.amount_nanotons for p in rows) == to_nano(1)
            # Пыль от нечётного дележа — меньшему player_id.
            assert rows[0].amount_nanotons - rows[1].amount_nanotons <= 1
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "leaderboard"))
            await session.execute(LeaderboardPot.__table__.delete().where(LeaderboardPot.month == month_key))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == MARKER_KEY))
            await session.execute(Vote.__table__.delete().where(Vote.player_id.in_([pid_a, pid_b])))
            await session.delete(round_row)
            for pid in (pid_a, pid_b):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


async def test_monthly_pot_carried_when_leader_has_no_wallet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ton_enabled", True)
    async with SessionLocal() as session:
        # Только игрок без кошелька угадывает.
        pid = 920_000 + int.from_bytes(os.urandom(2), "big")
        session.add(Player(id=pid, username=f"u{pid}"))
        prev_month = datetime.now(timezone.utc).replace(day=1) - timedelta(days=5)
        round_row = _closed_round(701_001, prev_month - timedelta(days=3))
        session.add(round_row)
        await session.flush()
        session.add(Vote(round_id=round_row.id, player_id=pid, card_position=1))
        pot = LeaderboardPot(month=prev_month.strftime("%Y-%m"), nanotons=to_nano(2))
        session.add(pot)
        month_key = pot.month
        await session.commit()
        try:
            assert await settle_month_if_due(bot=None) is False
            assert (await session.scalar(select(LeaderboardPot.nanotons).where(LeaderboardPot.month == month_key))) == to_nano(2)
            assert await session.get(WatcherState, MARKER_KEY) is None
        finally:
            await session.execute(LeaderboardPot.__table__.delete().where(LeaderboardPot.month == month_key))
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == MARKER_KEY))
            await session.delete(round_row)
            await session.execute(Vote.__table__.delete().where(Vote.player_id == pid))
            player = await session.get(Player, pid)
            if player is not None:
                await session.delete(player)
            await session.commit()


async def test_monthly_pot_ignores_already_settled_months(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Чемпион позапрошлого (уже выплаченного) месяца не забирает новый горш.

    Окно лидерборда — от начала самого старого невыплатенного месяца до
    начала текущего; голоса за его пределами не считаются.
    """
    monkeypatch.setattr(settings, "ton_enabled", True)
    pid_champ = 940_000 + int.from_bytes(os.urandom(2), "big")
    pid_new = pid_champ + 1
    wallet_new = "0:" + os.urandom(32).hex()
    now = datetime.now(timezone.utc)
    prev_first = (now.replace(day=1) - timedelta(days=1)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    two_ago_key = (prev_first - timedelta(days=1)).strftime("%Y-%m")
    async with SessionLocal() as session:
        session.add_all(
            [
                Player(id=pid_champ, username=f"u{pid_champ}"),
                Player(id=pid_new, username=f"u{pid_new}", wallet_address=wallet_new),
            ]
        )
        # Чемпион: 2 верных в позапрошлом месяце (вне окна выплат).
        champ_round_a = _closed_round(703_001, prev_first - timedelta(days=10))
        champ_round_b = _closed_round(703_003, prev_first - timedelta(days=5))
        # Новичок: 1 верный в прошлом месяце (внутри окна).
        new_round = _closed_round(703_002, prev_first + timedelta(days=5))
        session.add_all([champ_round_a, champ_round_b, new_round])
        await session.flush()
        session.add_all(
            [
                Vote(round_id=champ_round_a.id, player_id=pid_champ, card_position=1),
                Vote(round_id=champ_round_b.id, player_id=pid_champ, card_position=1),
                Vote(round_id=new_round.id, player_id=pid_new, card_position=1),
            ]
        )
        pot = LeaderboardPot(month=prev_first.strftime("%Y-%m"), nanotons=to_nano(1))
        month_key = pot.month
        session.add(pot)
        await session.commit()
        try:
            assert await settle_month_if_due(bot=None) is True
            rows = (
                (
                    await session.execute(
                        select(Payout).where(Payout.kind == "leaderboard", Payout.round_id.is_(None))
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].player_id == pid_new
            assert rows[0].dest_address == wallet_new
        finally:
            await session.execute(Payout.__table__.delete().where(Payout.kind == "leaderboard"))
            await session.execute(
                LeaderboardPot.__table__.delete().where(LeaderboardPot.month.in_([month_key, two_ago_key]))
            )
            await session.execute(WatcherState.__table__.delete().where(WatcherState.key == MARKER_KEY))
            await session.execute(Vote.__table__.delete().where(Vote.player_id.in_([pid_champ, pid_new])))
            for round_row in (champ_round_a, champ_round_b, new_round):
                await session.delete(round_row)
            for pid in (pid_champ, pid_new):
                player = await session.get(Player, pid)
                if player is not None:
                    await session.delete(player)
            await session.commit()


# ---------- Стоп-фильтр генераций ----------


def test_text_filter_flags_profanity(monkeypatch: pytest.MonkeyPatch) -> None:
    assert text_is_clean("Пёс ведёт стаю по пеплу.")
    assert not text_is_clean("какая-то хуйня на дороге")
    assert not text_is_clean("FUCK the road")
    monkeypatch.setattr(settings, "content_filter", False)
    assert text_is_clean("какая-то хуйня на дороге")


def _chapter_payload(description: str) -> dict:
    content = (
        '{"title":"День 1. Развилка","text":"Стая идёт по тракту.","lore_summary":"пепел",'
        '"cards":[{"title":"А","description":"' + description + '","consequence":"эхо","tag":"risk"},'
        '{"title":"Б","description":"бережно","consequence":"тепло","tag":"care"},'
        '{"title":"В","description":"обход","consequence":"тишина","tag":"cunning"}]}'
    )
    return {"choices": [{"message": {"content": content}}]}


def test_chapter_with_profanity_is_rejected() -> None:
    assert _parse_chapter(_chapter_payload("пиздецовая тропа"), day_index=1) is None
    parsed = _parse_chapter(_chapter_payload("узкая тропа"), day_index=1)
    assert parsed is not None and len(parsed["cards"]) == 3


async def test_epilogue_with_profanity_falls_back_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"choices": [{"message": {"content": "Стая решила и всё охуело."}}]}

    async def fake_completion(messages, timeout=45):
        return payload, "test-model"

    monkeypatch.setattr("app.story._chat_completion", fake_completion)
    assert await generate_epilogue(1, "Тропа", "дорога заросла", "1/2/3", "закон") == ""
