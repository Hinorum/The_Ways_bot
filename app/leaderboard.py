"""Автоматические выплаты копилок лидерборда: недели и месяца.

**Неделя.** Каждый день 2% фонда капает в копилку недели (WeeklyPot). В
понедельник приз уходит трём верхним СТУПЕНЯМ счёта прошедшей недели —
уровням верных ответов (7-6-5; без шестёрок — 7-5-4 и т.п.). Долю ступени
делят между собой все её члены поровну: ступень с тремя игроками платит
каждому треть суммы. Ступени платят по WEEKLY_PRIZE_PCTS (по умолчанию
20/30/50%): нижняя ступень пьедестала забирает половину копилки — так стая
платит больше тем, кто держался сзади. Претендент обязан иметь привязанный
кошелёк и минимум WEEKLY_MIN_DAYS дней голосования за неделю — иначе фермы
мультиаккаунтов собирают приз дешёвыми голосами. Пустая ступень (некому
платить) не открывается, её доля переносится в копилку новой недели.

**Месяц.** 1-го числа сумма всех накопленных месяцев до текущего уходит
игроку или игрокам с максимумом верных ответов за тот период. Ничья —
сумма делится поровну. Лидер без привязанного кошелька не блокирует
остальных: горш делится между оплачиваемыми, а месяц остаётся «незакрытым»,
пока хоть кому-то нельзя заплатить и никого оплатить нельзя вовсе.

Метки «выплачено до X» живут в watcher_state и переживают рестарт.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

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

# Приложение живёт в одном процессе, но закрытие дня (tick) и плановый
# сброс копилок могут вызывать settle-функции параллельно из разных задач
# одного цикла событий. Без взаимной блокировки два вызова прочитали бы
# метку «ещё не закрыто», построили Пэйауты и задвоили копилку. Мьютекс
# сериализует такие перекрытия внутри процесса.

_month_settle_lock: asyncio.Lock | None = None
_week_settle_lock: asyncio.Lock | None = None


def _month_settle():
    global _month_settle_lock
    if _month_settle_lock is None:
        _month_settle_lock = asyncio.Lock()
    return _month_settle_lock


def _week_settle():
    global _week_settle_lock
    if _week_settle_lock is None:
        _week_settle_lock = asyncio.Lock()
    return _week_settle_lock

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
    """Выплачивает копилку прошедших месяцев (безопасно при параллельных вызовах)."""
    async with _month_settle():
        return await _settle_month_locked(bot)


async def _settle_month_locked(bot: Bot | None = None) -> bool:
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


async def _memory_nomination(session, period_start: datetime, period_end: datetime) -> str | None:
    """«Самый памятливый пёс недели»: максимум находок памяти в окне недели.

    Косметика церемонии: ничего не платит и не влияет на копилки.
    """
    from app.models import MemoryHit

    row = (
        await session.execute(
            select(MemoryHit.player_id, func.count())
            .join(Round, Round.id == MemoryHit.round_id)
            .where(
                Round.opens_at >= period_start,
                Round.opens_at < period_end,
            )
            .group_by(MemoryHit.player_id)
            .order_by(func.count().desc(), MemoryHit.player_id.asc())
            .limit(1)
        )
    ).first()
    if row is None or not row[1]:
        return None
    pid, count = int(row[0]), int(row[1])
    player = await session.get(Player, pid)
    name = (
        (player.username if player and player.username else None)
        or (player.first_name if player else None)
        or f"игрок {pid}"
    )
    mod10, mod100 = count % 10, count % 100
    if mod10 == 1 and mod100 != 11:
        word = "находка"
    elif mod10 in (2, 3, 4) and mod100 not in (12, 13, 14):
        word = "находки"
    else:
        word = "находок"
    return f"🧠 Самый памятливый пёс недели: {name} — {count} {word} памяти."


async def settle_week_if_due(bot: Bot | None = None) -> bool:
    """Выплачивает копилку прошедших недель (безопасно при параллельных вызовах)."""
    async with _week_settle():
        return await _settle_week_locked(bot)


async def _settle_week_locked(bot: Bot | None = None) -> bool:
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

        # Короткая первая неделя забега: если сброс случился за 1–3 дня до
        # понедельника, порог WEEKLY_MIN_DAYS снимается — новичкам честно
        # дать шанс, а копилка не должна висеть вечно.
        relaxed = False
        try:
            from app.season import RUN_START_KEY, parse_anchor

            anchor_row = await session.get(WatcherState, RUN_START_KEY)
            anchor = parse_anchor(anchor_row.value if anchor_row is not None else None)
            if anchor is not None:
                a_year, a_month = (int(part) for part in anchor["key"].split("-"))
                start_date = date(a_year, a_month, max(1, min(int(anchor["dom"]), 31)))
                if period_start <= start_date < period_end:
                    span_days = (period_end - start_date).days
                    if 1 <= span_days <= 3:
                        relaxed = True
                        logger.info(
                            "Неделя %s — короткий старт забега (%d дн.): порог дней снят",
                            prev_key, span_days,
                        )
        except Exception:
            logger.warning("Не удалось проверить короткую стартовую неделю", exc_info=True)

        min_days = 0 if relaxed else max(1, settings.weekly_min_days)
        rows = await weekly_top(session, period_start, period_end, limit=50)
        # Места недели — три верхних УРОВНЯ верных ответов, а не три игрока:
        # ступень 7-6-5 может сжаться до 7-5-3, если шестёрок и четвёрок нет.
        # Внутри ступени приз делится между всеми её членами поровну.
        candidates: list[tuple[int, int, str]] = []
        for pid, correct, days in rows:
            if correct <= 0 or days < min_days:
                continue
            player = await session.get(Player, pid)
            if player is None or not player.wallet_address:
                continue
            candidates.append((pid, correct, player.wallet_address))
        tiers: list[tuple[int, list[tuple[int, str]]]] = []
        for pid, correct, wallet in candidates:
            if len(tiers) >= 3 and correct != tiers[-1][0]:
                break
            if not tiers or correct != tiers[-1][0]:
                tiers.append((correct, []))
            tiers[-1][1].append((pid, wallet))

        if not tiers:
            # Достойных нет: метку НЕ двигаем, копилка ждёт следующей недели.
            logger.warning(
                "Копилка недели %d нанотонов ждёт: нет игроков с кошельком и %s+ днями голосования%s",
                total,
                min_days or settings.weekly_min_days,
                " (короткая стартовая неделя — порог снят)" if relaxed else "",
            )
            return False

        amounts, rolled = _week_prize_amounts(total, len(tiers))
        network = "testnet" if settings.is_testnet else "mainnet"
        paid: list[tuple[str, str, int]] = []
        for place, ((correct, members), amount) in enumerate(zip(tiers, amounts), 1):
            # Долю ступени делим между всеми её членами; нанотонная пыль —
            # первым членам ступени, чтобы сумма сходилась до нанотона.
            share = amount // len(members)
            remainder = amount - share * len(members)
            names: list[str] = []
            for index, (pid, wallet) in enumerate(members):
                session.add(
                    Payout(
                        round_id=None,
                        player_id=pid,
                        kind="weekly",
                        amount_nanotons=share + (1 if index < remainder else 0),
                        dest_address=wallet,
                        network=network,
                    )
                )
                name_row = await session.get(Player, pid)
                names.append(
                    (name_row.username if name_row and name_row.username else None)
                    or (name_row.first_name if name_row else None)
                    or f"игрок {pid}"
                )
            paid.append((_MEDALS[place - 1], ", ".join(names) + f" — {correct} верн.", amount))

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
    lines.append("Стая платит больше тем, кто держался сзади: чем ниже ступень — тем больше её доля.")
    try:
        nomination = await _memory_nomination(session, period_start, period_end)
    except Exception:
        nomination = None
    if nomination:
        lines.append(nomination)
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
