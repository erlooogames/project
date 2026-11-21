import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from bot.database import DELETE_AGE, Database, DuplicateCommand
from bot.models import Action, ChatSettings


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "bot.sqlite3"

    async def test_owner_claim_is_atomic_and_persistent(self):
        async with Database(self.path) as db:
            results = await asyncio.gather(
                db.claim_owner(10, 1000), db.claim_owner(11, 1000)
            )
            self.assertEqual(sorted(results), [False, True])
            user_id = await db.owner_id()
        async with Database(self.path) as db:
            self.assertEqual(await db.owner_id(), user_id)
            self.assertFalse(await db.claim_owner(12, 1001))

    async def test_warning_expiry_and_consumption_survive_restart(self):
        async with Database(self.path) as db:
            for index, end in enumerate((1060, None, None), 1):
                warning = Action(
                    -100, 3, 4, "warn", "manual", "reason", 1000, end, index
                )
                warn_id, count, auto = await db.add_warning(
                    warning, ChatSettings(), allow_auto=True
                )
                self.assertEqual(count, index)
                self.assertEqual(warn_id, index)
                self.assertEqual(auto is not None, index == 3)
            self.assertTrue(await db.auto_batch_active(auto.id, 1059))
            self.assertFalse(await db.auto_batch_active(auto.id, 1060))
            self.assertEqual(await db.active_warnings(-100, 3, 1060), (2, [3, 2]))
            with self.assertRaises(DuplicateCommand):
                await db.add_warning(warning, ChatSettings(), allow_auto=True)
        async with Database(self.path) as db:
            fourth = replace(warning, message_id=4, created_at=1070)
            _, count, successor = await db.add_warning(
                fourth, ChatSettings(), allow_auto=True
            )
            self.assertEqual(count, 3)
            self.assertIsNone(successor)
            remove = replace(fourth, action="unwarn", message_id=5)
            self.assertEqual(await db.revoke_warning(remove, 2), 2)
            self.assertEqual(await db.active_warnings(-100, 3, 1070), (2, [4, 3]))
            self.assertTrue(
                all(
                    row.auto_action_id == auto.id
                    for row in (await db.warnings(-100, 3))[-3:]
                )
            )

    async def test_streak_uses_calendar_days_and_keeps_record(self):
        async with Database(self.path) as db:
            for day in (1, 1, 2, 5, 4, 6):
                await db.record_activity(
                    -100, 10, "Alice", date(2026, 1, day), streak=True, stats=True
                )
            await db.record_activity(
                -101, 10, "Alice", date(2026, 1, 5), streak=True, stats=False
            )
            for i in range(100, 112):
                await db.record_activity(
                    -100, i, "Bob", date(2026, 1, 6), streak=True, stats=False
                )
            alice = await db.activity(-100, 10)
            self.assertEqual(alice.streak(date(2026, 1, 7)), 2)
            self.assertEqual(alice.streak(date(2026, 1, 8)), 0)
            self.assertEqual(alice.best, 2)
            self.assertEqual((await db.activity(-101, 10)).current, 1)
            self.assertEqual(await db.stats(-100), (6, 1))
            ranked = await db.leaderboard(-100, date(2026, 1, 6))
            self.assertEqual(len(ranked), 10)
            self.assertEqual(ranked[0].user_id, 10)

    async def test_purge_excludes_other_topics_old_and_future_messages(self):
        async with Database(self.path) as db:
            for msg, topic, when in (
                (1, 10, 100),
                (2, 10, 200),
                (3, 20, 300),
                (4, 10, 400),
                (5, 10, 500),
            ):
                self.assertTrue(await db.remember_message(-100, msg, topic, when))
            self.assertFalse(await db.remember_message(-100, 1, 10, 100))
            ids = await db.purge_candidates(-100, 5, 10, 100 + DELETE_AGE, 100)
            self.assertEqual(ids, [4, 2])

    async def test_activity_count_survives_replayed_and_deleted_messages(self):
        async with Database(self.path) as db:
            await db.remember_message(-100, 1, 0, 15)
            day = date(2026, 1, 1)
            await db.record_activity(
                -100, 10, "Alice", day, streak=True, stats=True, message_id=1
            )
            await db.forget_messages(-100, [1])
        async with Database(self.path) as db:
            self.assertFalse(await db.remember_message(-100, 1, 0, 15))
            await db.record_activity(
                -100, 10, "Alice", day, streak=True, stats=True, message_id=1
            )
            self.assertEqual(await db.stats(-100), (1, 1))
            self.assertEqual(await db.purge_candidates(-100, 2, 0, 20, 1), [])

    async def test_group_migration_and_settings(self):
        async with Database(self.path) as db:
            await db.update_settings(-1, language="en", antispam_enabled=True)
            await db.update_settings(-2, timezone="UTC")
            await db.record_activity(
                -1, 10, "name", date(2026, 1, 1), streak=True, stats=True
            )
            await db.record_activity(
                -2, 10, "name", date(2026, 1, 2), streak=True, stats=True
            )
            await db.add_warning(
                Action(-1, 10, 4, "warn", "manual", "", 1000, None, 51),
                ChatSettings(),
                allow_auto=False,
            )
            await db.create_action(
                Action(
                    -2, 10, 4, "warn", "manual", "", 1000, None, 51, status="applied"
                )
            )
            await db.migrate_group(-1, -2)
            await db.migrate_group(-1, -2)
            self.assertTrue(await db.group_replaced(-1))
            self.assertEqual(await db.stats(-2), (2, 1))
            self.assertEqual(await db.active_warnings(-2, 10, 2000), (1, [1]))
            self.assertEqual(len(await db.actions(-2, 10)), 2)
            self.assertEqual((await db.settings(-2)).timezone, "UTC")
            self.assertEqual((await db.settings(-1)).language, "ru")
