"""Автоматическая выплата копилки месяца лидерам лидерборда.

1-го числа каждого месяца (проверяется при каждом тон-цикле обслуживания)
сумма всех накопленных месяцев до текущего уходит игроку или игрокам с
максимумом верных ответов за тот период. Ничья — сумма делится поровну.
Лидер без привязанного кошелька не блокирует остальных: горш делится между
оплачиваемыми, а месяц не закрывается, пока хоть кому-то нельзя заплатить и
никого оплатить нельзя вовсе — копилка остаётся ждать следующего цикла.

Метка «выплачено до месяца X» живёт в watcher_state и переживает рестарт.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from sqlalchemy import delete, func, select

from app.config import settings
from app.db import SessionLocal
from app.models import LeaderboardPot, Payout, Player, Round, RoundStatus, Vote, WatcherState
from app.stakes import current_network, split_equal

logger = logging.getLogger(__name__)

MARKER_KEY = "leaderboard_settled_through"


def previous_month_key(now: datetime | None = None) -> str:
    """Ключ последнего ПОЛНОСТЬЮ прошедшего месяца («YYYY-MM»)."""
    now = now or datetime.now(timezone.utc)
    first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first_of_month - timedelta(days=1)).strftime("%Y-%m")


async def _top_correct_voters(session, since: datetime, until: datetime) -> list[tuple[int, int]]:
    """Игроки с максимумом верных ответов в окне [since, until).

    Нижняя граница обязательна: без неё «чемпион всех времён» забирал бы
    копилку каждого следующего месяца, даже ничего не отгадав в нём.
    """
    result = await session.execute(
        select(Vote.player_id, func.count())
        .join(Round, Round.id == Vote.round_id)
        .where(
            Vote.card_position == Round.winner_card,
            Round.status == RoundStatus.CLOSED,
            Round.tally_ends_at >= since,
            Round.tally_ends_at < until,
        )
        .group_by(Vote.player_id)
        .order_by(func.count().desc(), Vote.player_id.asc())
    )
    rows = [(pid, count) for pid, count in result.all()]
    if not rows:
        return []
    best = rows[0][1]
    return [row for row in rows if row[1] == best]


async def settle_month_if_due(bot: Bot | None = None) -> bool:
    """Выплачивает копилку прошедших месяцев, если наступил новый месяц.

    Возвращает True, если выплата создана. Идемпотентно по метке в watcher_state:
    повторные вызовы в том же месяце ничего не делают.
    """
    now = datetime.now(timezone.utc)
    prev_key = previous_month_key(now)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    async with SessionLocal() as session:
        marker = await session.get(WatcherState, MARKER_KEY)
        settled_through = marker.value if marker is not None else ""
        if settled_through >= prev_key:
            return False  # этот месяц уже закрыт (или вообще ещё не наступил)

        pots = (
            await session.execute(
                select(LeaderboardPot).where(LeaderboardPot.month <= prev_key).order_by(LeaderboardPot.month.asc())
            )
        ).scalars().all()
        total = sum(pot.nanotons for pot in pots)
        # Период лидерборда: от начала самого старого НЕвыплаченного месяца
        # (после простоя копилка копится за несколько месяцев сразу) до начала
        # текущего — ровно то, за что платим.
        period_start = month_start
        if pots:
            year, mon = map(int, pots[0].month.split("-"))
            period_start = datetime(year, mon, 1, tzinfo=timezone.utc)
        winners = await _top_correct_voters(session, period_start, month_start)

        payable_ids: list[int] = []
        wallets: dict[int, str] = {}
        for player_id, _count in winners:
            player = await session.get(Player, player_id)
            if player is not None and player.wallet_address:
                wallets[player_id] = player.wallet_address
                payable_ids.append(player_id)

        skipped = len(winners) - len(payable_ids)
        if not payable_ids:
            # Платить некому: метку НЕ двигаем, копилка ждёт следующего цикла.
            logger.warning(
                "Копилка %d нанотонов ждёт: у лидеров (%s) нет привязанного кошелька",
                total,
                [pid for pid, _ in winners] or "нет голосов",
            )
            return False

        shares = split_equal(total, payable_ids)
        network = "testnet" if settings.is_testnet else "mainnet"
        for player_id, amount in shares.items():
            session.add(
                Payout(
                    round_id=None,
                    player_id=player_id,
                    kind="leaderboard",
                    amount_nanotons=amount,
                    dest_address=wallets[player_id],
                    network=network,
                )
            )
        await session.execute(
            delete(LeaderboardPot).where(LeaderboardPot.month <= prev_key)
        )
        if marker is None:
            session.add(WatcherState(key=MARKER_KEY, value=prev_key))
        else:
            marker.value = prev_key
        await session.commit()

    logger.info(
        "Копилка месяцев ≤ %s выплачена: %s нанотонов между %s%s",
        prev_key,
        total,
        shares,
        f" (без кошелька пропущены: {skipped})" if skipped else "",
    )
    if bot is not None and settings.admin_id_set:
        text = (
            f"🏆 Копилка лидерборда за {prev_key} отправлена: "
            f"{total / 1e9:.4f} Gram между {len(shares)} лидером(ами)."
        )
        for admin_id in settings.admin_id_set:
            try:
                await bot.send_message(admin_id, text)
            except Exception as exc:
                logger.warning("Алерт админу %s не доставлен: %s", admin_id, exc)
    return True
