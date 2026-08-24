"""Автоматические выплаты копилок лидерборда: недели и месяца.

**Неделя.** Каждый день 2% фонда капает в копилку недели (WeeklyPot). В
понедельник сумма уходит топ-3 прошедшей недели по числу верных ответов:
места делят приз по WEEKLY_PRIZE_PCTS (по умолчанию 20/30/50%). Претендент
обязан иметь привязанный кошелёк и минимум WEEKLY_MIN_DAYS дней голосования
за неделю — иначе фермы мультиаккаунтов собирают приз дешёвыми голосами.
Ничьи разбиваются детерминированно: верные пути ↓, дни участия ↓, id ↑.
Доля места без достойного игрока переносится в копилку новой недели.

**Месяц.** 1-го числа сумма всех накопленных месяцев до текущего уходит
игроку или игрокам с максимумом верных ответов за тот период. Ничья —
сумма делится поровну. Лидер без привязанного кошелька не блокирует
остальных: горш делится между оплачиваемыми, а месяц остаётся «незакрытым»,
пока хоть кому-то нельзя заплатить и никого оплатить нельзя вовсе.

Метки «выплачено до X» живут в watcher_state и переживают рестарт.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from sqlalchemy import case, delete, func, or_, select

from app.config import settings
from app.db import SessionLocal
from app.models import LeaderboardPot, Payout, Player, Round, RoundStatus, Vote, WatcherState, WeeklyPot
from app.stakes import split_equal
from app.weeks import iso_week_key, parse_prize_pcts, previous_week_key, week_bounds

logger = logging.getLogger(__name__)

MARKER_KEY = "leaderboard_settled_through"
WEEKLY_MARKER_KEY = "weekly_settled_through"

_MEDALS = ("🥇", "🥈", "🥉")


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


async def weekly_top(
    session,
    since: datetime,
    until: datetime,
    limit: int = 10,
) -> list[tuple[int, int, int]]:
    """Топ недели окном [since, until): (player_id, верных путей, дней голосования).

    Неделя дня считается по моменту ОТКРЫТИЯ дня — так же, как копилка
    недели в stakes.finalize_day_payouts, иначе последний день недели
    платил бы в горш, но оставался бы вне зачёта. Порядок детерминированный:
    верные пути ↓, дни участия ↓ (ничья в пользу более постоянного игрока),
    player_id ↑.
    """
    result = await session.execute(
        select(
            Vote.player_id,
            func.sum(case((Vote.card_position == Round.winner_card, 1), else_=0)),
            func.count(),
        )
        .join(Round, Round.id == Vote.round_id)
        .where(
            Round.status == RoundStatus.CLOSED,
            Round.opens_at >= since,
            Round.opens_at < until,
        )
        .group_by(Vote.player_id)
        .order_by(
            func.sum(case((Vote.card_position == Round.winner_card, 1), else_=0)).desc(),
            func.count().desc(),
            Vote.player_id.asc(),
        )
        .limit(limit)
    )
    return [(pid, int(correct or 0), int(days)) for pid, correct, days in result.all()]


def _week_prize_amounts(total_nanotons: int, places: int) -> tuple[list[int], int]:
    """Доли мест по WEEKLY_PRIZE_PCTS.

    Возвращает (оплачиваемые суммы, перенос в копилку новой недели).
    Целочисленная пыль достаётся последнему оплачиваемому месту; если
    достойных игроков меньше настроенных мест, лишние доли переносятся.
    """
    pcts = parse_prize_pcts(settings.weekly_prize_pcts)
    amounts = [total_nanotons * pct // 100 for pct in pcts]
    dust = total_nanotons - sum(amounts)
    paid = max(0, min(places, len(amounts)))
    if dust > 0 and paid > 0:
        amounts[paid - 1] += dust
        dust = 0
    return amounts[:paid], sum(amounts[paid:]) + dust


async def settle_week_if_due(bot: Bot | None = None) -> bool:
    """Выплачивает копилку прошедших недель топ-3, если началась новая неделя.

    Возвращает True, если выплата создана. Идемпотентно по метке в
    watcher_state; платить некому — метка не двигается, копилка ждёт.
    """
    now = datetime.now(timezone.utc)
    prev_key = previous_week_key(now)

    async with SessionLocal() as session:
        marker = await session.get(WatcherState, WEEKLY_MARKER_KEY)
        settled_through = marker.value if marker is not None else ""
        if settled_through >= prev_key:
            return False

        pots = (
            await session.execute(
                select(WeeklyPot).where(WeeklyPot.week <= prev_key).order_by(WeeklyPot.week.asc())
            )
        ).scalars().all()
        if not pots:
            if marker is None:
                session.add(WatcherState(key=WEEKLY_MARKER_KEY, value=prev_key))
            else:
                marker.value = prev_key
            await session.commit()
            return False
        total = sum(pot.nanotons for pot in pots)
        period_start = week_bounds(pots[0].week)[0]
        period_end = week_bounds(prev_key)[1]

        # Неделя долита не полностью (последний день ещё считается или его
        # выплаты не финализированы) — платить рано, метку не двигаем.
        unfinished = await session.execute(
            select(Round.id)
            .where(
                Round.opens_at >= period_start,
                Round.opens_at < period_end,
                or_(
                    Round.status != RoundStatus.CLOSED,
                    Round.payouts_finalized.is_(False),
                ),
            )
            .limit(1)
        )
        if unfinished.scalar_one_or_none() is not None:
            return False

        min_days = max(1, settings.weekly_min_days)
        rows = await weekly_top(session, period_start, period_end, limit=50)
        eligible: list[tuple[int, int, str]] = []
        for pid, correct, days in rows:
            if correct <= 0 or days < min_days:
                continue
            player = await session.get(Player, pid)
            if player is not None and player.wallet_address:
                eligible.append((pid, correct, player.wallet_address))
            if len(eligible) >= 3:
                break

        if not eligible:
            # Достойных нет: метку НЕ двигаем, копилка ждёт следующей недели.
            logger.warning(
                "Копилка недели %d нанотонов ждёт: нет игроков с кошельком и %d+ днями голосования",
                total,
                min_days,
            )
            return False

        amounts, rolled = _week_prize_amounts(total, len(eligible))
        network = "testnet" if settings.is_testnet else "mainnet"
        paid: list[tuple[str, str, int]] = []
        for place, ((pid, _correct, wallet), amount) in enumerate(zip(eligible, amounts), 1):
            session.add(
                Payout(
                    round_id=None,
                    player_id=pid,
                    kind="weekly",
                    amount_nanotons=amount,
                    dest_address=wallet,
                    network=network,
                )
            )
            name_row = await session.get(Player, pid)
            name = (
                (name_row.username if name_row and name_row.username else None)
                or (name_row.first_name if name_row else None)
                or f"игрок {pid}"
            )
            paid.append((_MEDALS[place - 1], name, amount))

        # Места без достойного игрока переносятся в копилку новой недели.
        if rolled > 0:
            current_week = iso_week_key(now)
            week_row = (
                await session.execute(select(WeeklyPot).where(WeeklyPot.week == current_week))
            ).scalar_one_or_none()
            if week_row is None:
                session.add(WeeklyPot(week=current_week, nanotons=rolled))
            else:
                week_row.nanotons += rolled

        await session.execute(delete(WeeklyPot).where(WeeklyPot.week <= prev_key))
        if marker is None:
            session.add(WatcherState(key=WEEKLY_MARKER_KEY, value=prev_key))
        else:
            marker.value = prev_key
        await session.commit()

    logger.info(
        "Копилка недель ≤ %s выплачена: %s нанотонов → %s",
        prev_key,
        total,
        [(medal, name, amount) for medal, name, amount in paid],
    )
    lines = ["🏆 Итоги недели Стаи:"]
    for medal, name, amount in paid:
        lines.append(f"{medal} {name} — {amount / 1e9:.4f} Gram")
    if rolled > 0:
        lines.append("Незаполненные места недели перешли в копилку новой.")
    lines.append("Неделя началась заново — копилка копится с нуля. Удачи!")
    ceremony = "\n".join(lines)
    if bot is not None:
        from app.broadcast import whisper_to_chats

        try:
            await whisper_to_chats(bot, ceremony)
        except Exception as exc:
            logger.warning("Церемония недели не разослана: %s", exc)
    if bot is not None and settings.admin_id_set:
        admin_text = f"🗓 Копилка недели {prev_key} отправлена: {total / 1e9:.4f} Gram."
        for admin_id in settings.admin_id_set:
            try:
                await bot.send_message(admin_id, admin_text)
            except Exception as exc:
                logger.warning("Алерт админу %s не доставлен: %s", admin_id, exc)
    return True
