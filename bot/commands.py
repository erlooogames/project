import re
from dataclasses import dataclass

from bot.models import MAX_DURATION

ALIASES = {
    "мут": "mute",
    "анмут": "unmute",
    "бан": "ban",
    "разбан": "unban",
    "варн": "warn",
    "снять варн": "unwarn",
    "кик": "kick",
    "варны": "warns",
    "очистить": "purge",
    "помощь": "help",
    "антиспам": "antispam",
    "музыка": "music",
    "огонек": "streak",
    "огоньки": "streaks",
    "ссылки": "links",
    "приветствие": "welcome",
    "история": "history",
    "наказания": "punishments",
    "статистика": "stats",
    "настройки": "settings",
}
ALIASES.update({name: name for name in tuple(ALIASES.values())})
UNIT_SECONDS = {"м": 60, "m": 60, "д": 86400, "d": 86400, "н": 604800, "w": 604800}
PERMANENT = {"навсегда", "perm", "permanent"}
SWITCHES = {"вкл": True, "on": True, "выкл": False, "off": False}
MAX_ID = 2**52 - 1


class UsageError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    args: str


@dataclass(frozen=True, slots=True)
class Target:
    user_id: int
    reason: str
    warn_id: int | None = None


def normalized(text: str) -> str:
    return text.casefold().replace("ё", "е")


def take_word(text: str) -> tuple[str, str]:
    parts = text.split(maxsplit=1)
    if not parts:
        return "", ""
    return parts[0], parts[1] if len(parts) == 2 else ""


def parse_command(text: str | None) -> Command | None:
    if not text or not text.startswith("!"):
        return None
    word, args = take_word(text[1:])
    name = normalized(word)
    if name == "снять":
        second, args = take_word(args)
        name = f"{name} {normalized(second)}"
    return Command(ALIASES.get(name, ""), args)


def bounded_int(text: str, minimum: int, maximum: int) -> int:
    if not re.fullmatch(r"[0-9]{1,16}", text):
        raise UsageError
    value = int(text)
    if not minimum <= value <= maximum:
        raise UsageError
    return value


def parse_duration(
    text: str, *, allow_permanent: bool = True
) -> tuple[int | None, str]:
    unit, rest = take_word(text)
    unit = normalized(unit)
    if allow_permanent and unit in PERMANENT:
        return None, rest
    if unit not in UNIT_SECONDS:
        raise UsageError
    amount, rest = take_word(rest)
    seconds_per_unit = UNIT_SECONDS[unit]
    return bounded_int(
        amount, 1, MAX_DURATION // seconds_per_unit
    ) * seconds_per_unit, rest


def parse_target(
    text: str,
    reply_id: int | None,
    *,
    default_id: int | None = None,
    allow_warn_id: bool = False,
    allow_reason: bool = True,
) -> Target:
    raw, rest = take_word(text)
    user_id = reply_id or default_id
    if re.fullmatch(r"[+-]?[0-9]+", raw) or raw.casefold().startswith("id="):
        user_id = bounded_int(
            raw[3:] if raw.casefold().startswith("id=") else raw, 1, MAX_ID
        )
        text = rest
    if user_id is None:
        raise UsageError

    warn_id = None
    raw, rest = take_word(text)
    if allow_warn_id and raw.startswith("#"):
        warn_id = bounded_int(raw[1:], 1, MAX_ID)
        text = rest
    if allow_reason:
        raw, rest = take_word(text)
        if raw == "--":
            text = rest
    if text and not allow_reason:
        raise UsageError
    return Target(user_id, text[:240], warn_id)


def parse_switch(text: str) -> bool:
    try:
        return SWITCHES[normalized(text)]
    except KeyError:
        raise UsageError from None
