import asyncio
import contextlib
import logging
import signal
from pathlib import Path

import httpx
from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from app.config import settings
from app.db import init_db
from app.handlers import build_dispatcher, create_bot
from app.profile import apply_profile
from app.scheduler import set_bot, start_scheduler, tick


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("way")


async def health(_request: web.Request) -> web.Response:
    """Живость + операционный снимок: тик, очередь выплат, watcher, день.

    Сбой снимка (переходное окно миграции, деградация БД) не роняет
    эндпоинт — Render должен видеть процесс живым; но и «ok» без данных мы
    не притворяемся: честный статус degraded.
    """
    try:
        from app.ops import snapshot

        payload = await snapshot()
    except Exception as exc:
        log.warning("snapshot упал — отвечаем degraded: %s", exc)
        payload = {"status": "degraded", "detail": "snapshot unavailable"}
    return web.json_response(payload)


async def _self_ping_loop(stop: asyncio.Event) -> None:
    """Пингует собственный /health: free plan Render засыпает без входящего
    трафика, а каждый пинг считается входящим запросом. Расписание дней
    якорится к UTC-сетке, поэтому без пинга день открывался бы при первом
    пробудившем запросе, а не в 11:00 UTC."""
    if not settings.public_base_url:
        return
    url = f"{settings.public_base_url}/health"
    while not stop.is_set():
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(url)
            log.info("self-ping %s -> %s", url, response.status_code)
        except Exception as exc:
            log.warning("self-ping не удался: %s", exc)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.self_ping_seconds)


def _install_stop_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass


async def boot_game(bot) -> None:
    """Стартовые шаги. Планировщик запускается ПЕРВЫМ делом: сетевой сбой
    бэкапа или профиля не смеет оставлять игру без тиков навсегда (раньше
    исключение до start_scheduler означало молчаливо мёртвое расписание)."""
    set_bot(bot)
    try:
        await tick(bot)
    except Exception:
        log.exception("Первый тик не удался — повторится по расписанию")
    start_scheduler()
    from app.scheduler import boot_maintenance
    # GEPA: загружаем лучший ген в memory-cache при старте
    try:
        from app.narrative_ai import load_active_gene
        await load_active_gene()
    except Exception:
        log.exception("GEPA: не удалось загрузить ген при старте")

    # AI World Engine: seed NPC profiles, prologue beats, season arcs
    try:
        from app.db import SessionLocal
        from app.npc_cog import seed_npc_profiles
        from app.prologue import seed_prologue_beats
        from app.story_arc import seed_season_arcs
        from app.story import _chat_completion

        async with SessionLocal() as session:
            npc_count = await seed_npc_profiles(session, llm_caller=_chat_completion)
            prologue_count = await seed_prologue_beats(session, llm_caller=_chat_completion, season=1)
            arc_count = await seed_season_arcs(session, llm_caller=_chat_completion, season=1)
            from app.lore import (
                seed_atmospheric_pools, load_all_atmospheric,
                seed_voice_examples, load_all_voice_examples,
                seed_inner_thoughts, load_all_inner_thoughts,
                seed_voice_banned, load_all_voice_banned,
                seed_dog_pads, load_all_dog_pads,
                seed_echo_tones, load_all_echo_tones,
                seed_weather_pool, load_weather_pool,
                seed_places, load_places,
            )
            from app.season import seed_villain_events, seed_heretic_events, load_villain_events, load_heretic_events
            atm_count = await seed_atmospheric_pools(session, llm_caller=_chat_completion, season=1)
            voice_count = await seed_voice_examples(session, llm_caller=_chat_completion, season=1)
            banned_count = await seed_voice_banned(session, llm_caller=_chat_completion, season=1)
            thoughts_count = await seed_inner_thoughts(session, llm_caller=_chat_completion, season=1)
            dogpad_count = await seed_dog_pads(session, llm_caller=_chat_completion, season=1)
            echo_count = await seed_echo_tones(session, llm_caller=_chat_completion, season=1)
            weather_count = await seed_weather_pool(session, llm_caller=_chat_completion, season=1)
            places_count = await seed_places(session, llm_caller=_chat_completion, season=1)
            villain_count = await seed_villain_events(session, llm_caller=_chat_completion, season=1)
            heretic_count = await seed_heretic_events(session, llm_caller=_chat_completion, season=1)
            await load_all_atmospheric(session, season=1)
            await load_all_voice_examples(session, season=1)
            await load_all_voice_banned(session, season=1)
            await load_all_inner_thoughts(session, season=1)
            await load_all_dog_pads(session, season=1)
            await load_all_echo_tones(session, season=1)
            await load_weather_pool(session, season=1)
            await load_places(session, season=1)
            await load_villain_events(session, season=1)
            await load_heretic_events(session, season=1)
            log.info(
                "AI World Engine: %d NPC, %d prologue, %d arcs, %d atm, %d voice, %d banned, %d thoughts, %d dogpad, %d echo, %d weather, %d places, %d villain, %d heretic",
                npc_count, prologue_count, arc_count, atm_count, voice_count, banned_count, thoughts_count, dogpad_count, echo_count, weather_count, places_count, villain_count, heretic_count,
            )
            # Auto-regenerate: замена хардкод-фолбэков на AI-данные
            try:
                from app.ai_regenerate import regenerate_all
                regen = await regenerate_all(session, _chat_completion, season=1)
                if regen:
                    log.info("AI Regeneration: %s", regen)
            except Exception:
                log.exception("AI Regeneration failed")
    except Exception:
        log.exception("AI World Engine: seeding failed — using hardcoded fallbacks")

    for name, step in (("backup", boot_maintenance), ("profile", lambda: apply_profile(bot))):
        try:
            await step()
        except Exception:
            log.exception("Шаг старта «%s» не удался — игра продолжается без него", name)


