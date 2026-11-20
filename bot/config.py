import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from aiogram.utils.token import TokenValidationError, validate_token
from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Config:
    token: str = field(repr=False)
    setup_code: str = field(repr=False)
    db_path: Path
    itunes_country: str
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        try:
            validate_token(token)
        except TokenValidationError:
            raise ValueError("Set a valid BOT_TOKEN in the environment.") from None
        code = os.getenv("OWNER_SETUP_CODE", "").strip()
        if code and (len(code) < 24 or any(char.isspace() for char in code)):
            raise ValueError(
                "OWNER_SETUP_CODE needs at least 24 characters without spaces."
            )
        country = os.getenv("ITUNES_COUNTRY", "US").strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise ValueError("ITUNES_COUNTRY must be a two-letter country code.")
        level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Invalid LOG_LEVEL.")
        path = Path(os.getenv("DB_PATH") or "data/moderation.sqlite3").expanduser()
        return cls(token, code, path, country, level)


class SecretFormatter(logging.Formatter):
    def __init__(self, *secrets: str):
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self.secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        for secret in self.secrets:
            formatted = formatted.replace(secret, "[redacted]")
        return formatted


def configure_logging(config: Config) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SecretFormatter(config.token, config.setup_code))
    logging.basicConfig(level=config.log_level, handlers=[handler], force=True)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
