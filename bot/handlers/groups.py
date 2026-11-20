import logging
from time import time

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramRetryAfter,
)
from aiogram.types import ChatMemberUpdated, LinkPreviewOptions, Message

from bot.commands import Command, UsageError, parse_command
from bot.database import DELETE_AGE, DuplicateCommand
from bot.handlers.features import feature, feature_usage, welcome_text
from bot.handlers.moderation import (
    HISTORY_COMMANDS,
    MOD_COMMANDS,
    history,
    moderate,
    moderation_usage,
)
from bot.handlers.presentation import Reply, display_user, duration_label, thread_id
from bot.handlers.settings import configure, settings_usage
from bot.models import ChatSettings
from bot.runtime import Services
from bot.services.access import ADMINS, Problem
from bot.services.spam import is_content, is_meaningful

logger = logging.getLogger(__name__)
GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
CONFIG_COMMANDS = {"settings", "antispam", "links", "welcome"}


def group_router() -> Router:
    router = Router(name="groups")
    router.message.filter(F.chat.type.in_(GROUP_TYPES))
    router.edited_message.filter(F.chat.type.in_(GROUP_TYPES))
    router.message.register(on_message)
    router.edited_message.register(on_edit)
    router.chat_member.register(on_member_change)
    router.my_chat_member.register(on_bot_member_change)
    return router


async def on_message(message: Message, services: Services) -> None:
    chat_id = message.chat.id
    if message.migrate_to_chat_id or message.migrate_from_chat_id:
        previous = message.migrate_from_chat_id or chat_id
        successor = message.migrate_to_chat_id or chat_id
        await services.db.migrate_group(previous, successor)
        services.access.invalidate_admins(previous)
        return
    if not message.new_chat_members and not is_content(message):
        return
    if message.chat.type == ChatType.GROUP and await services.db.group_replaced(
        chat_id
    ):
        return
    settings = await services.db.settings(chat_id)
    if message.new_chat_members:
        if settings.welcome_enabled and (text := welcome_text(message, settings)):
            await send(message, Reply(text), settings, services)
        return

    user = message.from_user
    sent_at = int(message.date.timestamp())
    if (
        settings.purge_enabled or settings.stats_enabled
    ) and sent_at > time() - DELETE_AGE:
        await services.db.remember_message(
            chat_id, message.message_id, thread_id(message), sent_at
        )
    if user is None or user.is_bot:
        return

    blocked, skip_activity, muted = await services.filters.inspect(message, settings)
    if muted:
        label = duration_label(settings.spam_mute_seconds, settings)
        name = display_user(user, user.id)
        await send(
            message,
            Reply(
                settings.text(
                    f"🔇 {name} — мут {label} · спам",
                    f"🔇 {name} — muted {label} · spam",
                )
            ),
            settings,
            services,
        )
    if blocked:
        return

    command = parse_command(message.text)
    if command is not None:
        await handle_command(message, command, settings, services)
    elif (
        not skip_activity
        and (settings.streaks_enabled or settings.stats_enabled)
        and is_meaningful(message)
    ):
        await services.db.record_activity(
            chat_id,
            user.id,
            display_user(user, user.id),
            settings.day(message.date),
            streak=settings.streaks_enabled,
            stats=settings.stats_enabled,
            message_id=message.message_id,
        )


async def on_edit(message: Message, services: Services) -> None:
    settings = await services.db.settings(message.chat.id)
    await services.filters.inspect_edit(message, settings)


async def on_member_change(event: ChatMemberUpdated, services: Services) -> None:
    if (event.old_chat_member.status in ADMINS) != (
        event.new_chat_member.status in ADMINS
    ):
        services.access.invalidate_admins(event.chat.id)


async def on_bot_member_change(event: ChatMemberUpdated, services: Services) -> None:
    services.access.invalidate_admins(event.chat.id)


async def handle_command(
    message: Message, command: Command, settings: ChatSettings, services: Services
) -> None:
    if message.sender_chat is not None:
        await send(
            message,
            Reply(settings.text("Нужен личный профиль.", "Use your own profile.")),
            settings,
            services,
        )
        return
    if not command.name:
        if message.text.strip() == "!":
            return
        await send(
            message,
            Reply(
                settings.text("Неизвестная команда. !помощь", "Unknown command. !help")
            ),
            settings,
            services,
        )
        return

    success = False
    try:
        if command.name in MOD_COMMANDS:
            reply = Reply(await moderate(message, command, settings, services))
        elif command.name in HISTORY_COMMANDS:
            reply = Reply(await history(message, command, settings, services))
        elif command.name in CONFIG_COMMANDS:
            reply = Reply(await configure(message, command, settings, services))
            if command.name == "settings":
                settings = await services.db.settings(message.chat.id)
        else:
            reply = await feature(message, command, settings, services)
        success = True
    except DuplicateCommand:
        return
    except Problem as exc:
        reply = Reply(settings.text(exc.ru, exc.en))
    except UsageError:
        if command.name in MOD_COMMANDS | HISTORY_COMMANDS:
            hint = moderation_usage(command.name, settings)
        elif command.name in CONFIG_COMMANDS:
            hint = settings_usage(command.name, settings)
        else:
            hint = feature_usage(command.name, settings)
        reply = Reply(hint)
    except TelegramAPIError as exc:
        logger.warning(
            "Command %s in chat %s failed (%s)",
            command.name,
            message.chat.id,
            type(exc).__name__,
        )
        reply = Reply(api_error(exc, settings))

    if success and settings.delete_commands:
        try:
            deleted = await message.delete()
        except TelegramAPIError:
            deleted = False
        if deleted and settings.purge_enabled:
            await services.db.forget_messages(message.chat.id, [message.message_id])
    await send(message, reply, settings, services)


def api_error(exc: TelegramAPIError, settings: ChatSettings) -> str:
    if isinstance(exc, TelegramRetryAfter):
        return settings.text(
            f"Telegram просит подождать {exc.retry_after} сек.",
            f"Telegram asks to wait {exc.retry_after}s.",
        )
    if isinstance(exc, TelegramMigrateToChat):
        return settings.text(
            "Чат стал супергруппой. Повторите команду.",
            "The chat became a supergroup. Retry there.",
        )
    if isinstance(exc, TelegramForbiddenError):
        return settings.text(
            "Боту недоступен чат или действие.",
            "The bot cannot access this chat or action.",
        )
    if isinstance(exc, TelegramBadRequest):
        return settings.text(
            "Telegram отклонил действие.", "Telegram rejected the action."
        )
    return settings.text(
        "Telegram недоступен. Попробуйте позже.",
        "Telegram is unavailable. Try again later.",
    )


async def send(
    message: Message, reply: Reply, settings: ChatSettings, services: Services
) -> None:
    try:
        sent = await message.answer(
            reply.text,
            parse_mode=reply.parse_mode,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except TelegramAPIError as exc:
        logger.warning(
            "Could not send a reply in chat %s (%s)",
            message.chat.id,
            type(exc).__name__,
        )
        return
    if settings.purge_enabled:
        await services.db.remember_message(
            sent.chat.id,
            sent.message_id,
            thread_id(message),
            int(sent.date.timestamp()),
        )
