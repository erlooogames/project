import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import ErrorEvent
from aiohttp import ClientSession, ClientTimeout, TCPConnector

from bot.config import Config, configure_logging
from bot.database import Database
from bot.handlers.groups import group_router
from bot.handlers.private import private_router
from bot.middlewares.telegram import TelegramRateLimit
from bot.runtime import Services

logger = logging.getLogger(__name__)


def create_dispatcher(services: Services) -> Dispatcher:
    dispatcher = Dispatcher(services=services)
    dispatcher.include_router(private_router())
    dispatcher.include_router(group_router())
    dispatcher.error.register(log_update_error)
    return dispatcher


async def log_update_error(event: ErrorEvent) -> bool:
    logger.error("Update %s failed", event.update.update_id, exc_info=event.exception)
    return True


async def prepare_bot(bot: Bot) -> None:
    delay = 2
    while True:
        try:
            await bot.me()
            await bot.delete_webhook(drop_pending_updates=False)
            return
        except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter) as exc:
            retry = (
                max(delay, exc.retry_after + 1)
                if isinstance(exc, TelegramRetryAfter)
                else delay
            )
            logger.warning(
                "Telegram startup failed (%s). Retrying in %ss.",
                type(exc).__name__,
                retry,
            )
            await asyncio.sleep(retry)
            delay = min(delay * 2, 30)


async def run(config: Config) -> None:
    session = AiohttpSession(timeout=12)
    session.middleware(TelegramRateLimit())
    async with (
        Bot(config.token, session=session) as bot,
        Database(config.db_path) as db,
        ClientSession(
            timeout=ClientTimeout(total=7),
            connector=TCPConnector(limit=4),
            trust_env=True,
        ) as http,
    ):
        services = Services.create(config, bot, db, http)
        dispatcher = create_dispatcher(services)
        await prepare_bot(bot)
        maintenance = asyncio.create_task(
            services.moderation.maintain(), name="moderation-maintenance"
        )
        try:
            await dispatcher.start_polling(
                bot,
                polling_timeout=30,
                tasks_concurrency_limit=32,
                tasks_shutdown_timeout=10,
                allowed_updates=dispatcher.resolve_used_update_types(),
                close_bot_session=False,
            )
        finally:
            maintenance.cancel()
            with suppress(asyncio.CancelledError):
                await maintenance


def main() -> None:
    try:
        config = Config.from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    configure_logging(config)
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        logger.info("Stopped.")
    except Exception:
        logger.exception("Bot stopped unexpectedly.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
