from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.broadcast import announce_new_day
from app.config import settings
from app.db import SessionLocal
from app.models import Round, RoundStatus
from app.rounds import (
    _now,
    claim_announcement,
    close_voting,
    create_next_round_detailed,
    ensure_current_round,
    finish_tally,
    get_latest_round,
    prepare_next_day,
    utc_aware,
    write_epilogue,
)
from app.tally import award_points


logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone=settings.timezone)
_bot: Bot | None = None


def set_bot(bot: Bot) -> None:
    global _bot
    _bot = bot


async def _prepare_job(round_id: int) -> None:
    """Фоновая заготовка следующего дня в час подсчёта.

    Ошибки не роняют тик: если заготовка не удалась, день откроется старым
    синхронным путём (чуть позже по сетке) — деградация мягкая.
    """
    await asyncio.sleep(0)
    try:
        async with SessionLocal() as session:
            round_row = await session.get(Round, round_id)
            if round_row is None or round_row.status != RoundStatus.TALLYING:
                return
            started = await prepare_next_day(session, round_row.day_index)
            if started:
                logger.info("Заготовка дня %s собрана заранее", round_row.day_index + 1)
    except Exception:
        logger.exception("Прегенерация следующего дня не удалась — откроем синхронно")


async def tick(bot: Bot | None = None) -> None:
    bot = bot or _bot
    from app.ops import mark_tick

    await mark_tick()
    async with SessionLocal() as session:
        previous = await get_latest_round(session)
        current = await ensure_current_round(session)

        # Первый запуск или только что созданный день — анонсим без итогов.
        # claim_announcement гарантирует ровно один пост на день, даже если
        # после деплоя секунду работают два процесса.
        if previous is None or current.id > previous.id:
            if await claim_announcement(session, current):
                await announce_new_day(bot, current)

        now = _now()
        if current.status == RoundStatus.OPEN and now >= utc_aware(current.voting_ends_at):
            await close_voting(session, current)
            return
        if current.status == RoundStatus.TALLYING and now < utc_aware(current.tally_ends_at):
            # Час подсчёта — свободное окно: готовим следующий день заранее,
            # чтобы в 11:00 UTC открыть его мгновенно из готовой заготовки.
            asyncio.create_task(_prepare_job(current.id))
        if current.status == RoundStatus.TALLYING and now >= utc_aware(current.tally_ends_at):
            finished, closed_here = await finish_tally(session, current)
            if closed_here:
                await award_points(session, finished)
                await write_epilogue(session, finished)
                # Финализируем всегда, а не только при включённом TON:
                # если флаг погасили посреди дня со ставками, долг игрокам
                # должен остаться видимым (очередь+алерты), а не исчезнуть.
                from app.stakes import finalize_day_payouts

                await finalize_day_payouts(session, finished)
            nxt, created = await create_next_round_detailed(session)
            if created:
                await announce_new_day(bot, nxt, finished if closed_here else None)


async def _watch_job() -> None:
    """Watcher ставок с ботом: игрок получает личное о судьбе перевода."""
    from app.ton_watch import watch_once

    await watch_once(bot=_bot)


async def _ton_maintenance() -> None:
    """Финализация дней, очередь выплат, ретраи — и копилка месяца 1-го числа."""
    from app.leaderboard import settle_month_if_due
    from app.ops import check_anomalies
    from app.ton_pay import settle_closed_rounds

    await settle_closed_rounds(bot=_bot)
    await settle_month_if_due(bot=_bot)
    try:
        problems = await check_anomalies(_bot)
        if problems:
            logger.warning("Аномалии: %s", "; ".join(problems))
    except Exception:
        logger.exception("Проверка аномалий упала (не мешает обслуживанию)")


async def boot_maintenance() -> None:
    """Разовые задачи при старте: свежий бэкап БД до всего остального."""
    from app.backups import backup_job

    await backup_job()


def start_scheduler() -> None:
    from app.backups import backup_job

    scheduler.add_job(
        tick,
        "interval",
        seconds=15,
        id="way-tick",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    # Суточный бэкап в «мёртвый» час: 04:17 UTC.
    scheduler.add_job(
        backup_job,
        "cron",
        hour=4,
        minute=17,
        id="db-backup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if settings.ton_enabled:
        scheduler.add_job(
            _watch_job,
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
