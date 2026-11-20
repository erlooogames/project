import re
from bisect import insort_right
from collections import OrderedDict
from dataclasses import dataclass, field
from hashlib import blake2s
from time import monotonic
from unicodedata import normalize

from aiogram.types import Message

from bot.models import ChatSettings

ATTACHMENTS = (
    "animation",
    "audio",
    "document",
    "photo",
    "sticker",
    "video",
    "video_note",
    "voice",
)
CONTENT = (
    *ATTACHMENTS,
    "text",
    "contact",
    "dice",
    "game",
    "location",
    "poll",
    "venue",
    "story",
    "paid_media",
)
LINK = re.compile(
    r"(?:\b(?:https?://|tg://|www\.)|(?<![@\w.])(?:t\.me|telegram\.me|telegram\.dog)/)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SpamResult:
    violation: str | None = None
    duplicate: bool = False


@dataclass(slots=True)
class Bucket:
    events: list[tuple[float, bytes | None, int | None]] = field(default_factory=list)
    next_enforcement: float = 0.0


class SpamMonitor:
    def __init__(self, capacity: int = 4096):
        self.buckets: OrderedDict[tuple[int, int], Bucket] = OrderedDict()
        self.capacity = capacity

    def observe(
        self,
        chat_id: int,
        user_id: int,
        sent_at: float,
        fingerprint: bytes | None,
        settings: ChatSettings,
        *,
        message_id: int | None = None,
    ) -> SpamResult:
        key = (chat_id, user_id)
        bucket = self.buckets.setdefault(key, Bucket())
        self.buckets.move_to_end(key)
        if len(self.buckets) > self.capacity:
            self.buckets.popitem(last=False)

        latest = max(sent_at, bucket.events[-1][0]) if bucket.events else sent_at
        cutoff = latest - settings.spam_window
        bucket.events = [event for event in bucket.events if event[0] > cutoff]
        if sent_at <= cutoff:
            return SpamResult()
        if message_id is not None and any(
            event[2] == message_id for event in bucket.events
        ):
            return SpamResult(duplicate=True)

        insort_right(
            bucket.events,
            (sent_at, fingerprint, message_id),
            key=lambda event: event[0],
        )
        count = len(bucket.events)
        repeats = (
            sum(content == fingerprint for _, content, _ in bucket.events)
            if fingerprint
            else 0
        )
        del bucket.events[: max(0, count - settings.spam_limit - 1)]
        if count > settings.spam_limit:
            return SpamResult("flood", repeats > 1)
        if repeats >= 3:
            return SpamResult("duplicate", True)
        return SpamResult(duplicate=repeats > 1)

    def should_enforce(self, chat_id: int, user_id: int) -> bool:
        bucket = self.buckets[(chat_id, user_id)]
        now = monotonic()
        if now < bucket.next_enforcement:
            return False
        bucket.next_enforcement = now + 15
        return True

    def reset_chat(self, chat_id: int) -> None:
        for key in tuple(self.buckets):
            if key[0] == chat_id:
                del self.buckets[key]


def is_content(message: Message) -> bool:
    return any(getattr(message, name, None) is not None for name in CONTENT)


def contains_link(message: Message) -> bool:
    return any(
        entity.type in {"url", "text_link"}
        for entity in (*(message.entities or ()), *(message.caption_entities or ()))
    ) or bool(LINK.search(message.text or message.caption or ""))


def fingerprint_for(message: Message) -> bytes | None:
    text = message.text or message.caption
    if text:
        value = " ".join(normalize("NFKC", text).casefold().split())
        if value:
            return blake2s(value.encode(), digest_size=12).digest()
    for field_name in ATTACHMENTS:
        attachment = getattr(message, field_name)
        if attachment:
            unique = (
                attachment[-1].file_unique_id
                if field_name == "photo"
                else attachment.file_unique_id
            )
            return blake2s(f"{field_name}:{unique}".encode(), digest_size=12).digest()
    return None


def is_meaningful(message: Message) -> bool:
    text = (message.text or message.caption or "").strip()
    if text.startswith(("!", "/")):
        return False
    if sum(char.isalnum() for char in text) >= 2:
        return True
    return any(getattr(message, name) for name in ATTACHMENTS if name != "sticker")
