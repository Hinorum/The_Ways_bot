"""Автоматические выплаты копилок лидерборда: недели и месяца.

**Неделя.** Каждый день 2% фонда капает в копилку недели (WeeklyPot). В
понедельник приз уходит трём сильнейшим игрокам прошедшей недели — доли по
WEEKLY_PRIZE_PCTS (по умолчанию 50/30/20%: первое место забирает половину).
Выплата возможна ТОЛЬКО после записи эпилога последнего дня недели (флаг
week_leaderboard_ready в watcher_state). Претендент обязан иметь привязанный
кошелёк, минимум WEEKLY_MIN_DAYS дней голосования за неделю и хотя бы одну
ставку в этой неделе — иначе фермы мультиаккаунтов собирают приз дешёвыми
голосами. Ставка-требование считается заново каждую неделю: приз не уйдёт
тому, кто в прошедшей неделе не поставил ни грамма. Ничья по верным путям
решается большим вкладом Gram за период, при равенстве ставок — кто раньше
нажал Claim в /start, далее — меньший player_id. Месту, которому не нашлось
достойного игрока, ждать нечего: его доля переносится в копилку новой недели.

**Месяц.** Выплата происходит ТОЛЬКО после записи эпилога последнего дня
месяца (флаг month_leaderboard_ready в watcher_state). Это гарантирует,
что лидерборд не сработает до завершения нарративной части дня. Сумма всех
накопленных месяцев до текущего уходит топ-K игрокам периода (обязательна
хотя бы одна ставка в этом месяце) по весам MONTHLY_PRIZE_WEIGHTS (по
умолчанию top-3, 50/30/20). Ничьи решаются в точности как в неделю: больший
вклад Gram, затем первый Claim, затем меньший player_id. Лидер без
привязанного кошелька или без ставки в периоде не блокирует остальных: горш
делится между оплачиваемыми, а месяц остаётся «незакрытым», пока хоть
кому-то нельзя заплатить и никого оплатить нельзя вовсе.

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
from app.models import (
    LeaderboardClaim,
    LeaderboardPot,
    Payout,
    Player,
    Round,
    RoundStatus,
    Stake,
    Vote,
    WatcherState,
    WeeklyPot,
)
from app.stakes import split_equal
from app.weeks import iso_week_key, parse_prize_pcts, previous_week_key, week_bounds

logger = logging.getLogger(__name__)

MARKER_KEY = "leaderboard_settled_through"
WEEKLY_MARKER_KEY = "weekly_settled_through"
MONTH_READY_KEY = "month_leaderboard_ready"
WEEK_READY_KEY = "week_leaderboard_ready"

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


def _capped(expr, cap: int):
    """Верное-количество, обрезанное потолком, портативно (SQLite/Postgres)."""
    if not cap or cap <= 0:
        return expr
    return case((expr > cap, cap), else_=expr)


def previous_month_key(now: datetime | None = None) -> str:
    """Ключ последнего ПОЛНОСТЬЮ прошедшего месяца («YYYY-MM»)."""
    now = now or datetime.now(timezone.utc)
    first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first_of_month - timedelta(days=1)).strftime("%Y-%m")


def active_claim_period(kind: str, now: datetime | None = None) -> str:
    """Период открытой претензии Claim: неделя «YYYY-Www» или месяц «YYYY-MM».

    Кнопка в /start живёт в течение периода, в который игрок мог вложиться
    ставкой; запись фиксирует момент претензии под settle того же периода.
    """
    now = now or datetime.now(timezone.utc)
    if kind == "month":
        return now.strftime("%Y-%m")
    return iso_week_key(now)


async def _top_correct_voters(session, since: datetime, until: datetime) -> list[tuple[int, int]]:
    """Игроки с максимумом верных ответов в окне [since, until).

    Нижняя граница обязательна: без неё «чемпион всех времён» забирал бы
    копилку каждого следующего месяца, даже ничего не отгадав в нём.
    """
    count_expr = _capped(func.count(), settings.leaderboard_correct_cap)
    result = await session.execute(
        select(Vote.player_id, count_expr)
        .join(Round, Round.id == Vote.round_id)
        .where(
            Vote.card_position == Round.winner_card,
            Round.status == RoundStatus.CLOSED,
            Round.tally_ends_at >= since,
            Round.tally_ends_at < until,
        )
        .group_by(Vote.player_id)
        .order_by(count_expr.desc(), Vote.player_id.asc())
    )
    rows = [(pid, int(count)) for pid, count in result.all()]
    if not rows:
        return []
    best = rows[0][1]
    return [row for row in rows if row[1] == best]


def _active_network() -> str:
    """Контур казны: вклад Gram учитывается только в живом контуре."""
    return "testnet" if settings.is_testnet else "mainnet"


async def _gram_contributions(
    session,
    period_start: datetime,
    period_end: datetime,
    by: str = "opens_at",
) -> dict[int, int]:
    """Проверенные суммы ставок игроков за период [period_start, period_end).

    Тайбрейк при равенстве верных путей: выше тот, кто поставил больше (в
    нанотонах). Считаем confirmed и refunded — как в _players_with_stake;
    ставка другого контура (mainnet/testnet) не в зачёт.
    """
    window = Round.opens_at if by == "opens_at" else Round.tally_ends_at
    rows = await session.execute(
        select(Stake.player_id, func.sum(Stake.amount_nanotons))
        .join(Round, Round.id == Stake.round_id)
        .where(
            Stake.status.in_(["confirmed", "refunded"]),
            Stake.network == _active_network(),
            window >= period_start,
            window < period_end,
        )
        .group_by(Stake.player_id)
    )
    return {int(pid): int(total) for pid, total in rows.all()}


async def _rank_window(
    session,
    since: datetime,
    until: datetime,
    by: str = "opens_at",
    limit: int = 50,
) -> list[tuple[int, int, int, int]]:
    """Ранг окна [since, until): (player_id, верных, дней, вклад Gram).

    Порядок — ровно тот, что использует выплата: верные пути ↓, вклад Gram
    ↓, player_id ↑ (претензия Claim докладывается уже после фильтра достойных
    в _order_by_ties). Верные пути — с учётом потолка leaderboard_correct_cap.
    LIMIT применяется ПОСЛЕ пересортировки: усечение до грам-тайбрейка
    выкидывало бы претендента с большим вкладом, но большим player_id.
    """
    correct_expr = _capped(
        func.sum(case((Vote.card_position == Round.winner_card, 1), else_=0)),
        settings.leaderboard_correct_cap,
    )
    window = Round.opens_at if by == "opens_at" else Round.tally_ends_at
    result = await session.execute(
        select(
            Vote.player_id,
            correct_expr,
            func.count(),
        )
        .join(Round, Round.id == Vote.round_id)
        .where(
            Round.status == RoundStatus.CLOSED,
            window >= since,
            window < until,
        )
        .group_by(Vote.player_id)
    )
    rows = [(pid, int(correct or 0), int(days)) for pid, correct, days in result.all()]
    if not rows:
        return []
    grams = await _gram_contributions(session, since, until, by=by)
    rows.sort(key=lambda row: (-row[1], -grams.get(row[0], 0), row[0]))
    return [
        (pid, correct, days, grams.get(pid, 0)) for pid, correct, days in rows[:limit]
    ]


async def _claim_times(session, kind: str, periods: list[str]) -> dict[int, datetime]:
    """Самая ранняя претензия игрока среди contexts периода (kind-окно).

    Ничья по (верность, вклад Gram): выше тот, кто раньше нажал Claim в
    течение периода; не заявлявшийся — позади любого заявившегося.
    """
    if not periods:
        return {}
    rows = await session.execute(
        select(LeaderboardClaim.player_id, func.min(LeaderboardClaim.claimed_at))
        .where(LeaderboardClaim.kind == kind, LeaderboardClaim.period.in_(periods))
        .group_by(LeaderboardClaim.player_id)
    )
    claims: dict[int, datetime] = {}
    for pid, claimed in rows.all():
        moment = claimed if claimed.tzinfo else claimed.replace(tzinfo=timezone.utc)
        claims[int(pid)] = min(claims.get(int(pid), moment), moment)
    return claims


_FAR_FUTURE = datetime(9999, 12, 31, tzinfo=timezone.utc)


def _order_by_ties(
    candidates: list[tuple[int, int, int, str]],
    claims: dict[int, datetime],
) -> list[tuple[int, int, int, str]]:
    """Места по (верность ↓, вклад Gram ↓, Claim ↓, player_id ↑).

    candidates — (player_id, верных, вклад Gram, кошелёк). Претензия решает
    только ПОЛНУЮ ничью (равны и верность, и вклад); тот, кто не заявлялся,
    уступает любому заявившемуся; дальше — меньший player_id.
    """
    def total_key(item: tuple[int, int, int, str]):
        pid, correct, gram, _wallet = item
        claimed = claims.get(pid)
        return (-correct, -gram, claimed if claimed is not None else _FAR_FUTURE, pid)

    return sorted(candidates, key=total_key)


async def _players_with_stake(
    session,
    period_start: datetime,
    period_end: datetime,
    by: str = "opens_at",
) -> set[int]:
    """Игроки, поставившие в периоде [period_start, period_end) хотя бы одну ставку.

    Требование «приз только тем, кто ставил»: бесплатные голоса не должны
    собирать копилки. Ставка — реальный TON-обязательство стаи, момент расчёта
    статусы уже разрешены: считаем confirmed и refunded (возврат ставки — когда
    платить некому — не отменяет того факта, что игрок ставил); rejected
    (нарушители) в зачёт не идут. Окно по колонке дня должно совпадать с окном,
    в котором settle-считает голоса ("opens_at" для недели, "tally_ends_at" для
    месяца). Проверка живёт внутри каждого периода, поэтому ставку нужно делать
    заново под каждый завершившийся лидерборд.
    """
    window = Round.opens_at if by == "opens_at" else Round.tally_ends_at
    rows = await session.execute(
        select(Stake.player_id)
        .join(Round, Round.id == Stake.round_id)
        .where(
            Stake.status.in_(["confirmed", "refunded"]),
            Stake.network == _active_network(),
            window >= period_start,
            window < period_end,
        )
        .distinct()
    )
    return {int(pid) for pid in rows.scalars().all()}


def is_last_day_of_month(moment: datetime) -> bool:
    """Проверяет, является ли момент последним днём календарного месяца."""
    next_day = moment + timedelta(days=1)
    return next_day.month != moment.month


def is_last_day_of_week(moment: datetime) -> bool:
    """Проверяет, является ли момент последним днём ISO-недели (воскресенье)."""
    next_day = moment + timedelta(days=1)
    return next_day.isocalendar()[1] != moment.isocalendar()[1]


async def mark_month_leaderboard_ready(session, month_key: str) -> None:
    """Отмечает, что эпилог последнего дня месяца написан — лидерборд может выплачиваться."""
    marker = await session.get(WatcherState, MONTH_READY_KEY)
    if marker is None:
        session.add(WatcherState(key=MONTH_READY_KEY, value=month_key))
    else:
        marker.value = month_key


async def mark_week_leaderboard_ready(session, week_key: str) -> None:
    """Отмечает, что эпилог последнего дня недели написан — лидерборд может выплачиваться."""
    marker = await session.get(WatcherState, WEEK_READY_KEY)
    if marker is None:
        session.add(WatcherState(key=WEEK_READY_KEY, value=week_key))
    else:
        marker.value = week_key


async def settle_month_if_due(bot: Bot | None = None) -> bool:
    """Выплачивает копилку прошедших месяцев (безопасно при параллельных вызовах)."""
    async with _month_settle():
        return await _settle_month_locked(bot)


async def _settle_month_locked(bot: Bot | None = None) -> bool:
    """Выплачивает копилку прошедших месяцев, если эпилог последнего дня записан.

    Возвращает True, если выплата создана. Идемпотентно по метке в watcher_state:
    повторные вызовы в том же месяце ничего не делают.

    Выплата возможна ТОЛЬКО после установки флага month_leaderboard_ready,
    который ставится в _finalize_new_day_job() при записи эпилога последнего дня
    месяца. Это гарантирует, что лидерборд не сработает раньше завершения
    нарративной части дня.
    """
    now = datetime.now(timezone.utc)
    prev_key = previous_month_key(now)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    async with SessionLocal() as session:
        marker = await session.get(WatcherState, MARKER_KEY)
        settled_through = marker.value if marker is not None else ""
        if settled_through >= prev_key:
            return False  # этот месяц уже закрыт (или вообще ещё не наступил)

        # Флаг готовности: эпилог последнего дня месяца записан.
        ready_marker = await session.get(WatcherState, MONTH_READY_KEY)
        ready_month = ready_marker.value if ready_marker is not None else ""
        if ready_month < prev_key:
            # Эпилог ещё не записан — лидерборд ждёт.
            return False

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

        # Последние дни месяца ещё не финализированы (долгий тик, сбой) —
        # их вклад в копилку может быть недолит. Как в неделе: платим только
        # когда весь период закрыт и выплаты дней финализированы.
        unfinished = await session.execute(
            select(Round.id)
            .where(
                Round.tally_ends_at >= period_start,
                Round.tally_ends_at < month_start,
                or_(
                    Round.status != RoundStatus.CLOSED,
                    Round.payouts_finalized.is_(False),
                ),
            )
            .limit(1)
        )
        if unfinished.scalar_one_or_none() is not None:
            return False

        top_k = max(1, settings.monthly_prize_top_k)
        if top_k > 1:
            # Сглаживание дисперсии: платим топ-K по верности с весами,
            # а не «забрал всё сильнейший». Веса из месячного весового списка;
            # ничьи решаются вкладом Gram, затем Claim.
            ranked = await _rank_window(
                session, period_start, month_start, by="tally_ends_at", limit=top_k
            )
            staked = await _players_with_stake(
                session, period_start, month_start, by="tally_ends_at"
            )
            claim_months: list[str] = []
            cursor = period_start
            while cursor < month_start:
                claim_months.append(cursor.strftime("%Y-%m"))
                cursor = (cursor + timedelta(days=35)).replace(
                    day=1, hour=0, minute=0, second=0, microsecond=0
                )
            claims = (
                await _claim_times(session, "month", claim_months)
                if settings.leaderboard_claim_enabled
                else {}
            )
            candidates: list[tuple[int, int, int, str]] = []
            skipped = 0
            for pid, score, _days, gram in ranked:
                player = await session.get(Player, pid)
                if (
                    player is not None
                    and player.wallet_address
                    and pid in staked
                ):
                    candidates.append((pid, score, gram, player.wallet_address))
                else:
                    skipped += 1
            if not candidates:
                logger.warning(
                    "Копилка %d нанотонов ждёт: у топ-%d лидеров нет кошелька/ставки",
                    total, top_k,
                )
                return False
            placed = _order_by_ties(candidates, claims)
            weights = parse_prize_pcts(settings.monthly_prize_weights)
            if not weights:
                # Пустой список весов (опечатка конфига) не должен сжечь горш:
                # метку не трогаем, копилка ждёт, пока конфиг не починят.
                logger.error(
                    "Копилка %d нанотонов ждёт: monthly_prize_weights не содержат "
                    "чисел — исправь конфиг, горш не тронут",
                    total,
                )
                return False
            payments: list[tuple[int, str, int]] = []
            top_amount = _weighted_amounts(total, placed, weights)
            for (player_id, _score, _gram, wallet), amount in zip(placed, top_amount):
                payments.append((player_id, wallet, amount))
        else:
            # Прежнее поведение: победители, набравшие максимум, делят ровно.
            winners = await _top_correct_voters(session, period_start, month_start)
            staked = await _players_with_stake(
                session, period_start, month_start, by="tally_ends_at"
            )
            payable_ids: list[int] = []
            wallets: dict[int, str] = {}
            for player_id, _count in winners:
                player = await session.get(Player, player_id)
                if (
                    player is not None
                    and player.wallet_address
                    and player_id in staked
                ):
                    wallets[player_id] = player.wallet_address
                    payable_ids.append(player_id)
            skipped = len(winners) - len(payable_ids)
            if not payable_ids:
                # Платить некому: метку НЕ двигаем, копилка ждёт следующего цикла.
                logger.warning(
                    "Копилка %d нанотонов ждёт: у лидеров (%s) нет привязанного кошелька "
                    "или ставки в этом месяце",
                    total,
                    [pid for pid, _ in winners] or "нет голосов",
                )
                return False
            shares = split_equal(total, payable_ids)
            payments = [
                (player_id, wallets[player_id], amount)
                for player_id, amount in shares.items()
            ]

        if not payments:
            # Страховка на любой неожиданный путь top-K: пустые выплаты не
            # двигают метку и не удаляют горш.
            logger.error(
                "Копилка %d нанотонов ждёт: нечего выплатить — метка не двигается",
                total,
            )
            return False

        network = "testnet" if settings.is_testnet else "mainnet"
        for player_id, wallet, amount in payments:
            session.add(
                Payout(
                    round_id=None,
                    player_id=player_id,
                    kind="leaderboard",
                    amount_nanotons=amount,
                    dest_address=wallet,
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
        payments,
        f" (без кошелька пропущены: {skipped})" if skipped else "",
    )
    if bot is not None and settings.admin_id_set:
        text = (
            f"🏆 Копилка лидерборда за {prev_key} отправлена: "
            f"{total / 1e9:.2f} Gram между {len(payments)} лидером(ами)."
        )
        for admin_id in settings.admin_id_set:
            try:
                await bot.send_message(admin_id, text)
            except Exception as exc:
                logger.warning("Алерт админу %s не доставлен: %s", admin_id, exc)
    return True


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


def _weighted_amounts(total_nanotons: int, payable: list, weights: list[int]) -> list[int]:
    """Доли месячной копилки между top-K получателями по весам.

    Веса нормируются по фактически оплачиваемым получателям: если весов
    меньше, чем получателей, последний вес повторяется до покрытия всех —
    так режим top-K не «теряет» места из-за короткого списка весов. Весь горш
    распределяется (пыль — первым получателям), незанятых мест нет.
    """
    if not payable or total_nanotons <= 0:
        return []
    used = list(weights[: len(payable)])
    if not used:
        return []
    if len(used) < len(payable):
        used += [used[-1]] * (len(payable) - len(used))
    total_w = sum(used)
    amounts = [total_nanotons * w // total_w for w in used]
    dust = total_nanotons - sum(amounts)
    for i in range(dust):
        amounts[i % len(amounts)] += 1
    return amounts


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
    """Выплачивает копилку прошедших недель топ-3, если эпилог последнего дня записи.

    Возвращает True, если выплата создана. Идемпотентно по метке в
    watcher_state; платить некому — метка не двигается, копилка ждёт.

    Выплата возможна ТОЛЬКО после установки флага week_leaderboard_ready,
    который ставится в _finalize_new_day_job() при записи эпилога последнего дня
    недели (воскресенья).
    """
    now = datetime.now(timezone.utc)
    prev_key = previous_week_key(now)

    async with SessionLocal() as session:
        marker = await session.get(WatcherState, WEEKLY_MARKER_KEY)
        settled_through = marker.value if marker is not None else ""
        if settled_through >= prev_key:
            return False

        # Флаг готовности: эпилог последнего дня недели записан.
        ready_marker = await session.get(WatcherState, WEEK_READY_KEY)
        ready_week = ready_marker.value if ready_marker is not None else ""
        if ready_week < prev_key:
            # Эпилог ещё не записан — лидерборд ждёт.
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
        rows = await _rank_window(session, period_start, period_end, by="opens_at", limit=50)
        staked = await _players_with_stake(session, period_start, period_end, by="opens_at")
        # Места недели — три сильнейших ИГРОКА по (верные пути, вклад Gram,
        # Claim, player_id), а не ступени счёта: внутри места приз не делится.
        claims = (
            await _claim_times(session, "week", [prev_key])
            if settings.leaderboard_claim_enabled
            else {}
        )
        candidates: list[tuple[int, int, int, str]] = []
        for pid, correct, days, gram in rows:
            if correct <= 0 or days < min_days or pid not in staked:
                continue
            player = await session.get(Player, pid)
            if player is None or not player.wallet_address:
                continue
            candidates.append((pid, correct, gram, player.wallet_address))
        places = _order_by_ties(candidates, claims)[:3]

        if not places:
            # Достойных нет: метку НЕ двигаем, копилка ждёт следующей недели.
            logger.warning(
                "Копилка недели %d нанотонов ждёт: нет игроков с кошельком, %s+ днями голосования "
                "и ставкой за неделю%s",
                total,
                min_days or settings.weekly_min_days,
                " (короткая стартовая неделя — порог снят)" if relaxed else "",
            )
            return False

        amounts, rolled = _week_prize_amounts(total, len(places))
        network = _active_network()
        paid: list[tuple[str, str, int]] = []
        for place, ((pid, correct, _gram, wallet), amount) in enumerate(zip(places, amounts), 1):
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
            paid.append((_MEDALS[place - 1], f"{name} — {correct} верных путей", amount))

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
        try:
            nomination = await _memory_nomination(session, period_start, period_end)
        except Exception:
            nomination = None
        await session.commit()

    logger.info(
        "Копилка недель ≤ %s выплачена: %s нанотонов → %s",
        prev_key,
        total,
        [(medal, name, amount) for medal, name, amount in paid],
    )
    lines = ["🏆 Итоги недели Стаи:"]
    for medal, name, amount in paid:
        lines.append(f"{medal} {name} — {amount / 1e9:.2f} Gram")
    lines.append(
        "При равенстве верных путей Стая смотрит на вклад Gram, а затем — "
        "кто раньше всех заявил о месте."
    )
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
