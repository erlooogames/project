import asyncio
from math import ceil
from time import monotonic, time

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import BanChatMember, GetUpdates, RestrictChatMember


class TimedActionPast(Exception):
    pass


class TelegramRateLimit(BaseRequestMiddleware):
    def __init__(self):
        self.blocked_until = 0.0

    async def __call__(self, make_request, bot, method):
        if isinstance(method, GetUpdates):
            while True:
                try:
                    return await make_request(bot, method)
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(max(1, exc.retry_after) + 0.2)

        for attempt in range(3):
            delay = self.blocked_until - monotonic()
            if delay > 5:
                raise TelegramRetryAfter(method, "Telegram retry window", ceil(delay))
            if delay > 0:
                await asyncio.sleep(delay)
            if isinstance(method, (BanChatMember, RestrictChatMember)):
                until = method.until_date
                if isinstance(until, int) and until - time() < 35:
                    raise TimedActionPast
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as exc:
                self.blocked_until = max(
                    self.blocked_until, monotonic() + max(1, exc.retry_after) + 0.2
                )
                if exc.retry_after >= 5 or attempt == 2:
                    raise
