from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic
from urllib.parse import urlsplit

import aiohttp

from bot.services.access import Problem

ITUNES_URL = "https://itunes.apple.com/search"


@dataclass(frozen=True, slots=True)
class Track:
    artist: str
    title: str
    url: str
    preview: str | None


def provider_url(value: object, domains: tuple[str, ...]) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        if (
            url.scheme != "https"
            or url.username is not None
            or url.password is not None
        ):
            return None
    except ValueError:
        return None
    return (
        value
        if any(host == domain or host.endswith("." + domain) for domain in domains)
        else None
    )


def retry_delay(header: str) -> float:
    try:
        return max(1, int(header))
    except ValueError:
        try:
            when = parsedate_to_datetime(header)
            return max(1, (when.astimezone(UTC) - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 60


class MusicSearch:
    def __init__(self, session: aiohttp.ClientSession, country: str):
        self.session = session
        self.country = country
        self.user_calls: OrderedDict[int, float] = OrderedDict()
        self.api_calls: deque[float] = deque()
        self.cache: OrderedDict[str, tuple[float, Track | None]] = OrderedDict()
        self.blocked_until = 0.0

    async def search(self, query: str, user_id: int) -> Track | None:
        now = monotonic()
        if now - self.user_calls.get(user_id, -1000.0) < 8:
            raise Problem(
                "Попробуйте через несколько секунд.", "Try again in a few seconds."
            )
        self.user_calls[user_id] = now
        self.user_calls.move_to_end(user_id)
        if len(self.user_calls) > 1024:
            self.user_calls.popitem(last=False)

        key = " ".join(query.casefold().split())
        cached = self.cache.get(key)
        if cached is not None and cached[0] > now:
            self.cache.move_to_end(key)
            return cached[1]

        while self.api_calls and self.api_calls[0] <= now - 60:
            self.api_calls.popleft()
        if now < self.blocked_until or len(self.api_calls) >= 18:
            raise Problem(
                "Поиск занят. Попробуйте позже.",
                "Music search is busy. Try again later.",
            )
        self.api_calls.append(now)
        try:
            async with self.session.get(
                ITUNES_URL,
                params={
                    "term": query,
                    "country": self.country,
                    "media": "music",
                    "entity": "song",
                    "limit": 4,
                },
            ) as response:
                if response.status == 429:
                    self.blocked_until = monotonic() + retry_delay(
                        response.headers.get("Retry-After", "")
                    )
                    raise Problem(
                        "Поиск занят. Попробуйте позже.",
                        "Music search is busy. Try again later.",
                    )
                response.raise_for_status()
                data = await response.json(content_type=None)
            tracks = data["results"]
            if not isinstance(tracks, list):
                raise TypeError("Invalid music search results")
        except (
            aiohttp.ClientError,
            TimeoutError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise Problem(
                "Сервис музыки недоступен.", "Music search is unavailable."
            ) from exc

        track = next(
            (found for item in tracks if (found := parse_track(item)) is not None), None
        )
        self.cache[key] = (monotonic() + 600, track)
        self.cache.move_to_end(key)
        if len(self.cache) > 128:
            self.cache.popitem(last=False)
        return track


def parse_track(item: object) -> Track | None:
    if not isinstance(item, dict):
        return None
    url = provider_url(item.get("trackViewUrl"), ("apple.com",))
    title, artist = item.get("trackName"), item.get("artistName")
    if (
        not url
        or not isinstance(title, str)
        or not isinstance(artist, str)
        or not title.strip()
        or not artist.strip()
    ):
        return None
    preview = provider_url(item.get("previewUrl"), ("apple.com", "mzstatic.com"))
    return Track(artist[:128], title[:128], url, preview)
