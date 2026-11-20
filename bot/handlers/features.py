import re
from datetime import UTC, datetime
from html import escape
from time import time

from aiogram.types import Message

from bot.commands import Command, UsageError, bounded_int, parse_target
from bot.handlers.presentation import (
    Reply,
    day_count,
    display_user,
    reply_user_id,
    thread_id,
)
from bot.models import ChatSettings
from bot.runtime import Services
from bot.services.access import Problem

HELP_RU = """Модерация · ответом или по Telegram ID
!мут / !mute · !бан / !ban · !варн / !warn <срок> [ID] [причина]
!анмут / !unmute · !разбан / !unban · !кик / !kick [ID] [причина]
!снять варн / !unwarn [ID] [#варн] [причина]
!варны / !warns · !история / !history · !наказания / !punishments [ID]
Сроки м — минуты, д — дни, н — недели. Навсегда — perm.
Примеры !мут м 30 · !ban д 7 123456789 · !unwarn #12

!антиспам / !antispam вкл|выкл|лимит 5 10|мут м 30
!ссылки / !links вкл|выкл · !очистить / !purge N
!приветствие / !welcome вкл|выкл|текст …
!огонёк / !streak [ID] · !огоньки / !streaks
!музыка / !music запрос · !статистика / !stats
!настройки / !settings · !помощь / !help
Настройки · язык, пояс, лимит, варнмут, история, очистка, статистика, музыка, огоньки, команды."""

HELP_EN = """Moderation · reply to a message or use a Telegram ID
!mute · !ban · !warn <duration> [ID] [reason]
!unmute · !unban · !kick [ID] [reason]
!unwarn [ID] [#warning] [reason]
!warns · !history · !punishments [ID]
Units m/м minutes, d/д days, w/н weeks. Use perm for permanent.
Examples !mute m 30 · !ban d 7 123456789 · !unwarn #12

!antispam on|off|limit 5 10|mute m 30
!links on|off · !purge N · !welcome on|off|text …
!streak [ID] · !streaks · !music query · !stats
!settings · !help
Settings · language, timezone, warnlimit, warnmute, history, purge, stats, music, streaks, deletecommands.
Russian command aliases also work."""


async def feature(
    message: Message, command: Command, settings: ChatSettings, services: Services
) -> Reply:
    chat_id = message.chat.id
    user = message.from_user
    db = services.db
    if command.name == "help":
        return Reply(settings.text(HELP_RU, HELP_EN))
    if command.name == "purge":
        return Reply(await purge(message, command, settings, services))
    if command.name == "music":
        if not settings.music_enabled:
            raise Problem("Поиск музыки отключён.", "Music search is off.")
        if not 1 <= len(command.args) <= 160:
            raise UsageError
        track = await services.music.search(command.args, user.id)
        if track is None:
            return Reply(settings.text("Трек не найден.", "No track found."))
        listen = f'<a href="{escape(track.url, quote=True)}">Apple Music / iTunes</a>'
        if track.preview:
            preview = settings.text("Превью", "Preview")
            listen += f' · <a href="{escape(track.preview, quote=True)}">{preview}</a>'
        return Reply(
            f"🎵 {escape(track.artist)} — {escape(track.title)}\n{listen}", "HTML"
        )
    if command.name == "stats":
        if not settings.stats_enabled:
            raise Problem("Статистика отключена.", "Stats are off.")
        if command.args:
            raise UsageError
        messages, participants = await db.stats(chat_id)
        return Reply(
            settings.text(
                f"Сообщений {messages} · участников {participants}",
                f"Messages {messages} · participants {participants}",
            )
        )

    if not settings.streaks_enabled:
        raise Problem("Огоньки отключены.", "Streaks are off.")
    today = settings.day(datetime.now(UTC))
    if command.name == "streaks":
        if command.args:
            raise UsageError
        rows = await db.leaderboard(chat_id, today)
        if not rows:
            return Reply(settings.text("Активных огоньков нет.", "No active streaks."))
        lines = [settings.text("🔥 Огоньки", "🔥 Streaks")]
        for index, row in enumerate(rows, 1):
            days = day_count(row.streak(today), settings)
            lines.append(f"{index}. {row.display_name} — {days}")
        return Reply("\n".join(lines))

    target = parse_target(
        command.args, reply_user_id(message), default_id=user.id, allow_reason=False
    )
    row = await db.activity(chat_id, target.user_id)
    current = day_count(row.streak(today) if row else 0, settings)
    best = day_count(row.best if row else 0, settings)
    text = settings.text(
        f"🔥 Огонёк: {current}\n🏆 Рекорд: {best}",
        f"🔥 Streak: {current}\n🏆 Best: {best}",
    )
    if target.user_id != user.id:
        display_name = row.display_name if row else str(target.user_id)
        text = f"{display_name}\n{text}"
    return Reply(text)


async def purge(
    message: Message, command: Command, settings: ChatSettings, services: Services
) -> str:
    if not settings.purge_enabled:
        raise Problem("Очистка отключена.", "Purge is off.")
    await services.access.moderator(message, "can_delete_messages")
    count = bounded_int(command.args, 1, 100)
    chat_id = message.chat.id
    await services.access.bot_admin(chat_id, "can_delete_messages")
    ids = await services.db.purge_candidates(
        chat_id, message.message_id, thread_id(message), int(time()), count
    )
    if not ids:
        raise Problem(
            "Нет доступных сообщений для удаления.", "No eligible messages to delete."
        )
    if not await services.access.bot.delete_messages(chat_id, ids):
        raise Problem(
            "Telegram не выполнил удаление.", "Telegram could not delete the messages."
        )
    await services.db.forget_messages(chat_id, ids)
    return settings.text(
        f"✅ Очистка до {len(ids)} сообщ.", f"✅ Purged up to {len(ids)} messages."
    )


def feature_usage(command: str, settings: ChatSettings) -> str:
    options = {
        "music": ("!музыка <название>", "!music <song>"),
        "purge": ("!очистить <1–100>", "!purge <1–100>"),
        "streak": ("!огонёк [ID]", "!streak [ID]"),
        "streaks": ("!огоньки", "!streaks"),
        "stats": ("!статистика", "!stats"),
    }
    return settings.text(*options[command])


def welcome_text(message: Message, settings: ChatSettings) -> str | None:
    names = [
        display_user(user, user.id)
        for user in (message.new_chat_members or [])
        if not user.is_bot
    ]
    if not names:
        return None
    values = {"name": ", ".join(names[:8]), "chat": message.chat.title or ""}
    template = settings.welcome_text or settings.text(
        "Добро пожаловать, {name}!", "Welcome, {name}!"
    )
    return re.sub(r"\{(name|chat)\}", lambda match: values[match[1]], template)[:800]
