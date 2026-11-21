from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.enums import ChatType
from aiogram.types import Message

from bot.commands import (
    Command,
    UsageError,
    bounded_int,
    normalized,
    parse_duration,
    parse_switch,
    take_word,
)
from bot.handlers.presentation import duration_label
from bot.models import ChatSettings
from bot.runtime import Services
from bot.services.access import require_supergroup

OPTION_NAMES = {
    "лимит": "warnlimit",
    "варнмут": "warnmute",
    "пояс": "timezone",
    "язык": "language",
    "история": "history",
    "очистка": "purge",
    "статистика": "stats",
    "музыка": "music",
    "огоньки": "streaks",
    "команды": "deletecommands",
}
SWITCHABLE = {
    name: f"{name}_enabled"
    for name in ("history", "purge", "stats", "music", "streaks")
}
SWITCHABLE["deletecommands"] = "delete_commands"


def on_off(value: bool, settings: ChatSettings) -> str:
    return settings.text("вкл", "on") if value else settings.text("выкл", "off")


async def configure(
    message: Message, command: Command, settings: ChatSettings, services: Services
) -> str:
    await services.access.moderator(message)
    chat_id = message.chat.id
    word, rest = take_word(command.args)
    option = normalized(word)
    db = services.db

    if command.name == "antispam":
        if not option:
            return spam_status(settings)
        if option in {"лимит", "limit"}:
            values = rest.split()
            if len(values) != 2:
                raise UsageError
            limit = bounded_int(values[0], 1, 100)
            window = bounded_int(values[1], 1, 600)
            settings = await db.update_settings(
                chat_id, spam_limit=limit, spam_window=window
            )
        elif option in {"мут", "mute"}:
            duration, extra = parse_duration(rest, allow_permanent=False)
            if extra:
                raise UsageError
            settings = await db.update_settings(chat_id, spam_mute_seconds=duration)
        else:
            enabled = parse_switch(command.args)
            if enabled:
                if message.chat.type != ChatType.SUPERGROUP:
                    raise require_supergroup()
                await services.access.bot_admin(chat_id, "can_restrict_members")
            settings = await db.update_settings(chat_id, antispam_enabled=enabled)
        services.filters.spam.reset_chat(chat_id)
        return spam_status(settings)

    if command.name == "links":
        if command.args:
            enabled = parse_switch(command.args)
            if enabled:
                await services.access.bot_admin(chat_id, "can_delete_messages")
            settings = await db.update_settings(chat_id, links_enabled=enabled)
        state = on_off(settings.links_enabled, settings)
        return settings.text(f"🛡 Фильтр ссылок {state}", f"🛡 Link filter {state}")

    if command.name == "welcome":
        if option in {"текст", "text"}:
            if not 1 <= len(rest) <= 400:
                raise UsageError
            settings = await db.update_settings(chat_id, welcome_text=rest)
        elif command.args:
            settings = await db.update_settings(
                chat_id, welcome_enabled=parse_switch(command.args)
            )
        state = on_off(settings.welcome_enabled, settings)
        return settings.text(f"Приветствие {state}", f"Welcome {state}")

    if not option:
        return settings_summary(settings)
    option = OPTION_NAMES.get(option, option)
    if option in SWITCHABLE:
        settings = await db.update_settings(
            chat_id, **{SWITCHABLE[option]: parse_switch(rest)}
        )
    elif option == "warnlimit":
        settings = await db.update_settings(
            chat_id, warn_limit=bounded_int(rest, 1, 100)
        )
    elif option == "warnmute":
        duration, extra = parse_duration(rest, allow_permanent=False)
        if extra:
            raise UsageError
        settings = await db.update_settings(chat_id, warn_mute_seconds=duration)
    elif option == "timezone":
        try:
            if not 1 <= len(rest) <= 80:
                raise ValueError
            ZoneInfo(rest)
        except (ZoneInfoNotFoundError, ValueError):
            raise UsageError from None
        settings = await db.update_settings(chat_id, timezone=rest)
    elif option == "language":
        language = normalized(rest)
        if language not in {"ru", "en"}:
            raise UsageError
        settings = await db.update_settings(chat_id, language=language)
    else:
        raise UsageError
    return settings.text("✅ Настройки сохранены", "✅ Settings saved")


def spam_status(settings: ChatSettings) -> str:
    label = (
        settings.text("Антиспам включён", "Antispam on")
        if settings.antispam_enabled
        else settings.text("Антиспам выключен", "Antispam off")
    )
    rate = settings.text(
        f"{settings.spam_limit} сообщ. / {settings.spam_window} сек",
        f"{settings.spam_limit} messages / {settings.spam_window}s",
    )
    duration = duration_label(settings.spam_mute_seconds, settings)
    mute = settings.text(f"мут {duration}", f"mute {duration}")
    return f"🛡 {label} · {rate} · {mute}"


def settings_summary(settings: ChatSettings) -> str:
    duration = duration_label(settings.warn_mute_seconds, settings)
    header = settings.text(
        f"Варны {settings.warn_limit} → мут {duration}",
        f"{settings.warn_limit} warnings → mute {duration}",
    )
    ru_labels = (
        "Антиспам",
        "Ссылки",
        "Приветствие",
        "История",
        "Очистка",
        "Статистика",
        "Музыка",
        "Огоньки",
        "Удаление команд",
    )
    en_labels = (
        "Antispam",
        "Links",
        "Welcome",
        "History",
        "Purge",
        "Stats",
        "Music",
        "Streaks",
        "Delete commands",
    )
    statuses = (
        settings.antispam_enabled,
        settings.links_enabled,
        settings.welcome_enabled,
        settings.history_enabled,
        settings.purge_enabled,
        settings.stats_enabled,
        settings.music_enabled,
        settings.streaks_enabled,
        settings.delete_commands,
    )
    labels = en_labels if settings.language == "en" else ru_labels
    features = [
        f"{label} {on_off(status, settings)}"
        for label, status in zip(labels, statuses, strict=True)
    ]
    return f"{settings.language} · {settings.timezone}\n{header}\n" + " · ".join(
        features
    )


def settings_usage(command: str, settings: ChatSettings) -> str:
    options = {
        "antispam": (
            "!антиспам вкл|выкл|лимит N сек|мут м N",
            "!antispam on|off|limit N seconds|mute m N",
        ),
        "links": ("!ссылки вкл|выкл", "!links on|off"),
        "welcome": ("!приветствие вкл|выкл|текст …", "!welcome on|off|text …"),
        "settings": (
            "!настройки язык|пояс|лимит|варнмут|история|очистка|статистика|музыка|огоньки|команды <значение>",
            "!settings language|timezone|warnlimit|warnmute|history|purge|stats|music|streaks|deletecommands <value>",
        ),
    }
    return settings.text(*options[command])
