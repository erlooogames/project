from hmac import compare_digest
from time import time

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from bot.handlers.features import HELP_EN, HELP_RU
from bot.runtime import Services

INITIAL_OWNER_USERNAME = "unixx_prime"


def private_router() -> Router:
    router = Router(name="private")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(claim, Command("claim", ignore_case=True))
    router.message.register(start, CommandStart())
    router.message.register(
        show_help, Command("помощь", "help", prefix="!", ignore_case=True)
    )
    return router


async def claim(message: Message, command: CommandObject, services: Services) -> None:
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    user = message.from_user
    if user is None or user.is_bot:
        return
    bound_id = await services.db.owner_id()
    if bound_id is not None:
        text = (
            "Владелец уже привязан." if bound_id == user.id else "Привязка недоступна."
        )
        await message.answer(text)
        return

    username = (user.username or "").casefold()
    code = services.config.setup_code
    supplied = (command.args or "").strip()
    if (
        username != INITIAL_OWNER_USERNAME
        or not code
        or not compare_digest(code.encode(), supplied.encode())
    ):
        await message.answer("Привязка недоступна.")
        return

    if await services.db.claim_owner(user.id, int(time())):
        await message.answer("✅ Владелец привязан по Telegram ID.")
    else:
        await message.answer("Владелец уже привязан.")


async def start(message: Message) -> None:
    await message.answer(
        "Добавьте бота в группу и назначьте администратором. "
        "Справка в личке — !помощь / !help."
    )


async def show_help(message: Message, command: CommandObject) -> None:
    if command.command.casefold() == "help":
        intro = (
            "Add the bot as a group admin and send moderation commands in the group."
        )
        guide = HELP_EN
    else:
        intro = (
            "Добавьте бота администратором и отправляйте команды модерации в группе."
        )
        guide = HELP_RU
    await message.answer(f"{intro}\n\n{guide}")
