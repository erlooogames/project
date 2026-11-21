import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    ChatMember,
    ChatMemberAdministrator,
    ChatMemberRestricted,
    Message,
)

from bot.database import Database

ADMINS = {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}
UNKNOWN_MEMBER = (
    "user not found",
    "member not found",
    "participant_id_invalid",
    "user_id_invalid",
)


class Problem(Exception):
    def __init__(self, ru: str, en: str):
        super().__init__(en)
        self.ru = ru
        self.en = en


@dataclass(frozen=True, slots=True)
class Actor:
    id: int
    is_owner: bool


def is_present(member: ChatMember) -> bool:
    if isinstance(member, ChatMemberRestricted):
        return member.is_member
    return member.status in {*ADMINS, ChatMemberStatus.MEMBER}


def is_muted(member: ChatMember) -> bool:
    return (
        isinstance(member, ChatMemberRestricted)
        and member.is_member
        and not member.can_send_messages
    )


def require_supergroup() -> Problem:
    return Problem(
        "Эта команда работает в супергруппе.", "This command requires a supergroup."
    )


class Access:
    def __init__(self, bot: Bot, db: Database):
        self.bot = bot
        self.db = db
        self.admin_cache: OrderedDict[int, tuple[float, frozenset[int]]] = OrderedDict()
        self.admin_locks = tuple(asyncio.Lock() for _ in range(16))

    async def member(self, chat_id: int, user_id: int) -> ChatMember | None:
        try:
            return await self.bot.get_chat_member(chat_id, user_id)
        except TelegramBadRequest as exc:
            if any(reason in exc.message.lower() for reason in UNKNOWN_MEMBER):
                return None
            raise

    async def moderator(self, message: Message, right: str | None = None) -> Actor:
        user = message.from_user
        if user is None or user.is_bot or message.sender_chat is not None:
            raise Problem(
                "Нужен личный профиль администратора.", "Use your own admin profile."
            )
        if user.id == await self.db.owner_id():
            return Actor(user.id, True)
        member = await self.member(message.chat.id, user.id)
        if member is None or member.status not in ADMINS:
            raise Problem("Команда для администраторов.", "This command is for admins.")
        if (
            isinstance(member, ChatMemberAdministrator)
            and right
            and not getattr(member, right)
        ):
            raise Problem(
                "У вас нет нужного права администратора.",
                "You lack the required admin permission.",
            )
        return Actor(user.id, False)

    async def bot_admin(self, chat_id: int, right: str | None = None) -> None:
        member = await self.member(chat_id, self.bot.id)
        if member is None or member.status not in ADMINS:
            raise Problem(
                "Сделайте бота администратором.", "Make the bot an admin first."
            )
        if (
            isinstance(member, ChatMemberAdministrator)
            and right
            and not getattr(member, right)
        ):
            missing = {
                "can_restrict_members": ("ограничивать участников", "restrict members"),
                "can_delete_messages": ("удалять сообщения", "delete messages"),
            }
            ru, en = missing[right]
            raise Problem(
                f"Боту нужно право {ru}.", f"The bot needs permission to {en}."
            )

    async def target(
        self, chat_id: int, user_id: int, actor: Actor | None, *, local_only: bool
    ) -> ChatMember | None:
        if user_id in {self.bot.id, await self.db.owner_id()}:
            raise Problem("Этот пользователь защищён.", "This user is protected.")
        member = await self.member(chat_id, user_id)
        if member is None:
            return None
        if member.status in ADMINS:
            owner_warn = actor is not None and actor.is_owner and local_only
            if member.status == ChatMemberStatus.CREATOR or not owner_warn:
                raise Problem("Этот администратор защищён.", "This admin is protected.")
        return member

    def invalidate_admins(self, chat_id: int) -> None:
        self.admin_cache.pop(chat_id, None)

    async def is_filter_exempt(self, chat_id: int, user_id: int) -> bool:
        if user_id == await self.db.owner_id():
            return True
        cached = self.admin_cache.get(chat_id)
        if cached is not None and cached[0] > monotonic():
            self.admin_cache.move_to_end(chat_id)
            return user_id in cached[1]
        async with self.admin_locks[chat_id % len(self.admin_locks)]:
            cached = self.admin_cache.get(chat_id)
            if cached is None or cached[0] <= monotonic():
                admins = await self.bot.get_chat_administrators(chat_id)
                cached = (
                    monotonic() + 60,
                    frozenset(admin.user.id for admin in admins),
                )
                self.admin_cache[chat_id] = cached
            self.admin_cache.move_to_end(chat_id)
            if len(self.admin_cache) > 256:
                self.admin_cache.popitem(last=False)
            return user_id in cached[1]
