import unittest
from dataclasses import replace
from datetime import UTC, datetime
from time import time
from unittest.mock import AsyncMock, Mock, patch

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import BanChatMember, GetUpdates

from bot.commands import (
    ALIASES,
    MAX_ID,
    UsageError,
    parse_command,
    parse_duration,
    parse_target,
)
from bot.middlewares.telegram import TelegramRateLimit, TimedActionPast
from bot.models import ChatSettings
from bot.services.access import Problem
from bot.services.music import MusicSearch, parse_track
from bot.services.spam import SpamMonitor


class CommandTests(unittest.TestCase):
    def test_aliases_are_case_insensitive(self):
        for alias, canonical in ALIASES.items():
            with self.subTest(alias=alias):
                command = parse_command(f"!{alias.upper()} текст")
                self.assertEqual(command.name, canonical)
                self.assertEqual(command.args, "текст")
        self.assertEqual(parse_command("!СНЯТЬ\tВАРН 25 #7").name, "unwarn")
        self.assertEqual(parse_command("!огонёк").name, "streak")
        self.assertIsNone(parse_command("ordinary text !ban"))
        self.assertEqual(parse_command("!unknown").name, "")

    def test_russian_and_english_durations_and_bounds(self):
        for expression, expected in {
            "м 30": 1800,
            "д 7": 604800,
            "н 2": 1209600,
            "m 1": 60,
            "d 2": 172800,
            "w 1": 604800,
            "д 365": 31536000,
            "навсегда": None,
            "perm": None,
        }.items():
            with self.subTest(expression=expression):
                self.assertEqual(parse_duration(f"{expression} 123"), (expected, "123"))
        for expression in ("м 0", "мин 5", "д 366", "м -1", "н 99", "m 3.5", "w", ""):
            with self.subTest(expression=expression), self.assertRaises(UsageError):
                parse_duration(expression)
        with self.assertRaises(UsageError):
            parse_duration("perm", allow_permanent=False)

    def test_targets_and_warning_id(self):
        self.assertEqual(parse_target("", 42).user_id, 42)
        self.assertEqual(parse_target("123456789 причина", 42).user_id, 123456789)
        self.assertEqual(
            parse_target("id=91 причина с пробелами", None).reason,
            "причина с пробелами",
        )
        self.assertEqual(parse_target("-- 1 повтор", 42).reason, "1 повтор")
        self.assertEqual(parse_target("#7 причина", 42, allow_warn_id=True).warn_id, 7)
        self.assertEqual(
            parse_target("123 #7 причина", None, allow_warn_id=True).warn_id, 7
        )
        for target in ("", "0", "-4", str(MAX_ID + 1), "@unknown"):
            with self.subTest(target=target), self.assertRaises(UsageError):
                parse_target(target, None)

    def test_kyiv_calendar_day(self):
        utc_evening = datetime(2026, 7, 1, 21, 5, tzinfo=UTC)
        self.assertEqual(ChatSettings().day(utc_evening).isoformat(), "2026-07-02")
        self.assertEqual(
            ChatSettings(timezone="UTC").day(utc_evening).isoformat(), "2026-07-01"
        )


class SpamTests(unittest.TestCase):
    def test_threshold_window_and_chat_isolation(self):
        monitor = SpamMonitor()
        settings = replace(ChatSettings(), spam_limit=2, spam_window=10)
        self.assertIsNone(monitor.observe(1, 2, 100, b"a", settings).violation)
        self.assertIsNone(monitor.observe(1, 2, 104, b"b", settings).violation)
        self.assertIsNone(monitor.observe(2, 2, 105, b"c", settings).violation)
        self.assertEqual(monitor.observe(1, 2, 109, b"c", settings).violation, "flood")
        self.assertTrue(monitor.should_enforce(1, 2))
        self.assertFalse(monitor.should_enforce(1, 2))
        self.assertIsNone(monitor.observe(1, 2, 119, b"d", settings).violation)
        monitor.reset_chat(1)
        self.assertNotIn((1, 2), monitor.buckets)
        self.assertIn((2, 2), monitor.buckets)

    def test_duplicates_and_bounded_state(self):
        monitor = SpamMonitor(capacity=2)
        settings = ChatSettings()
        self.assertFalse(monitor.observe(1, 2, 101, b"repeat", settings).duplicate)
        self.assertTrue(monitor.observe(1, 2, 100, b"repeat", settings).duplicate)
        self.assertEqual(
            monitor.observe(1, 2, 102, b"repeat", settings).violation, "duplicate"
        )
        monitor.observe(1, 3, 100, b"a", settings)
        monitor.observe(1, 4, 100, b"a", settings)
        self.assertNotIn((1, 2), monitor.buckets)


class TelegramLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_after_respected_for_requests_and_polling(self):
        for method in (BanChatMember(chat_id=-1, user_id=2), GetUpdates(timeout=30)):
            with self.subTest(method=type(method).__name__):
                clock = Mock(return_value=0.0)

                async def sleep(delay, clock=clock):
                    clock.return_value += delay

                request = AsyncMock(
                    side_effect=[TelegramRetryAfter(method, "slow", 2), True]
                )
                with (
                    patch("bot.middlewares.telegram.monotonic", clock),
                    patch("bot.middlewares.telegram.asyncio.sleep", side_effect=sleep),
                ):
                    self.assertTrue(await TelegramRateLimit()(request, None, method))
                self.assertGreaterEqual(clock.return_value, 2)
                self.assertEqual(request.await_count, 2)

    async def test_nearly_expired_ban_is_never_sent_as_permanent(self):
        request = AsyncMock()
        method = BanChatMember(chat_id=-1, user_id=2, until_date=int(time()) + 25)
        with self.assertRaises(TimedActionPast):
            await TelegramRateLimit()(request, None, method)
        request.assert_not_awaited()


class MusicTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_uses_cache_and_per_user_limit(self):
        response = AsyncMock()
        response.__aenter__.return_value = response
        response.status = 200
        response.raise_for_status = unittest.mock.Mock()
        response.json.return_value = {
            "results": [
                {
                    "artistName": "A & B",
                    "trackName": "A track",
                    "trackViewUrl": "https://music.apple.com/us/song/sample",
                    "previewUrl": "https://audio-ssl.itunes.apple.com/sample.m4a",
                }
            ]
        }
        client = unittest.mock.Mock()
        client.get.return_value = response
        music = MusicSearch(client, "US")
        track = await music.search("A track", 1)
        self.assertEqual(track.artist, "A & B")
        with self.assertRaises(Problem):
            await music.search("Other track", 1)
        self.assertEqual(await music.search("a  track", 2), track)
        self.assertEqual(client.get.call_count, 1)
        self.assertEqual(client.get.call_args.kwargs["params"]["entity"], "song")

    async def test_provider_429_respects_retry_header(self):
        response = AsyncMock()
        response.__aenter__.return_value = response
        response.status = 429
        response.headers = {"Retry-After": "120"}
        client = unittest.mock.Mock()
        client.get.return_value = response
        music = MusicSearch(client, "US")
        for user in (1, 2):
            with self.assertRaises(Problem):
                await music.search("sample", user)
        self.assertEqual(client.get.call_count, 1)

    def test_rejects_non_provider_track_links_and_preview(self):
        item = {
            "artistName": "Example",
            "trackName": "A song",
            "trackViewUrl": "https://apple.com.invalid/a",
        }
        self.assertIsNone(parse_track(item))
        item.update(
            trackViewUrl="https://music.apple.com/a",
            previewUrl="http://itunes.apple.com/a",
        )
        self.assertIsNone(parse_track(item).preview)
