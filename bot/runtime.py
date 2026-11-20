from dataclasses import dataclass, field

from aiogram import Bot
from aiohttp import ClientSession

from bot.config import Config
from bot.database import Database
from bot.services.access import Access
from bot.services.group_filters import GroupFilters
from bot.services.moderation import Moderation
from bot.services.music import MusicSearch


@dataclass(slots=True)
class Services:
    config: Config = field(repr=False)
    db: Database
    access: Access
    moderation: Moderation
    filters: GroupFilters
    music: MusicSearch

    @classmethod
    def create(
        cls, config: Config, bot: Bot, db: Database, http: ClientSession
    ) -> "Services":
        access = Access(bot, db)
        moderation = Moderation(bot, db, access)
        return cls(
            config,
            db,
            access,
            moderation,
            GroupFilters(bot, access, moderation),
            MusicSearch(http, config.itunes_country),
        )
