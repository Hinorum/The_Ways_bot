from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.broadcast import announce_new_day, send_personal_echoes
from app.config import settings
from app.db import SessionLocal
from app.models import Round, RoundStatus, WatcherState
from app.rounds import (
    _now,
    claim_announcement,
    close_voting,
    create_next_round_detailed,
    ensure_current_round,
    finish_tally,
    get_latest_round,
    patch_prepared_day,
    prepare_next_day,
    utc_aware,
    write_epilogue,
)
from app.story import _TEASER_FALLBACKS
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


async def _micro_event_job(round_id: int, day_index: int) -> None:
    """Вечерний привал: микросцена-продолжение утренней главы в прайм-тайм,
    чтобы вечер жил между утром и итогами. Падения полностью некритичны."""
    try:
        from app.db import SessionLocal
        from app.models import Round, WatcherState

        marker = f"micro_event:{round_id}"
        async with SessionLocal() as session:
            row = await session.get(WatcherState, marker)
            if row is not None:
                return
            round_row = await session.get(Round, round_id)
            if round_row is None or round_row.status != RoundStatus.OPEN:
                return
            from app.rounds import get_run_anchor
            from app.season import run_position

            anchor = await get_run_anchor(session)
            moment = utc_aware(round_row.voting_ends_at)
            run_day, total = run_position(anchor, moment)
            season_hint = (
                "ДЕНЬ ПЕРВОГО ЛАЯ"
                if run_day >= total
                else f"до Дня Первого Лая {total - run_day} дн."
            )
            chapter_excerpt = (round_row.chapter_text or "")[:700]
            promises = []
            try:
                from app.promises import due_promises

                promises = await due_promises(session, day_index)
            except Exception:
                pass
            intrigue = day_index % 3 == 0
            promise_text = (promises[0] or {}).get("text") if promises else None
            text = await _compose_whisper(
                day_index, season_hint, chapter_excerpt,
                intrigue=intrigue, promise=promise_text,
            )
            session.add(WatcherState(key=marker, value="1"))
            await session.commit()
        from app.broadcast import whisper_to_chats

        await whisper_to_chats(_bot, text)
    except Exception:
        logger.exception("Вечерняя микросцена не удалась (не мешает тику)")


