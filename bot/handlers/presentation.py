from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram.types import Message, User

from bot.models import ChatSettings


@dataclass(frozen=True, slots=True)
class Reply:
    text: str
    parse_mode: str | None = None


def reply_user_id(message: Message) -> int | None:
    reply = message.reply_to_message
    if reply is not None and reply.sender_chat is None and reply.from_user is not None:
        return reply.from_user.id
    return None


def display_user(user: User | None, user_id: int) -> str:
    if user is None:
        return str(user_id)
    if user.username:
        return f"@{user.username}"
    return " ".join(user.full_name.split())[:64] or str(user_id)


def duration_label(seconds: int | None, settings: ChatSettings) -> str:
    if seconds is None:
        return settings.text("навсегда", "permanent")
    if seconds % 86400 == 0:
        return settings.text(f"{seconds // 86400} дн", f"{seconds // 86400}d")
    return settings.text(f"{seconds // 60} мин", f"{seconds // 60} min")


def day_count(count: int, settings: ChatSettings) -> str:
    if settings.language == "en":
        return f"{count} day" if count == 1 else f"{count} days"
    mod = count % 10
    if 11 <= count % 100 <= 14:
        word = "дней"
    elif mod == 1:
        word = "день"
    elif 2 <= mod <= 4:
        word = "дня"
    else:
        word = "дней"
    return f"{count} {word}"


def local_time(timestamp: int, settings: ChatSettings) -> str:
    return (
        datetime.fromtimestamp(timestamp, UTC)
        .astimezone(ZoneInfo(settings.timezone))
        .strftime("%d.%m.%Y %H:%M")
    )


def compact(text: str) -> str:
    return " ".join(text.split())[:100]


def thread_id(message: Message) -> int:
    return message.message_thread_id or 0
