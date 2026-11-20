import asyncio
import logging
from dataclasses import dataclass
from time import time

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import (
    ChatMember,
    ChatMemberBanned,
    ChatMemberRestricted,
    ChatPermissions,
    User,
)

from bot.commands import Target
from bot.database import Database
from bot.middlewares.telegram import TimedActionPast
from bot.models import Action, ChatSettings, Restriction
from bot.services.access import (
    Access,
    Actor,
    Problem,
    is_muted,
    is_present,
    require_supergroup,
)

logger = logging.getLogger(__name__)
MUTE_FAMILY = ("mute", "unmute")
BAN_FAMILY = ("ban", "unban", "kick")
SENDING = tuple(
    name
    for name in ChatPermissions.model_fields
    if name.startswith("can_send_") or name == "can_add_web_page_previews"
)
UNRESTRICTED = ChatPermissions(**dict.fromkeys(ChatPermissions.model_fields, True))


@dataclass(frozen=True, slots=True)
class Result:
    user: User | None
    status: str = "applied"
    warn_id: int | None = None
    warning_count: int = 0
    auto_status: str | None = None


class Moderation:
    def __init__(self, bot: Bot, db: Database, access: Access):
        self.bot = bot
        self.db = db
        self.access = access
        self.locks = tuple(asyncio.Lock() for _ in range(64))

    def lock_for(self, chat_id: int, user_id: int) -> asyncio.Lock:
        return self.locks[hash((chat_id, user_id)) % len(self.locks)]

    async def issue(
        self,
        chat_id: int,
        is_supergroup: bool,
        actor: Actor,
        target: Target,
        kind: str,
        duration: int | None,
        message_id: int,
        settings: ChatSettings,
    ) -> Result:
        local = kind in {"warn", "unwarn"}
        if not local and not is_supergroup:
            raise require_supergroup()
        async with self.lock_for(chat_id, target.user_id):
            await self.access.bot_admin(
                chat_id, None if local else "can_restrict_members"
            )
            member = await self.access.target(
                chat_id, target.user_id, actor, local_only=local
            )
            user = member.user if member else None
            now = int(time())
            action = Action(
                chat_id,
                target.user_id,
                actor.id,
                kind,
                "manual",
                target.reason,
                now,
                now + duration if duration else None,
                message_id,
            )
            if kind == "unwarn":
                warn_id = await self.db.revoke_warning(action, target.warn_id)
                if warn_id is None:
                    raise Problem(
                        "Активный варн не найден.", "Active warning not found."
                    )
                return Result(user, warn_id=warn_id)

            validate_member(kind, member)
            if kind == "warn":
                warn_id, count, auto = await self.db.add_warning(
                    action, settings, allow_auto=is_supergroup
                )
                status = None
                if auto is not None:
                    status = await self.deliver(auto, recover=True)
                return Result(
                    user, warn_id=warn_id, warning_count=count, auto_status=status
                )

            action = await self.db.create_action(action)
            status = await self.deliver(action)
            return Result(user, status)

    async def mute_spammer(
        self, chat_id: int, user_id: int, duration: int, reason: str
    ) -> str:
        async with self.lock_for(chat_id, user_id):
            await self.access.bot_admin(chat_id, "can_restrict_members")
            member = await self.access.target(chat_id, user_id, None, local_only=False)
            validate_member("mute", member)
            if is_muted(member) or await self.db.mute_pending(chat_id, user_id):
                return "skipped"
            now = int(time())
            action = await self.db.create_action(
                Action(
                    chat_id,
                    user_id,
                    None,
                    "mute",
                    "antispam",
                    reason,
                    now,
                    now + duration,
                )
            )
            return await self.deliver(action)

    async def deliver(self, action: Action, *, recover: bool = False) -> str:
        try:
            if recover:
                resolved = await self.check_before_retry(action)
                if resolved:
                    return await self.finish(action, *resolved)

            if action.ends_at is not None and action.ends_at - time() < 35:
                raise TimedActionPast
            if not await self.call_telegram(action):
                return await self.finish(action, "failed", "telegram_returned_false")
        except TimedActionPast:
            return await self.finish(action, "failed", "duration_elapsed")
        except (TelegramRetryAfter, TelegramNetworkError, TelegramServerError) as exc:
            if action.action == "kick" or action.attempts >= 8:
                return await self.finish(action, "failed", type(exc).__name__)
            delay = (
                exc.retry_after + 1
                if isinstance(exc, TelegramRetryAfter)
                else min(300, 5 * 2**action.attempts)
            )
            await self.db.defer_action(action, int(time()) + delay, type(exc).__name__)
            logger.warning(
                "Moderation action %s deferred for %ss (%s)",
                action.id,
                delay,
                type(exc).__name__,
            )
            return "pending"
        except Problem as exc:
            return await self.finish(action, "skipped", exc.en)
        except (
            TelegramBadRequest,
            TelegramForbiddenError,
            TelegramMigrateToChat,
        ) as exc:
            logger.warning(
                "Telegram rejected moderation action %s: %s", action.id, exc.message
            )
            return await self.finish(action, "failed", type(exc).__name__)
        return await self.finish(action, "applied")

    async def finish(self, action: Action, status: str, detail: str = "") -> str:
        await self.db.finish_action(action, status, detail)
        return status

    async def check_before_retry(self, action: Action) -> tuple[str, str] | None:
        family = MUTE_FAMILY if action.action in MUTE_FAMILY else BAN_FAMILY
        if await self.db.newer_action_exists(action, family):
            return "skipped", "superseded"
        await self.access.bot_admin(action.chat_id, "can_restrict_members")
        member = await self.access.target(
            action.chat_id, action.user_id, None, local_only=False
        )
        if restriction_matches(member, action.action, action.ends_at or 0):
            return "applied", "already_applied"
        if action.action == "kick":
            # A second kick could remove someone who rejoined after the first request.
            if member is not None and member.status == ChatMemberStatus.LEFT:
                return "applied", "already_left"
            return "failed", "kick_result_uncertain"
        if member is not None:
            if action.action == "unmute" and not isinstance(
                member, ChatMemberRestricted
            ):
                return "applied", "already_unmuted"
            if action.action == "unban" and member.status != ChatMemberStatus.KICKED:
                return "applied", "already_unbanned"
        if action.source == "warn_limit" and not await self.db.auto_batch_active(
            action.id, int(time())
        ):
            return "skipped", "warning_batch_inactive"
        if action.source != "manual" and member is not None and is_muted(member):
            return "skipped", "already_muted"
        validate_member(action.action, member)
        return None

    async def call_telegram(self, action: Action) -> bool:
        chat_id, user_id = action.chat_id, action.user_id
        if action.action == "mute":
            chat = await self.bot.get_chat(chat_id)
            permissions = chat.permissions or ChatPermissions()
            muted = permissions.model_copy(update=dict.fromkeys(SENDING, False))
            return await self.bot.restrict_chat_member(
                chat_id,
                user_id,
                muted,
                use_independent_chat_permissions=True,
                until_date=action.ends_at,
            )
        if action.action == "ban":
            return await self.bot.ban_chat_member(
                chat_id, user_id, until_date=action.ends_at
            )
        if action.action == "unmute":
            return await self.unmute(chat_id, user_id)
        return await self.bot.unban_chat_member(
            chat_id, user_id, only_if_banned=action.action == "unban"
        )

    async def unmute(self, chat_id: int, user_id: int) -> bool:
        return await self.bot.restrict_chat_member(
            chat_id,
            user_id,
            UNRESTRICTED,
            use_independent_chat_permissions=True,
        )

    async def expire(self, restriction: Restriction) -> None:
        chat_id, user_id = restriction.chat_id, restriction.user_id
        async with self.lock_for(chat_id, user_id):
            current = await self.db.restriction(chat_id, user_id, restriction.kind)
            if current is None or current.action_id != restriction.action_id:
                return
            try:
                member = await self.access.member(chat_id, user_id)
                restored = restriction_matches(
                    member, restriction.kind, restriction.telegram_until
                )
                if restored:
                    await self.access.bot_admin(chat_id, "can_restrict_members")
                    if restriction.kind == "mute":
                        ok = await self.unmute(chat_id, user_id)
                    else:
                        ok = await self.bot.unban_chat_member(
                            chat_id, user_id, only_if_banned=True
                        )
                    if not ok:
                        raise Problem(
                            "Telegram не снял наказание.",
                            "Telegram did not lift the restriction.",
                        )
            except (Problem, TelegramAPIError) as exc:
                delay = min(3600, 15 * 2 ** min(current.attempts, 8))
                if isinstance(exc, TelegramRetryAfter):
                    delay = max(delay, exc.retry_after + 1)
                await self.db.defer_expiry(current, int(time()) + delay)
                if current.attempts == 0:
                    logger.warning(
                        "Could not expire action %s: %s",
                        current.action_id,
                        type(exc).__name__,
                    )
                return
            await self.db.finish_expiry(current, int(time()), restored)

    async def maintain_once(self) -> None:
        for restriction in await self.db.due_restrictions(int(time())):
            await self.expire(restriction)
        for pending in await self.db.due_actions(int(time())):
            async with self.lock_for(pending.chat_id, pending.user_id):
                action = await self.db.action(pending.id)
                if action and action.status == "pending" and action.retry_at <= time():
                    await self.deliver(action, recover=True)

    async def maintain(self) -> None:
        next_cleanup = 0
        while True:
            try:
                await self.maintain_once()
                now = int(time())
                if now >= next_cleanup:
                    await self.db.clean_messages(now)
                    next_cleanup = now + 300
            except Exception:
                logger.exception("Moderation maintenance failed")
            await asyncio.sleep(15)


def validate_member(kind: str, member: ChatMember | None) -> None:
    if member is None:
        raise Problem("Пользователь не найден.", "User not found.")
    if kind in {"mute", "warn", "kick"} and not is_present(member):
        raise Problem("Пользователь не в чате.", "User is not in the chat.")
    if kind == "ban" and member.status == ChatMemberStatus.KICKED:
        raise Problem("У пользователя уже есть бан.", "The user is already banned.")
    if kind == "unban" and member.status != ChatMemberStatus.KICKED:
        raise Problem("Активного бана нет.", "No active ban.")
    if kind == "unmute" and not isinstance(member, ChatMemberRestricted):
        raise Problem("Активного мута нет.", "No active mute.")


def restriction_matches(member: ChatMember | None, kind: str, until: int) -> bool:
    if kind == "ban" and isinstance(member, ChatMemberBanned):
        return int(member.until_date.timestamp()) == until
    if kind == "mute" and isinstance(member, ChatMemberRestricted):
        return (
            not any(getattr(member, field, False) for field in SENDING)
            and int(member.until_date.timestamp()) == until
        )
    return False