async def _compose_whisper(
    day_index: int,
    season_hint: str,
    chapter_excerpt: str = "",
    intrigue: bool = False,
    promise: str | None = None,
) -> str:
    """Микросцена вечера: нейротекст с офлайн-фолбэком. Не раскрывает ни эхи,
    ни расклад голосов — только продолжает утреннюю сцену одной репликой."""
    import random as _random

    from app.story import DM_SYSTEM_PROMPT, _chat_completion, text_is_clean

    hint = ""
    try:
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import LoreEcho

        async with SessionLocal() as session:
            echo = (
                await session.execute(
                    select(LoreEcho)
                    .where(
                        LoreEcho.status == "dormant",
                        LoreEcho.earliest_day <= day_index + 2,
                    )
                    .order_by(LoreEcho.strength.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if echo is not None:
            texture = {
                "угроза": "в воздухе иногда пахнет жжёной проводкой",
                "память": "чужой тёплый свет вспоминает миски",
                "обман": "среди теней мелькает чужой силуэт",
            }.get(echo.kind, "портал гудит не в такт")
            hint = f"Осторожная примета дня (без деталей и без слов «эхо»): {texture}."
    except Exception:
        hint = ""

    task = (
        "Вечерняя ИНТРИГА: поставь утреннюю примету под сомнение одной "
        "деталяю или вопросом, которого никто не произнёс вслух; "
        "финал — недоговорённость."
        if intrigue
        else (
            "Напиши микросцену вечера: 2-4 предложения (до 450 знаков). "
            "Стая у карт, сцена дотянулась до заката; одна прямая реплика "
            "персонажа в его манере речи; финал — недоговорённость перед "
            "закрытием развилки."
        )
    )
    promise_note = (
        f"Учти обещание мира: «{promise}». "
        if promise
        else ""
    )

    messages = [
        {"role": "system", "content": DM_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Вечерний привал, день {day_index}. Сезон: {season_hint}. "
                f"{hint} "
                + (
                    f"Утренняя глава дня начиналась так:\n«{chapter_excerpt}»\n"
                    if chapter_excerpt
                    else ""
                )
                + task
                + promise_note
                + "Без цифр голосов, без намёков на текущий расклад. "
                "Ответь чистым текстом, без JSON."
            ),
        },
    ]
    result = await _chat_completion(messages, timeout=45)
    if result is not None:
        try:
            text = str(result[0]["choices"][0]["message"]["content"]).strip()
            if text and text_is_clean(text):
                return text[:600]
        except Exception:
            pass
    fallback = [
        "Вечер стянул туман к самой земле. Стая легла вокруг карт, и никто не притронулся к ним первым.",
        "Лайнер прошёл мимо, позвякивая склянками с чужими снами: «Решайте, решайте… я только послушать».",
        "Архивариус захлопнул папку дня ровно на середине. «До утра всё может стать иначе», — шепнул он.",
        "Миски остыли, не тронутые. Баркод смотрел то на одну карту, то на другую, будто ждал подсказки.",
        "Безымянная встала мордой к порталу и не двинулась до темноты. Собаки чувствуют развилки кожей.",
        "Порталы загудели ниже обычного — почти шёпотом. Так сеть прислушивается к ещё не принятым решениям.",
        "Вектор обошёл карты по кругу трижды и лёг спиной к развилке. Упрямство тоже способ ждать.",
        "Где-то за холмом щёлкнул счётчик архива и замолчал на середине цифры. Никто не спросил, какой.",
        "Ветер принёс запах страницы, которую ещё не написали. Пиксель заворчал на пустоту.",
        "Хозяин Ошибки сегодня не считал вслух. Тишина от него страшнее любого счёта.",
        "Тени легли к картам, хотя света уже не было. Вечер здесь не спрашивает разрешения у физики.",
        "Стая переглянулась: до закрытия развилки оставалась ночь, а решение всё ещё было только одно — на всех.",
    ]
    rng = _random.Random(f"whisper:{day_index}")
    return rng.choice(fallback)


async def _teaser_job(round_id: int) -> None:
    """Тизер в час подсчёта: выбор сделан, победитель не называется.

    Драматургическая пауза между «стая проголосовала» и «итоги»: одна-две
    строки недосказанности во все чаты. Падения полностью некритичны.
    """
    try:
        marker = f"teaser:{round_id}"
        async with SessionLocal() as session:
            row = await session.get(WatcherState, marker)
            if row is not None:
                return
            round_row = await session.get(Round, round_id)
            if round_row is None or round_row.status != RoundStatus.TALLYING:
                return
            from app.models import RULE_MASKS, RULE_PHRASES

            rule_phrase = RULE_PHRASES[round_row.win_rule]
            mask_title, mask_mood = RULE_MASKS[round_row.win_rule]
            sealed = bool(getattr(round_row, "sealed", False))
        from app.story import generate_teaser

        text = await generate_teaser(round_row.day_index, rule_phrase)
        if not text:
            import random as _random

            text = _random.Random(f"teaser:{round_id}").choice(list(_TEASER_FALLBACKS))
        text = f"Маска дня — «{mask_title}»: {mask_mood}. {text}"
        if sealed:
            text += " И это ещё не всё: закон дня вскроется вместе с итогами."
        async with SessionLocal() as session2:
            session2.add(WatcherState(key=marker, value="1"))
            await session2.commit()
        from app.broadcast import whisper_to_chats

        delivered = await whisper_to_chats(_bot, f"🕯 {text}")
        logger.info("Тизер подсчёта дня %s разослан в %d чат(ов)", round_id, delivered)
    except Exception:
        logger.exception("Тизер подсчёта не удался (не мешает тику)")


async def _image_upgrade_job(day_index: int) -> None:
    """Отложенная перерисовка PIL-заглушек: пик 429 спадает за четверть часа."""
    try:
        await asyncio.sleep(900)
        from app.rounds import upgrade_stub_images

        upgraded = await upgrade_stub_images(day_index)
        if upgraded:
            logger.info("Картинки дня %d обновлены: %d кадр(ов) перерисовано", day_index, upgraded)
    except Exception:
        logger.exception("Апгрейд картинок дня %s не удался (не мешает тику)", day_index)


async def _personal_echo_job(round_id: int) -> None:
    """Личное эхо проигравшим голосующим: чем пахла бы их тропа.

    At-most-once: маркер ставится до отправок, чтобы сбой рассылки не
    превращался в повторные сообщения тем же игрокам. Падения некритичны.
    """
    try:
        marker = f"pecho:{round_id}"
        async with SessionLocal() as session:
            already = await session.get(WatcherState, marker)
            if already is not None:
                return
            session.add(WatcherState(key=marker, value="1"))
            await session.commit()
        async with SessionLocal() as session:
            round_row = (
                await session.execute(
                    select(Round)
                    .options(selectinload(Round.cards))
                    .where(Round.id == round_id, Round.status == RoundStatus.CLOSED)
                    .limit(1)
                )
            ).scalar_one_or_none()
        delivered = await send_personal_echoes(_bot, round_row)
        logger.info("Личное эхо дня %s доставлено %d игроку(ам)", round_id, delivered)
    except Exception:
        logger.exception("Личные эха дня %s не разосланы (не мешает тику)", round_id)


async def tick(bot: Bot | None = None) -> None:
    bot = bot or _bot
    from app.ops import mark_tick

    await mark_tick()
    async with SessionLocal() as session:
        previous = await get_latest_round(session)
        current = await ensure_current_round(session)

        # Прогрев кэшей для синхронных постов: якорь забега и живой банк дня.
        from app.rounds import get_run_anchor, refresh_round_pot_cache

        try:
            await get_run_anchor(session)
        except Exception:
            logger.exception("Якорь забега не прочитан (кэш останется прежним)")
        if current.status == RoundStatus.OPEN and settings.ton_enabled:
            try:
                await refresh_round_pot_cache(session, current)
            except Exception:
                logger.exception("Банк дня не обновлён (кэш останется прежним)")

        # Первый запуск или только что созданный день — анонсим без итогов.
        # claim_announcement гарантирует ровно один пост на день, даже если
        # после деплоя секунду работают два процесса.
        if previous is None or current.id > previous.id:
            if await claim_announcement(session, current):
                await announce_new_day(bot, current)

        now = _now()
        # Прегенерация следующего дня — в PREGEN_HOUR_UTC (за пару часов до
        # закрытия): глава, арт-библия и картинки готовы заранее, поэтому
        # на закрытии день откроется мгновенно.
        if (
            current.status == RoundStatus.OPEN
            and now.hour == settings.pregen_hour_utc % 24
            and now.minute < 15
        ):
            asyncio.create_task(_prepare_job(current.id))
        if current.status == RoundStatus.OPEN and now >= utc_aware(current.voting_ends_at):
            # БЕСШОВНОЕ ЗАКРЫТИЕ: подсчёт мгновенный — не выходим из тика,
            # а проваливаемся дальше к финализации в этом же проходе.
            await close_voting(session, current)
        # Вечерний привал: один раз за день, в настраиваемый час (прайм-тайм).
        if (
            current.status == RoundStatus.OPEN
            and now.hour == settings.whisper_hour_utc % 24
        ):
            from app.models import WatcherState

            marker = f"micro_event:{current.id}"
            async with SessionLocal() as session:
                already = await session.get(WatcherState, marker)
            if already is None:
                asyncio.create_task(_micro_event_job(current.id, current.day_index))
        if current.status == RoundStatus.TALLYING and now < utc_aware(current.tally_ends_at):
            # ЛЕГАСИ-окно (старые раунды с часом подсчёта): готовим следующий
            # день и тизер. Новая сетка проходит здесь насквозь мгновенно.
            asyncio.create_task(_prepare_job(current.id))
            # Тизер ожидания: раз за день, сразу после закрытия голосования.
            from app.models import WatcherState

            marker = f"teaser:{current.id}"
            async with SessionLocal() as session:
                already = await session.get(WatcherState, marker)
            if already is None:
                asyncio.create_task(_teaser_job(current.id))
        if current.status == RoundStatus.TALLYING and now >= utc_aware(current.tally_ends_at):
            finished, closed_here = await finish_tally(session, current)
            if closed_here:
                await award_points(session, finished)
                await write_epilogue(session, finished)
                # Фаза 2 прегенерации: заготовка завтра собрана до вскрытия
                # итогов — теперь итог дня известен и вплетается в её начало.
                try:
                    await patch_prepared_day(session, finished)
                except Exception:
                    logger.exception("Патч заготовки итогом дня не удался — день откроется как есть")
                # Финализируем всегда, а не только при включённом TON:
                # если флаг погасили посреди дня со ставками, долг игрокам
                # должен остаться видимым (очередь+алерты), а не исчезнуть.
                from app.stakes import finalize_day_payouts

                await finalize_day_payouts(session, finished)
                # Вознаграждения победителям: мгновенный пинок диспетчера,
                # деньги уходят в течение пары минут после итогов.
                asyncio.create_task(_payout_dispatch_job())
            nxt, created = await create_next_round_detailed(session)
            if created:
                await announce_new_day(bot, nxt, finished if closed_here else None)
            if closed_here and settings.personal_echo:
                asyncio.create_task(_personal_echo_job(finished.id))


async def _payout_dispatch_job() -> None:
    """Немедленная отправка вознаграждений после вскрытия итогов."""
    try:
        from app.ton_pay import dispatch_pending_payouts

        sent = await dispatch_pending_payouts(bot=_bot)
        logger.info("Диспетчер выплат (kick): отправлено %d", sent)
    except Exception:
        logger.exception("Kick выплат не удался (ретраи продолжатся по расписанию)")


async def _watch_job() -> None:
    """Watcher ставок с ботом: игрок получает личное о судьбе перевода."""
    from app.ton_watch import watch_once

    await watch_once(bot=_bot)


async def _ton_maintenance() -> None:
    """Финализация дней, очередь выплат, ретраи, копилки недели и месяца."""
    from app.leaderboard import settle_month_if_due, settle_week_if_due
    from app.ops import check_anomalies
    from app.ton_pay import settle_closed_rounds

    await settle_closed_rounds(bot=_bot)
    await settle_week_if_due(bot=_bot)
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
    # Шлифовка картинок-заглушек: каждые 2 часа, окно 24 часа с момента дня.
    from app.rounds import polish_stub_images

    scheduler.add_job(
        polish_stub_images,
        "interval",
        hours=2,
        id="img-polish",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    # Еженедельная L2-вычитка стиля: воскресенье 18:00 UTC, отчёт админам.
    from app.style_review import run_weekly_review_and_notify

    scheduler.add_job(
        run_weekly_review_and_notify,
        "cron",
        day_of_week="sun",
        hour=18,
        minute=0,
        id="style-review",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
