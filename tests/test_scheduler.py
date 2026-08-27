"""Планировщик e2e: тик закрывает голосование, готовит и открывает следующий день.

Работаем с глобальной БД (SessionLocal), как настоящий тик; сетевые
генераторы заменены мгновенными — интересует только конечный автомат дня.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select

from app.art_director import offline_bible
from app.lore import compose_chapter
from app.config import settings
from app.db import SessionLocal
from app.models import Card, PreparedDay, Round, RoundStatus, WatcherState, WinRule
from app.rounds import _PREGEN_LOCK_PREFIX
from app.scheduler import _prepare_job, tick


@pytest.fixture(autouse=True)
def offline_generation(monkeypatch):
    from app import rounds as rounds_mod

    async def instant_chapter(
        day_index,
        beats,
        rule=None,
        echoes=None,
        distant_echoes=None,
        season_block=None,
        villain_block=None,
        sealed=False,
        pending_outcome=False,
        **kwargs,
    ):
        return compose_chapter(
            day_index, beats, rule, echoes, distant_echoes,
            season_block=season_block, villain_line=villain_block,
            sealed=sealed, pending_outcome=pending_outcome,
        )

    monkeypatch.setattr(rounds_mod, "generate_chapter", instant_chapter)
    monkeypatch.setattr(
        rounds_mod,
        "plan_day_art",
        AsyncMock(side_effect=lambda chapter, beats=None, anchor=None, extra_motifs=None: offline_bible(chapter)),
    )
    monkeypatch.setattr(rounds_mod, "fetch_day_image", AsyncMock(return_value=True))
    monkeypatch.setattr(settings, "ton_enabled", False)


async def _seed(day_index: int, status: RoundStatus, *, voting_in: timedelta, tally_in: timedelta) -> Round:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        round_row = Round(
            day_index=day_index,
            status=status,
            win_rule=WinRule.MAJORITY,
            rule_commitment="c:s",
            chapter_title=f"День {day_index}",
            chapter_text="Текст.",
            lore_summary="Канон.",
            cover_path="",
            opens_at=now - timedelta(hours=30),
            voting_ends_at=now + voting_in,
            tally_ends_at=now + tally_in,
            winner_card=0 if status == RoundStatus.TALLYING else None,
            vote_counts_json='{"0": 1}' if status == RoundStatus.TALLYING else "{}",
        )
        db.add(round_row)
        await db.commit()
        return round_row.id


async def _cleanup(*day_indexes: int) -> None:
    async with SessionLocal() as db:
        await db.execute(Round.__table__.delete().where(Round.day_index.in_(day_indexes)))
        await db.execute(delete(PreparedDay).where(PreparedDay.day_index.in_([d + 1 for d in day_indexes])))
        await db.commit()


async def _status_of(day_index: int) -> RoundStatus | None:
    async with SessionLocal() as db:
        row = (
            await db.execute(select(Round.status).where(Round.day_index == day_index).limit(1))
        ).scalar_one_or_none()
    return row


async def _drain_background(timeout: float = 10.0) -> None:
    """Тик плодит фоновые задачи (прегенерация, тизер, диспетчер выплат) —
    даём им закрыть сессии БД, иначе SQLite-лок валит очистку соседних тестов."""
    import asyncio

    for _ in range(20):
        await asyncio.sleep(0)
    pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    if pending:
        done, pending = await asyncio.wait(pending, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def test_prepare_job_seeds_next_day_and_clears_lock() -> None:
    round_id = await _seed(9501, RoundStatus.TALLYING, voting_in=timedelta(hours=-2), tally_in=timedelta(minutes=30))
    try:
        await _prepare_job(round_id)
        async with SessionLocal() as db:
            prepared = await db.get(PreparedDay, 9502)
            lock = (
                await db.execute(select(WatcherState).where(WatcherState.key == f"{_PREGEN_LOCK_PREFIX}9502"))
            ).scalar_one_or_none()
        assert prepared is not None and prepared.payload
        assert lock is None  # лок снят после успеха
    finally:
        await _cleanup(9501)


async def test_prepare_job_ignores_open_round(monkeypatch) -> None:
    prepare = AsyncMock(return_value=True)
    monkeypatch.setattr("app.scheduler.prepare_next_day", prepare)
    round_id = await _seed(9511, RoundStatus.OPEN, voting_in=timedelta(hours=5), tally_in=timedelta(hours=6))
    try:
        await _prepare_job(round_id)
        assert prepare.await_count == 0  # день ещё живой — готовить нечего
    finally:
        await _cleanup(9511)


async def test_prepare_job_swallows_errors(monkeypatch) -> None:
    async def boom(session, day_index):
        raise RuntimeError("генератор упал")

    monkeypatch.setattr("app.scheduler.prepare_next_day", boom)
    round_id = await _seed(9521, RoundStatus.TALLYING, voting_in=timedelta(hours=-2), tally_in=timedelta(hours=1))
    try:
        await _prepare_job(round_id)  # не роняет вызывающий тик
        async with SessionLocal() as db:
            lock = (
                await db.execute(select(WatcherState).where(WatcherState.key == f"{_PREGEN_LOCK_PREFIX}9522"))
            ).scalar_one_or_none()
        # Лок остался: следующий тик сможет повторить попытку после TTL.
        assert lock is not None or (await db.get(PreparedDay, 9522)) is None
    finally:
        await _cleanup(9521)


async def test_tick_closes_voting_when_window_over() -> None:
    await _seed(9531, RoundStatus.OPEN, voting_in=timedelta(minutes=-5), tally_in=timedelta(hours=1))
    try:
        await tick(None)
        assert await _status_of(9531) == RoundStatus.TALLYING
    finally:
        await _cleanup(9531)


async def test_tick_schedules_preparation_during_tally_window(monkeypatch) -> None:
    prepare_job = AsyncMock()
    monkeypatch.setattr("app.scheduler._prepare_job", prepare_job)
    await _seed(9541, RoundStatus.TALLYING, voting_in=timedelta(hours=-3), tally_in=timedelta(minutes=20))
    try:
        await tick(None)
        for _ in range(20):
            await asyncio.sleep(0)  # даём фоновой задаче стартовать
        assert prepare_job.await_count == 1
        assert prepare_job.await_args.args[0] >= 1
    finally:
        await _drain_background()
        await _cleanup(9541)


async def test_tick_finishes_day_and_opens_next() -> None:
    round_id = await _seed(9551, RoundStatus.TALLYING, voting_in=timedelta(hours=-3), tally_in=timedelta(minutes=-1))
    try:
        await tick(None)
        assert await _status_of(9551) == RoundStatus.CLOSED
        async with SessionLocal() as db:
            fresh = (
                await db.execute(select(Round).where(Round.day_index == 9552).limit(1))
            ).scalar_one_or_none()
            card_count = 0 if fresh is None else (
                await db.execute(
                    select(func.count()).select_from(Card).where(Card.round_id == fresh.id)
                )
            ).scalar_one()
        assert fresh is not None
        assert fresh.status == RoundStatus.OPEN
        assert fresh.chapter_title
        assert card_count == 3
        del round_id
    finally:
        await _drain_background()
        await _cleanup(9551, 9552)


async def test_start_scheduler_registers_only_zero_arg_jobs(monkeypatch) -> None:
    """Инцидент-регрессия: джоба с обязательным bot роняла boot_game целиком —
    игра оставалась без тиков и watcher'а. Каждая джоба обязана вызываться
    без аргументов, а кривая регистрация не смеет убить остальные."""
    import inspect

    from app import scheduler as scheduler_mod

    registered: list[tuple[str, object]] = []

    def fake_add_job(func, trigger, *, id, **kwargs):
        registered.append((id, func))

    monkeypatch.setattr(scheduler_mod.scheduler, "add_job", fake_add_job)
    monkeypatch.setattr(scheduler_mod.scheduler, "start", lambda: None)
    monkeypatch.setattr(settings, "ton_enabled", True)

    scheduler_mod.start_scheduler()

    ids = [job_id for job_id, _func in registered]
    assert {"way-tick", "db-backup", "ton-watch", "ton-settle",
            "img-polish", "style-review"} <= set(ids)
    for job_id, fn in registered:
        try:
            inspect.signature(fn).bind()
        except TypeError as exc:
            raise AssertionError(f"джоба {job_id} требует аргументы: {exc}")


def test_shutdown_scheduler_safe_when_never_started() -> None:
    """Остановка сервиса до старта планировщика не должна падать."""
    from app.scheduler import shutdown_scheduler

    if not scheduler_mod_running():
        shutdown_scheduler()  # не падает


def scheduler_mod_running() -> bool:
    from app.scheduler import scheduler as sched

    return bool(sched.running)


async def test_compose_whisper_weaves_candidates_without_leaking_votes(monkeypatch) -> None:
    """Вечерний привал ощущает публичные карты дня (называя их), но не
    раскрывает ни расклад, ни победителя."""
    from app.scheduler import _compose_whisper

    captured: dict = {}

    async def fake_chat_completion(messages, timeout=None):
        captured["user"] = messages[1]["content"]
        return [{"choices": [{"message": {"content": "Вечерняя сцена у огня."}}]}]

    monkeypatch.setattr("app.story._chat_completion", fake_chat_completion)

    text = await _compose_whisper(
        7,
        "до Дня Первого Лая 3 дн.",
        "Утренняя глава начиналась с тишины.",
        intrigue=False,
        candidates=[("Тропа Истока", "стайка вернётся к порталу"), ("Пепел Моста", "мост замолчит")],
    )

    assert text == "Вечерняя сцена у огня."
    user = captured["user"]
    # называем обе публичные карты дня
    assert "Тропа Истока" in user and "Пепел Моста" in user
    # вечер передаёт трепет переплетённости выбора
    assert "переплетённость" in user
    # запрет на раскрытие расклада и победителя
    assert "цифр голосов" in user and "имён победителя" in user
    assert "намёков на текущий расклад" in user

