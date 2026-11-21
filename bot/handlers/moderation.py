from time import time

from aiogram.enums import ChatType
from aiogram.types import Message

from bot.commands import (
    MAX_ID,
    Command,
    bounded_int,
    parse_duration,
    parse_target,
    take_word,
)
from bot.handlers.presentation import (
    compact,
    display_user,
    duration_label,
    local_time,
    reply_user_id,
)
from bot.models import ChatSettings
from bot.runtime import Services
from bot.services.access import Problem

TIMED = {"mute", "ban", "warn"}
MOD_COMMANDS = {*TIMED, "unmute", "unban", "unwarn", "kick"}
HISTORY_COMMANDS = {"warns", "history", "punishments"}
RUSSIAN_ACTIONS = {
    "mute": "мут",
    "unmute": "снятие мута",
    "ban": "бан",
    "unban": "снятие бана",
    "warn": "варн",
    "unwarn": "снятие варна",
    "kick": "кик",
    "expire_mute": "истечение мута",
    "expire_ban": "истечение бана",
}
ENGLISH_ACTIONS = {"expire_mute": "mute expired", "expire_ban": "ban expired"}


async def moderate(
    message: Message, command: Command, settings: ChatSettings, services: Services
) -> str:
    actor = await services.access.moderator(message, "can_restrict_members")
    args = command.args
    duration = None
    if command.name in TIMED:
        duration, args = parse_duration(args)
    reply_id = reply_user_id(message)
    if command.name == "unwarn" and reply_id is None:
        first, _ = take_word(args)
        if first.startswith("#"):
            warn_id = bounded_int(first[1:], 1, MAX_ID)
            reply_id = await services.db.warning_target(message.chat.id, warn_id)
            if reply_id is None:
                raise Problem("Активный варн не найден.", "Active warning not found.")
    target = parse_target(args, reply_id, allow_warn_id=command.name == "unwarn")
    result = await services.moderation.issue(
        message.chat.id,
        message.chat.type == ChatType.SUPERGROUP,
        actor,
        target,
        command.name,
        duration,
        message.message_id,
        settings,
    )
    user = display_user(result.user, target.user_id)
    label = duration_label(duration, settings)
    if result.status == "pending":
        return settings.text(
            "Telegram задержал действие. Повторю в фоне.",
            "Telegram delayed the action. Retrying shortly.",
        )
    if result.status != "applied":
        raise Problem(
            "Telegram не выполнил действие.", "Telegram could not perform the action."
        )
    if command.name == "warn":
        number = f"{result.warning_count}/{settings.warn_limit}"
        text = settings.text(
            f"⚠️ {user} — предупреждение {number} (#{result.warn_id})",
            f"⚠️ {user} — warning {number} (#{result.warn_id})",
        )
        if result.auto_status == "applied":
            mute_for = duration_label(settings.warn_mute_seconds, settings)
            text += settings.text(f" · мут {mute_for}", f" · muted {mute_for}")
        elif result.auto_status == "pending":
            text += settings.text(" · мут в очереди", " · mute pending")
        elif result.auto_status == "failed":
            text += settings.text(" · мут не применён", " · mute failed")
        return text
    if command.name == "unwarn":
        return settings.text(
            f"✅ Варн #{result.warn_id} снят", f"✅ Warning #{result.warn_id} removed"
        )
    replies = {
        "mute": (f"🔇 {user} — мут {label}", f"🔇 {user} — muted {label}"),
        "ban": (f"🔨 {user} — бан {label}", f"🔨 {user} — banned {label}"),
        "unmute": ("✅ Мут снят", "✅ Mute lifted"),
        "unban": ("✅ Бан снят", "✅ Ban lifted"),
        "kick": (f"✅ {user} — исключён", f"✅ {user} — kicked"),
    }
    return settings.text(*replies[command.name])


async def history(
    message: Message, command: Command, settings: ChatSettings, services: Services
) -> str:
    user = message.from_user
    if user is None or user.is_bot or message.sender_chat is not None:
        raise Problem("Нужен личный профиль.", "Use your own profile.")
    if command.name != "warns" and not settings.history_enabled:
        raise Problem("История отключена.", "History is off.")
    target = parse_target(
        command.args, reply_user_id(message), default_id=user.id, allow_reason=False
    )
    if target.user_id != user.id:
        await services.access.moderator(message)

    chat_id, user_id = message.chat.id, target.user_id
    db = services.db
    now = int(time())
    if command.name == "warns":
        count, ids = await db.active_warnings(chat_id, user_id, now)
        number = f"{count}/{settings.warn_limit}"
        listing = ", ".join(f"#{warn_id}" for warn_id in ids)
        text = settings.text(
            f"⚠️ Активные варны {number}", f"⚠️ Active warnings {number}"
        )
        return f"{text} · {listing}" if listing else text
    if command.name == "history":
        rows = await db.warnings(chat_id, user_id)
        if not rows:
            return settings.text("Предупреждений нет.", "No warnings.")
        lines = []
        for warning in rows:
            if warning.revoked_at is not None:
                state = settings.text("снят", "removed")
            elif not warning.is_active(now):
                state = settings.text("истёк", "expired")
            elif warning.ends_at is None:
                state = settings.text("навсегда", "permanent")
            else:
                when = local_time(warning.ends_at, settings)
                state = settings.text(f"до {when}", f"until {when}")
            detail = f" — {compact(warning.reason)}" if warning.reason else ""
            lines.append(f"#{warning.id} · {state}{detail}")
        return "\n".join(lines)

    rows = await db.actions(chat_id, user_id)
    if not rows:
        return settings.text("Наказаний нет.", "No moderation actions.")
    states = {
        "pending": ("в очереди", "pending"),
        "failed": ("ошибка", "failed"),
        "skipped": ("пропущено", "skipped"),
    }
    lines = []
    for action in rows:
        kind = settings.text(
            RUSSIAN_ACTIONS[action.action],
            ENGLISH_ACTIONS.get(action.action, action.action),
        )
        status = (
            f" · {settings.text(*states[action.status])}"
            if action.status in states
            else ""
        )
        when = local_time(action.created_at, settings)
        detail = f" — {compact(action.reason)}" if action.reason else ""
        lines.append(f"{when} · {kind}{status}{detail}")
    return "\n".join(lines)


def moderation_usage(command: str, settings: ChatSettings) -> str:
    localized = {
        "mute": "мут",
        "unmute": "анмут",
        "ban": "бан",
        "unban": "разбан",
        "warn": "варн",
        "unwarn": "снять варн",
        "kick": "кик",
        "warns": "варны",
        "history": "история",
        "punishments": "наказания",
    }
    name = settings.text(localized[command], command)
    duration = (
        settings.text("<м/д/н N|навсегда> ", "<m/d/w N|perm> ")
        if command in TIMED
        else ""
    )
    warn = " [#ID]" if command == "unwarn" else ""
    reason = settings.text(" [причина]", " [reason]") if command in MOD_COMMANDS else ""
    prompt = settings.text(
        "ID или ответ на сообщение.", "Use an ID or reply to a message."
    )
    return f"!{name} {duration}[ID]{warn}{reason}\n{prompt}"
