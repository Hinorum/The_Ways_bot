from __future__ import annotations

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.broadcast import announce_new_day
from app.config import settings
from app.db import SessionLocal
from app.models import RoundStatus
from app.rounds import (
    _now,
    close_voting,
    create_next_round_detailed,
    ensure_current_round,
    finish_tally,
    get_latest_round,
    write_epilogue,
)
from app.tally import award_points


scheduler = AsyncIOScheduler(timezone=settings.timezone)
_bot: Bot | None = None


def set_bot(bot: Bot) -> None:
    global _bot
    _bot = bot


async def tick(bot: Bot | None = None) -> None:
    bot = bot or _bot
    async with SessionLocal() as session:
        previous = await get_latest_round(session)
        current = await ensure_current_round(session)

        # Первый запуск или только что созданный день — анонсим без итогов.
        if previous is None or current.id > previous.id:
            await announce_new_day(bot, current)

        now = _now()
        if current.status == RoundStatus.OPEN and now >= current.voting_ends_at:
            await close_voting(session, current)
            return
        if current.status == RoundStatus.TALLYING and now >= current.tally_ends_at:
            finished, closed_here = await finish_tally(session, current)
            if closed_here:
                await award_points(session, finished)
                await write_epilogue(session, finished)
                if settings.ton_enabled:
                    # Финализируем сразу: пост итогов показывает реальные цифры.
                    from app.stakes import finalize_day_payouts

                    await finalize_day_payouts(session, finished)
            nxt, created = await create_next_round_detailed(session)
            if created:
                await announce_new_day(bot, nxt, finished if closed_here else None)


async def _ton_maintenance() -> None:
    """Финализация дней, очередь выплат, ретраи — и копилка месяца 1-го числа."""
    from app.leaderboard import settle_month_if_due
    from app.ton_pay import settle_closed_rounds

    await settle_closed_rounds(bot=_bot)
    await settle_month_if_due(bot=_bot)


def start_scheduler() -> None:
    scheduler.add_job(
        tick,
        "interval",
        seconds=15,
        id="way-tick",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if settings.ton_enabled:
        from app.ton_watch import watch_once

        scheduler.add_job(
            watch_once,
            "interval",
            seconds=60,
            id="ton-watch",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            _ton_maintenance,
            "interval",
            seconds=120,
            id="ton-settle",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    scheduler.start()
