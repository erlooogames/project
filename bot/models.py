from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

MAX_DURATION = 365 * 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class ChatSettings:
    language: str = "ru"
    timezone: str = "Europe/Kyiv"
    warn_limit: int = 3
    warn_mute_seconds: int = 86400
    antispam_enabled: bool = False
    spam_limit: int = 5
    spam_window: int = 10
    spam_mute_seconds: int = 1800
    links_enabled: bool = False
    welcome_enabled: bool = False
    welcome_text: str = ""
    history_enabled: bool = True
    purge_enabled: bool = True
    stats_enabled: bool = False
    music_enabled: bool = True
    streaks_enabled: bool = True
    delete_commands: bool = True

    def text(self, ru: str, en: str) -> str:
        return en if self.language == "en" else ru

    def day(self, moment: datetime) -> date:
        return moment.astimezone(ZoneInfo(self.timezone)).date()


@dataclass(frozen=True, slots=True)
class Action:
    chat_id: int
    user_id: int
    actor_id: int | None
    action: str
    source: str
    reason: str
    created_at: int
    ends_at: int | None = None
    message_id: int | None = None
    id: int = 0
    status: str = "pending"
    detail: str = ""
    retry_at: int = 0
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class Warning:
    id: int
    chat_id: int
    user_id: int
    actor_id: int
    reason: str
    created_at: int
    ends_at: int | None
    revoked_at: int | None
    auto_action_id: int | None

    def is_active(self, now: int) -> bool:
        return self.revoked_at is None and (self.ends_at is None or self.ends_at > now)


@dataclass(frozen=True, slots=True)
class Restriction:
    chat_id: int
    user_id: int
    kind: str
    action_id: int
    ends_at: int | None
    telegram_until: int
    retry_at: int
    attempts: int


@dataclass(frozen=True, slots=True)
class Activity:
    user_id: int
    display_name: str
    last_day: date | None
    current: int
    best: int

    def streak(self, today: date) -> int:
        if self.last_day is not None and self.last_day >= today - timedelta(days=1):
            return self.current
        return 0
