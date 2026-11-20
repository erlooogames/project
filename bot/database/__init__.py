import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Self

import aiosqlite

from bot.models import Action, Activity, ChatSettings, Restriction, Warning

DELETE_AGE = 48 * 60 * 60
RECENT_LIMIT = 200
ACTIVE_WARNING = "revoked_at IS NULL AND (ends_at IS NULL OR ends_at > ?)"


class DuplicateCommand(Exception):
    pass


async def one(connection: aiosqlite.Connection, sql: str, params: tuple = ()):
    async with connection.execute(sql, params) as cursor:
        return await cursor.fetchone()


async def execute(
    connection: aiosqlite.Connection, sql: str, params: tuple = ()
) -> int:
    async with connection.execute(sql, params) as cursor:
        return cursor.rowcount


def settings_from(row: aiosqlite.Row | None) -> ChatSettings:
    return ChatSettings(**json.loads(row["settings"])) if row else ChatSettings()


def activity_from(row: aiosqlite.Row) -> Activity:
    last_day = date.fromisoformat(row["last_day"]) if row["last_day"] else None
    return Activity(
        row["user_id"], row["display_name"], last_day, row["current"], row["best"]
    )


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.connection: aiosqlite.Connection

    async def __aenter__(self) -> Self:
        await asyncio.to_thread(self.path.parent.mkdir, parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path, isolation_level=None)
        self.connection.row_factory = aiosqlite.Row
        try:
            await execute(self.connection, "PRAGMA busy_timeout = 5000")
            await execute(self.connection, "PRAGMA journal_mode = WAL")
            await execute(self.connection, "PRAGMA foreign_keys = ON")
            version = await one(self.connection, "PRAGMA user_version")
            if version[0] == 0:
                schema = await asyncio.to_thread(
                    Path(__file__).with_name("schema.sql").read_text, encoding="utf-8"
                )
                async with self.connection.executescript(schema):
                    pass
            elif version[0] != 1:
                raise RuntimeError(f"Unsupported database schema version: {version[0]}")
        except BaseException:
            await self.connection.close()
            raise
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.connection.close()

    @asynccontextmanager
    async def session(
        self, *, write: bool = False
    ) -> AsyncIterator[aiosqlite.Connection]:
        async with self.lock:
            if not write:
                yield self.connection
                return
            try:
                await execute(self.connection, "BEGIN IMMEDIATE")
                yield self.connection
                await self.connection.commit()
            except BaseException:
                await self.connection.rollback()
                raise

    async def owner_id(self) -> int | None:
        async with self.session() as connection:
            row = await one(connection, "SELECT user_id FROM owner WHERE singleton = 1")
        return row["user_id"] if row else None

    async def claim_owner(self, user_id: int, now: int) -> bool:
        async with self.session(write=True) as connection:
            count = await execute(
                connection,
                "INSERT INTO owner VALUES (1, ?, ?) ON CONFLICT (singleton) DO NOTHING",
                (user_id, now),
            )
        return count == 1

    async def settings(self, chat_id: int) -> ChatSettings:
        async with self.session() as connection:
            row = await one(
                connection,
                "SELECT settings FROM chat_settings WHERE chat_id = ?",
                (chat_id,),
            )
        return settings_from(row)

    async def update_settings(self, chat_id: int, **changes) -> ChatSettings:
        async with self.session(write=True) as connection:
            row = await one(
                connection,
                "SELECT settings FROM chat_settings WHERE chat_id = ?",
                (chat_id,),
            )
            settings = replace(settings_from(row), **changes)
            await execute(
                connection,
                """INSERT INTO chat_settings VALUES (?, ?)
                   ON CONFLICT (chat_id) DO UPDATE SET settings = excluded.settings""",
                (chat_id, json.dumps(asdict(settings), ensure_ascii=False)),
            )
        return settings

    async def group_replaced(self, chat_id: int) -> bool:
        async with self.session() as connection:
            row = await one(
                connection,
                "SELECT 1 FROM chat_migrations WHERE old_chat_id = ?",
                (chat_id,),
            )
        return row is not None

    async def migrate_group(self, old_chat_id: int, new_chat_id: int) -> None:
        async with self.session(write=True) as connection:
            changed = await execute(
                connection,
                "INSERT INTO chat_migrations VALUES (?, ?) ON CONFLICT DO NOTHING",
                (old_chat_id, new_chat_id),
            )
            if not changed:
                return
            await execute(
                connection,
                """INSERT OR IGNORE INTO chat_settings
                                SELECT ?, settings FROM chat_settings WHERE chat_id = ?""",
                (new_chat_id, old_chat_id),
            )
            await execute(
                connection,
                "DELETE FROM chat_settings WHERE chat_id = ?",
                (old_chat_id,),
            )
            await execute(
                connection,
                "UPDATE actions SET chat_id = ?, message_id = NULL WHERE chat_id = ?",
                (new_chat_id, old_chat_id),
            )
            await execute(
                connection,
                "UPDATE warnings SET chat_id = ? WHERE chat_id = ?",
                (new_chat_id, old_chat_id),
            )
            await execute(
                connection,
                """INSERT INTO activities
                   SELECT ?, user_id, display_name, last_day, current, best, messages
                   FROM activities WHERE chat_id = ?
                   ON CONFLICT (chat_id, user_id) DO UPDATE SET
                       last_day = NULLIF(MAX(COALESCE(activities.last_day, ''), COALESCE(excluded.last_day, '')), ''),
                       current = CASE
                           WHEN COALESCE(excluded.last_day, '') > COALESCE(activities.last_day, '')
                               THEN excluded.current
                           WHEN COALESCE(excluded.last_day, '') = COALESCE(activities.last_day, '')
                               THEN MAX(activities.current, excluded.current)
                           ELSE activities.current
                       END,
                       best = MAX(activities.best, excluded.best),
                       messages = activities.messages + excluded.messages""",
                (new_chat_id, old_chat_id),
            )
            await execute(
                connection, "DELETE FROM activities WHERE chat_id = ?", (old_chat_id,)
            )
            await execute(
                connection,
                "DELETE FROM recent_messages WHERE chat_id = ?",
                (old_chat_id,),
            )

    @staticmethod
    async def insert_action(connection: aiosqlite.Connection, action: Action) -> int:
        row = await one(
            connection,
            """INSERT INTO actions
               (chat_id, user_id, actor_id, action, source, reason, created_at, ends_at,
                message_id, status, detail, retry_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (chat_id, message_id) DO NOTHING RETURNING id""",
            (
                action.chat_id,
                action.user_id,
                action.actor_id,
                action.action,
                action.source,
                action.reason,
                action.created_at,
                action.ends_at,
                action.message_id,
                action.status,
                action.detail,
                action.created_at,
            ),
        )
        if row is None:
            raise DuplicateCommand
        return row["id"]

    async def create_action(self, action: Action) -> Action:
        async with self.session(write=True) as connection:
            action_id = await self.insert_action(connection, action)
        return replace(action, id=action_id, retry_at=action.created_at)

    async def action(self, action_id: int) -> Action | None:
        async with self.session() as connection:
            row = await one(
                connection, "SELECT * FROM actions WHERE id = ?", (action_id,)
            )
        return Action(**dict(row)) if row else None

    async def due_actions(self, now: int) -> list[Action]:
        async with self.session() as connection:
            rows = await connection.execute_fetchall(
                "SELECT * FROM actions WHERE status = 'pending' AND retry_at <= ? ORDER BY retry_at, id LIMIT 40",
                (now,),
            )
        return [Action(**dict(row)) for row in rows]

    async def newer_action_exists(
        self, action: Action, family: tuple[str, ...]
    ) -> bool:
        placeholders = ", ".join("?" for _ in family)
        async with self.session() as connection:
            row = await one(
                connection,
                f"""SELECT 1 FROM actions WHERE chat_id = ? AND user_id = ? AND id > ?
                     AND status IN ('pending', 'applied') AND action IN ({placeholders}) LIMIT 1""",
                (action.chat_id, action.user_id, action.id, *family),
            )
        return row is not None

    async def mute_pending(self, chat_id: int, user_id: int) -> bool:
        async with self.session() as connection:
            row = await one(
                connection,
                """SELECT 1 FROM actions WHERE chat_id = ? AND user_id = ?
                   AND action = 'mute' AND status = 'pending' LIMIT 1""",
                (chat_id, user_id),
            )
        return row is not None

    async def defer_action(self, action: Action, retry_at: int, detail: str) -> None:
        async with self.session(write=True) as connection:
            await execute(
                connection,
                """UPDATE actions SET retry_at = ?, detail = ?, attempts = attempts + 1
                   WHERE id = ? AND status = 'pending'""",
                (retry_at, detail, action.id),
            )

    async def finish_action(
        self, action: Action, status: str, detail: str = ""
    ) -> None:
        async with self.session(write=True) as connection:
            changed = await execute(
                connection,
                "UPDATE actions SET status = ?, detail = ? WHERE id = ? AND status = 'pending'",
                (status, detail, action.id),
            )
            if changed != 1 or status != "applied":
                return
            if action.action in {"mute", "ban"}:
                await execute(
                    connection,
                    """INSERT INTO restrictions
                       (chat_id, user_id, kind, action_id, ends_at, telegram_until, retry_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT (chat_id, user_id, kind) DO UPDATE SET
                           action_id = excluded.action_id, ends_at = excluded.ends_at,
                           telegram_until = excluded.telegram_until, retry_at = excluded.retry_at,
                           attempts = 0""",
                    (
                        action.chat_id,
                        action.user_id,
                        action.action,
                        action.id,
                        action.ends_at,
                        action.ends_at or 0,
                        action.ends_at or 0,
                    ),
                )
            elif action.action in {"unmute", "unban"}:
                await execute(
                    connection,
                    "DELETE FROM restrictions WHERE chat_id = ? AND user_id = ? AND kind = ?",
                    (action.chat_id, action.user_id, action.action[2:]),
                )

    async def due_restrictions(self, now: int) -> list[Restriction]:
        async with self.session() as connection:
            rows = await connection.execute_fetchall(
                "SELECT * FROM restrictions WHERE ends_at IS NOT NULL AND retry_at <= ? ORDER BY retry_at LIMIT 40",
                (now,),
            )
        return [Restriction(**dict(row)) for row in rows]

    async def restriction(
        self, chat_id: int, user_id: int, kind: str
    ) -> Restriction | None:
        async with self.session() as connection:
            row = await one(
                connection,
                "SELECT * FROM restrictions WHERE chat_id = ? AND user_id = ? AND kind = ?",
                (chat_id, user_id, kind),
            )
        return Restriction(**dict(row)) if row else None

    async def defer_expiry(self, restriction: Restriction, retry_at: int) -> None:
        async with self.session(write=True) as connection:
            await execute(
                connection,
                "UPDATE restrictions SET retry_at = ?, attempts = attempts + 1 WHERE action_id = ?",
                (retry_at, restriction.action_id),
            )

    async def finish_expiry(
        self, restriction: Restriction, now: int, restored: bool
    ) -> None:
        async with self.session(write=True) as connection:
            changed = await execute(
                connection,
                "DELETE FROM restrictions WHERE action_id = ?",
                (restriction.action_id,),
            )
            if changed:
                await self.insert_action(
                    connection,
                    Action(
                        restriction.chat_id,
                        restriction.user_id,
                        None,
                        f"expire_{restriction.kind}",
                        "expiry",
                        "",
                        now,
                        status="applied",
                        detail="restored" if restored else "no_change_needed",
                    ),
                )

    async def add_warning(
        self, action: Action, settings: ChatSettings, *, allow_auto: bool
    ) -> tuple[int, int, Action | None]:
        async with self.session(write=True) as connection:
            log_id = await self.insert_action(
                connection, replace(action, status="applied")
            )
            row = await one(
                connection,
                """INSERT INTO warnings (chat_id, user_id, actor_id, reason, created_at, ends_at)
                   VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
                (
                    action.chat_id,
                    action.user_id,
                    action.actor_id,
                    action.reason,
                    action.created_at,
                    action.ends_at,
                ),
            )
            warn_id = row["id"]
            await execute(
                connection,
                "UPDATE actions SET detail = ? WHERE id = ?",
                (f"#{warn_id}", log_id),
            )
            params = (action.chat_id, action.user_id, action.created_at)
            active = await one(
                connection,
                f"SELECT COUNT(*) FROM warnings WHERE chat_id = ? AND user_id = ? AND {ACTIVE_WARNING}",
                params,
            )
            if not allow_auto or active[0] < settings.warn_limit:
                return warn_id, active[0], None
            batch = await connection.execute_fetchall(
                f"""SELECT id FROM warnings WHERE chat_id = ? AND user_id = ? AND {ACTIVE_WARNING}
                    AND auto_action_id IS NULL ORDER BY id LIMIT ?""",
                (*params, settings.warn_limit),
            )
            if len(batch) < settings.warn_limit:
                return warn_id, active[0], None
            auto = Action(
                action.chat_id,
                action.user_id,
                None,
                "mute",
                "warn_limit",
                "",
                action.created_at,
                action.created_at + settings.warn_mute_seconds,
            )
            auto_id = await self.insert_action(connection, auto)
            placeholders = ", ".join("?" for _ in batch)
            await execute(
                connection,
                f"UPDATE warnings SET auto_action_id = ? WHERE id IN ({placeholders})",
                (auto_id, *(item["id"] for item in batch)),
            )
            return (
                warn_id,
                active[0],
                replace(auto, id=auto_id, retry_at=auto.created_at),
            )

    async def auto_batch_active(self, action_id: int, now: int) -> bool:
        async with self.session() as connection:
            row = await one(
                connection,
                f"SELECT COUNT(*) AS total, SUM({ACTIVE_WARNING}) AS active FROM warnings WHERE auto_action_id = ?",
                (now, action_id),
            )
        return row["total"] > 0 and row["total"] == row["active"]

    async def revoke_warning(self, action: Action, warn_id: int | None) -> int | None:
        async with self.session(write=True) as connection:
            row = await one(
                connection,
                f"""SELECT id FROM warnings WHERE chat_id = ? AND user_id = ? AND {ACTIVE_WARNING}
                    AND (? IS NULL OR id = ?) ORDER BY id DESC LIMIT 1""",
                (action.chat_id, action.user_id, action.created_at, warn_id, warn_id),
            )
            if row is None:
                return None
            warn_id = row["id"]
            await self.insert_action(
                connection, replace(action, status="applied", detail=f"#{warn_id}")
            )
            await execute(
                connection,
                "UPDATE warnings SET revoked_at = ? WHERE id = ?",
                (action.created_at, warn_id),
            )
            return warn_id

    async def warning_target(self, chat_id: int, warn_id: int) -> int | None:
        async with self.session() as connection:
            row = await one(
                connection,
                "SELECT user_id FROM warnings WHERE chat_id = ? AND id = ?",
                (chat_id, warn_id),
            )
        return row["user_id"] if row else None

    async def active_warnings(
        self, chat_id: int, user_id: int, now: int
    ) -> tuple[int, list[int]]:
        async with self.session() as connection:
            row = await one(
                connection,
                f"SELECT COUNT(*) FROM warnings WHERE chat_id = ? AND user_id = ? AND {ACTIVE_WARNING}",
                (chat_id, user_id, now),
            )
            rows = await connection.execute_fetchall(
                f"""SELECT id FROM warnings WHERE chat_id = ? AND user_id = ? AND {ACTIVE_WARNING}
                    ORDER BY id DESC LIMIT 8""",
                (chat_id, user_id, now),
            )
        return row[0], [warning["id"] for warning in rows]

    async def warnings(self, chat_id: int, user_id: int) -> list[Warning]:
        async with self.session() as connection:
            rows = await connection.execute_fetchall(
                "SELECT * FROM warnings WHERE chat_id = ? AND user_id = ? ORDER BY id DESC LIMIT 8",
                (chat_id, user_id),
            )
        return [Warning(**dict(row)) for row in rows]

    async def actions(self, chat_id: int, user_id: int) -> list[Action]:
        async with self.session() as connection:
            rows = await connection.execute_fetchall(
                "SELECT * FROM actions WHERE chat_id = ? AND user_id = ? ORDER BY id DESC LIMIT 8",
                (chat_id, user_id),
            )
        return [Action(**dict(row)) for row in rows]

    async def record_activity(
        self,
        chat_id: int,
        user_id: int,
        display_name: str,
        day: date,
        *,
        streak: bool,
        stats: bool,
        message_id: int | None = None,
    ) -> None:
        async with self.session(write=True) as connection:
            if stats and message_id is not None:
                counted = await execute(
                    connection,
                    """UPDATE recent_messages SET counted = 1
                       WHERE chat_id = ? AND message_id = ? AND counted = 0""",
                    (chat_id, message_id),
                )
                stats = counted == 1
            if not (streak or stats):
                return
            row = await one(
                connection,
                "SELECT * FROM activities WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
            prior = (
                activity_from(row)
                if row
                else Activity(user_id, display_name, None, 0, 0)
            )
            if streak and (prior.last_day is None or day > prior.last_day):
                continuing = prior.last_day == day - timedelta(days=1)
                current = prior.current + 1 if continuing else 1
                prior = replace(
                    prior, last_day=day, current=current, best=max(current, prior.best)
                )
            await execute(
                connection,
                """INSERT INTO activities (chat_id, user_id, display_name, last_day, current, best, messages)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (chat_id, user_id) DO UPDATE SET display_name = excluded.display_name,
                   last_day = excluded.last_day, current = excluded.current, best = excluded.best,
                   messages = activities.messages + excluded.messages""",
                (
                    chat_id,
                    user_id,
                    display_name,
                    prior.last_day.isoformat() if prior.last_day else None,
                    prior.current,
                    prior.best,
                    int(stats),
                ),
            )

    async def activity(self, chat_id: int, user_id: int) -> Activity | None:
        async with self.session() as connection:
            row = await one(
                connection,
                "SELECT * FROM activities WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
        return activity_from(row) if row else None

    async def leaderboard(self, chat_id: int, today: date) -> list[Activity]:
        async with self.session() as connection:
            rows = await connection.execute_fetchall(
                """SELECT * FROM activities WHERE chat_id = ? AND last_day >= ?
                   ORDER BY current DESC, best DESC, user_id ASC LIMIT 10""",
                (chat_id, (today - timedelta(days=1)).isoformat()),
            )
        return [activity_from(row) for row in rows]

    async def stats(self, chat_id: int) -> tuple[int, int]:
        async with self.session() as connection:
            row = await one(
                connection,
                "SELECT COALESCE(SUM(messages), 0), COUNT(*) FROM activities WHERE chat_id = ? AND messages > 0",
                (chat_id,),
            )
        return row[0], row[1]

    async def remember_message(
        self, chat_id: int, message_id: int, thread_id: int, sent_at: int
    ) -> bool:
        async with self.session(write=True) as connection:
            inserted = await execute(
                connection,
                """INSERT INTO recent_messages (chat_id, message_id, thread_id, sent_at)
                   VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                (chat_id, message_id, thread_id, sent_at),
            )
            if inserted:
                await execute(
                    connection,
                    """DELETE FROM recent_messages WHERE chat_id = ? AND message_id < (
                        SELECT message_id FROM recent_messages WHERE chat_id = ?
                        ORDER BY message_id DESC LIMIT 1 OFFSET ?
                    )""",
                    (chat_id, chat_id, RECENT_LIMIT - 1),
                )
        return inserted == 1

    async def purge_candidates(
        self, chat_id: int, command_id: int, thread_id: int, now: int, limit: int
    ) -> list[int]:
        async with self.session() as connection:
            rows = await connection.execute_fetchall(
                """SELECT message_id FROM recent_messages
                   WHERE chat_id = ? AND thread_id = ? AND message_id < ? AND sent_at > ? AND deleted = 0
                   ORDER BY message_id DESC LIMIT ?""",
                (chat_id, thread_id, command_id, now - DELETE_AGE, limit),
            )
        return [row["message_id"] for row in rows]

    async def forget_messages(self, chat_id: int, message_ids: list[int]) -> None:
        async with (
            self.session(write=True) as connection,
            connection.executemany(
                "UPDATE recent_messages SET deleted = 1 WHERE chat_id = ? AND message_id = ?",
                ((chat_id, message_id) for message_id in message_ids),
            ),
        ):
            pass

    async def clean_messages(self, now: int) -> None:
        async with self.session(write=True) as connection:
            await execute(
                connection,
                "DELETE FROM recent_messages WHERE sent_at <= ?",
                (now - DELETE_AGE,),
            )
