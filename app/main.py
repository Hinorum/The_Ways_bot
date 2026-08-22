import asyncio
import contextlib
import logging
import signal
from pathlib import Path

from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from app.config import settings
from app.db import init_db
from app.handlers import build_dispatcher, create_bot
from app.profile import apply_profile
from app.scheduler import scheduler, set_bot, start_scheduler, tick


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("way")


async def health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def boot_game(bot) -> None:
    try:
        set_bot(bot)
        await apply_profile(bot)
        await tick(bot)
        start_scheduler()
    except Exception:
        log.exception("Failed to prepare the daily round")


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
        await bot.set_webhook(
            f"{settings.public_base_url}{path}",
            secret_token=secret,
            drop_pending_updates=True,
        )
    boot_task = asyncio.create_task(boot_game(bot))
    stop = asyncio.Event()
    _install_stop_handlers(stop)
    await stop.wait()
    log.info("Остановка: глушим планировщик и веб-сервер")
    scheduler.shutdown(wait=False)
    boot_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await boot_task
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
    await bot.delete_webhook(drop_pending_updates=True)
    await boot_game(bot)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
