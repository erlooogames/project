import logging
from time import time

from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from bot.models import ChatSettings
from bot.services.access import ADMINS, Access, Problem
from bot.services.moderation import Moderation
from bot.services.spam import SpamMonitor, contains_link, fingerprint_for

logger = logging.getLogger(__name__)


class GroupFilters:
    def __init__(self, bot: Bot, access: Access, moderation: Moderation):
        self.bot = bot
        self.access = access
        self.moderation = moderation
        self.spam = SpamMonitor()

    async def inspect(
        self, message: Message, settings: ChatSettings
    ) -> tuple[bool, bool, bool]:
        """Return whether to block handling, skip activity, and announce a new mute."""
        user = message.from_user
        if user is None or user.is_bot or message.sender_chat is not None:
            return False, True, False
        check_rate = (
            settings.antispam_enabled
            or settings.streaks_enabled
            or settings.stats_enabled
        )
        check_link = settings.links_enabled and contains_link(message)
        if not (check_rate or check_link) or await self.exempt(
            message.chat.id, user.id
        ):
            return False, False, False

        duplicate = False
        reason = None
        sent_at = message.date.timestamp()
        if check_rate and sent_at > time() - settings.spam_window:
            result = self.spam.observe(
                message.chat.id,
                user.id,
                sent_at,
                fingerprint_for(message),
                settings,
                message_id=message.message_id,
            )
            reason, duplicate = result.violation, result.duplicate

        announce = False
        if (
            reason is not None
            and settings.antispam_enabled
            and message.chat.type == ChatType.SUPERGROUP
            and self.spam.should_enforce(message.chat.id, user.id)
        ):
            try:
                status = await self.moderation.mute_spammer(
                    message.chat.id,
                    user.id,
                    settings.spam_mute_seconds,
                    reason,
                )
                announce = status == "applied"
            except (Problem, TelegramAPIError) as exc:
                logger.debug(
                    "Antispam action skipped in chat %s (%s)",
                    message.chat.id,
                    type(exc).__name__,
                )

        if check_link:
            removed = await self.delete_link(message)
            if removed:
                return True, True, announce
        if reason and settings.antispam_enabled:
            return True, True, announce
        return False, bool(reason) or duplicate, announce

    async def inspect_edit(self, message: Message, settings: ChatSettings) -> bool:
        user = message.from_user
        if (
            settings.links_enabled
            and user is not None
            and not user.is_bot
            and message.sender_chat is None
            and contains_link(message)
            and not await self.exempt(message.chat.id, user.id)
        ):
            return await self.delete_link(message)
        return False

    async def exempt(self, chat_id: int, user_id: int) -> bool:
        try:
            return await self.access.is_filter_exempt(chat_id, user_id)
        except TelegramAPIError as exc:
            logger.debug(
                "Could not verify group admins in chat %s (%s)",
                chat_id,
                type(exc).__name__,
            )
            return True

    async def delete_link(self, message: Message) -> bool:
        chat_id, user_id = message.chat.id, message.from_user.id
        try:
            member = await self.access.member(chat_id, user_id)
            if (
                member is None
                or member.status in ADMINS
                or user_id == await self.access.db.owner_id()
            ):
                return False
            await self.access.bot_admin(chat_id, "can_delete_messages")
            deleted = await self.bot.delete_message(chat_id, message.message_id)
            if deleted:
                await self.access.db.forget_messages(chat_id, [message.message_id])
            return deleted
        except (Problem, TelegramAPIError) as exc:
            logger.debug(
                "Could not delete a link in chat %s (%s)", chat_id, type(exc).__name__
            )
            return False