async def run_webhook(bot, dispatcher) -> None:
    path = "/webhook"
    secret = settings.webhook_secret or None
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    SimpleRequestHandler(dispatcher=dispatcher, bot=bot, secret_token=secret).register(app, path=path)
    setup_application(app, dispatcher, bot=bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.port)
    await site.start()
    if settings.public_base_url:
        # drop_pending_updates=False: накопленные за сон апдейты (голоса
        # кнопками, оплаты Stars!) должны обработаться, а не выброситься.
        await bot.set_webhook(
            f"{settings.public_base_url}{path}",
            secret_token=secret,
            drop_pending_updates=False,
        )
    boot_task = asyncio.create_task(boot_game(bot))
    stop = asyncio.Event()
    ping_task = asyncio.create_task(_self_ping_loop(stop))
    _install_stop_handlers(stop)
    await stop.wait()
    log.info("Остановка: глушим планировщик и веб-сервер")
    from app.scheduler import shutdown_scheduler

    shutdown_scheduler()
    stop.set()  # будим self-ping для корректного завершения
    boot_task.cancel()
    ping_task.cancel()
    for task in (boot_task, ping_task):
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await runner.cleanup()
    await bot.session.close()


def ensure_webhook_secret() -> None:
    """Fail-fast: вебхук без секрета принимает поддельные апдейты.

    Кто угодно, знающий URL сервиса, мог бы отправить фальшивое «сообщение
    от админа» и выполнить /resetgame или /advance. Лучше упасть на старте,
    чем держать открытый командный контур.
    """
    if settings.use_webhook and not settings.webhook_secret:
        raise RuntimeError(
            "WEBHOOK_SECRET обязателен в режиме вебхука: без него кто угодно, "
            "знающий URL сервиса, может подсунуть фальшивый апдейт Telegram "
            "(вплоть до сообщений от имени админа)."
        )


async def main() -> None:
    Path("data").mkdir(exist_ok=True)
    Path(settings.media_dir).mkdir(parents=True, exist_ok=True)
    await init_db()
    bot = await create_bot()
    dispatcher = build_dispatcher()
    if settings.use_webhook:
        ensure_webhook_secret()
        await run_webhook(bot, dispatcher)
        return
    await bot.delete_webhook(drop_pending_updates=False)
    await boot_game(bot)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
