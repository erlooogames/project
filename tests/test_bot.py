import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import time
from types import SimpleNamespace
from unittest.mock import patch

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import (
    BanChatMember,
    DeleteMessage,
    DeleteMessages,
    DeleteWebhook,
    GetChat,
    GetChatAdministrators,
    GetChatMember,
    GetMe,
    GetUpdates,
    RestrictChatMember,
    SendMessage,
    UnbanChatMember,
)
from aiogram.types import (
    Chat,
    ChatMemberAdministrator,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberRestricted,
    ChatPermissions,
    Message,
    Update,
    User,
)
from aiohttp import ClientSession

from bot.config import Config
from bot.database import Database
from bot.runtime import Services
from main import create_dispatcher, run

CHAT = Chat(id=-100123, type="supergroup", title="Test group")
BOT = User(id=101, is_bot=True, first_name="Bot", username="test_bot")
ADMIN = User(id=102, is_bot=False, first_name="Admin")
NO_RIGHTS = User(id=103, is_bot=False, first_name="No rights")
USER = User(id=104, is_bot=False, first_name="Alice", username="alice")
OTHER = User(id=105, is_bot=False, first_name="Bob")
OWNER = User(id=106, is_bot=False, first_name="Owner", username="UnixX_Prime")
FAKE_OWNER = User(id=107, is_bot=False, first_name="Impostor", username="UnixX_Prime")
CODE = "test-claim-code-for-fixtures-only"


def admin(user: User, *, rights: bool = True):
    return ChatMemberAdministrator.model_construct(
        user=user,
        status=ChatMemberStatus.ADMINISTRATOR,
        can_delete_messages=rights,
        can_restrict_members=rights,
    )


class TelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.members = {
            user.id: ChatMemberMember(user=user)
            for user in (USER, OTHER, OWNER, FAKE_OWNER)
        }
        self.members.update(
            {
                BOT.id: admin(BOT),
                ADMIN.id: admin(ADMIN),
                NO_RIGHTS.id: admin(NO_RIGHTS, rights=False),
            }
        )
        self.calls = []
        self.replies = []
        self.counter = 0
        self.polling_started = asyncio.Event()
        self.fail_next_restriction = False
        self.lose_next_restriction_reply = False

    def next_id(self):
        self.counter += 1
        return self.counter

    async def close(self):
        pass

    async def stream_content(self, *_args, **_kwargs):
        for chunk in ():
            yield chunk

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetMe):
            return BOT
        if isinstance(method, DeleteWebhook):
            return True
        if isinstance(method, GetUpdates):
            self.polling_started.set()
            return await asyncio.Future()
        if isinstance(method, GetChatMember):
            if method.user_id not in self.members:
                raise TelegramBadRequest(method, "Bad Request: user not found")
            return self.members[method.user_id]
        if isinstance(method, GetChatAdministrators):
            return [
                item
                for item in self.members.values()
                if isinstance(item, ChatMemberAdministrator)
            ]
        if isinstance(method, GetChat):
            return SimpleNamespace(
                permissions=ChatPermissions(
                    can_send_messages=True, can_invite_users=True
                )
            )
        if isinstance(method, RestrictChatMember):
            if self.fail_next_restriction:
                self.fail_next_restriction = False
                raise TelegramNetworkError(method, "temporary failure")
            user = self.members[method.user_id].user
            if method.permissions.can_send_messages:
                self.members[user.id] = ChatMemberMember(user=user)
            else:
                rights = {
                    name: bool(getattr(method.permissions, name))
                    for name in ChatPermissions.model_fields
                }
                self.members[user.id] = ChatMemberRestricted(
                    user=user,
                    is_member=True,
                    until_date=datetime.fromtimestamp(method.until_date or 0, UTC),
                    **rights,
                )
            if self.lose_next_restriction_reply:
                self.lose_next_restriction_reply = False
                raise TelegramNetworkError(method, "response lost")
            return True
        if isinstance(method, BanChatMember):
            user = self.members[method.user_id].user
            self.members[user.id] = ChatMemberBanned(
                user=user,
                until_date=datetime.fromtimestamp(method.until_date or 0, UTC),
            )
            return True
        if isinstance(method, UnbanChatMember):
            user = self.members[method.user_id].user
            self.members[user.id] = ChatMemberLeft(user=user)
            return True
        if isinstance(method, (DeleteMessage, DeleteMessages)):
            return True
        if isinstance(method, SendMessage):
            self.replies.append(method.text)
            chat = (
                CHAT
                if method.chat_id == CHAT.id
                else Chat(id=method.chat_id, type="private")
            )
            return Message(
                message_id=self.next_id(),
                chat=chat,
                date=datetime.now(UTC),
                from_user=BOT,
            )
        raise AssertionError(f"Unhandled test method {type(method).__name__}")


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "test.sqlite3"
        self.config = Config(f"{BOT.id}:test-only", CODE, self.path, "US", "INFO")
        self.db = await self.enterAsyncContext(Database(self.path))
        self.session = TelegramSession()
        self.bot = await self.enterAsyncContext(
            Bot(self.config.token, session=self.session)
        )
        self.http = await self.enterAsyncContext(ClientSession())
        self.services = Services.create(self.config, self.bot, self.db, self.http)
        self.dp = create_dispatcher(self.services)

    async def send(
        self,
        text,
        user=ADMIN,
        *,
        chat=CHAT,
        reply=None,
        moment=None,
        topic=None,
        edited_id=None,
    ):
        now = moment or datetime.now(UTC)
        parent = (
            None
            if reply is None
            else Message(
                message_id=1, chat=chat, date=now, from_user=reply, text="test"
            )
        )
        message = Message(
            message_id=edited_id or self.session.next_id(),
            chat=chat,
            date=now,
            from_user=user,
            reply_to_message=parent,
            text=text,
            message_thread_id=topic,
            is_topic_message=bool(topic),
        )
        return await self.feed(message, edited=edited_id is not None)

    async def feed(self, message, *, edited=False):
        before = len(self.session.replies)
        key = "edited_message" if edited else "message"
        await self.dp.feed_update(
            self.bot, Update(update_id=self.session.next_id(), **{key: message})
        )
        return self.session.replies[-1] if len(self.session.replies) > before else None

    def requests(self, method_type):
        return [
            method for method in self.session.calls if isinstance(method, method_type)
        ]

    async def test_entrypoint_polls_and_shuts_down(self):
        config = replace(self.config, db_path=self.path.with_name("startup.sqlite3"))
        dispatcher = None

        def build(services):
            nonlocal dispatcher
            dispatcher = create_dispatcher(services)
            return dispatcher

        with (
            patch("main.AiohttpSession", return_value=self.session),
            patch("main.create_dispatcher", side_effect=build),
        ):
            task = asyncio.create_task(run(config))
            try:
                await asyncio.wait_for(self.session.polling_started.wait(), timeout=5)
                await asyncio.wait_for(dispatcher.stop_polling(), timeout=5)
                await task
            finally:
                if not task.done():
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
        self.assertFalse(self.requests(DeleteWebhook)[0].drop_pending_updates)
        self.assertIn("edited_message", self.requests(GetUpdates)[0].allowed_updates)

    async def test_both_languages_reply_id_precedence_and_removals(self):
        self.assertIn(
            "мут 15 мин", await self.send(f"!mute м 15 {USER.id} example", reply=OTHER)
        )
        mute = self.requests(RestrictChatMember)[-1]
        self.assertEqual(mute.user_id, USER.id)
        self.assertGreaterEqual(mute.until_date - int(time()), 899)
        self.assertTrue(mute.use_independent_chat_permissions)
        self.assertFalse(mute.permissions.can_send_photos)
        self.assertTrue(mute.permissions.can_invite_users)
        self.assertIn("Мут снят", await self.send("!анмут", reply=USER))
        self.assertTrue(
            self.requests(RestrictChatMember)[-1].permissions.can_send_messages
        )
        self.assertIn("бан 7 дн", await self.send("!бан д 7", reply=OTHER))
        self.assertGreater(self.requests(BanChatMember)[-1].until_date - time(), 604790)
        self.assertIn("Бан снят", await self.send(f"!unban {OTHER.id}"))
        self.assertTrue(self.requests(UnbanChatMember)[-1].only_if_banned)
        self.assertIn("исключён", await self.send(f"!кик {USER.id}"))
        self.assertFalse(self.requests(UnbanChatMember)[-1].only_if_banned)
        self.assertIn("бан навсегда", await self.send(f"!ban perm {OTHER.id}"))
        self.assertIsNone(self.requests(BanChatMember)[-1].until_date)
        self.assertTrue(self.requests(DeleteMessage))

    async def test_unknown_user_permissions_and_protected_admin(self):
        for command in (
            f"!mute м 2 {USER.id}",
            f"!warn м 2 {USER.id}",
            f"!ban д 1 {USER.id}",
        ):
            self.assertIn("администратор", await self.send(command, USER))
            self.assertIn("права", await self.send(command, NO_RIGHTS))
        self.assertIn("Пользователь не найден", await self.send("!mute м 2 99999"))
        self.assertIn("защищён", await self.send(f"!warn м 2 {NO_RIGHTS.id}"))
        self.assertFalse(self.requests(RestrictChatMember))
        self.session.members[BOT.id] = admin(BOT, rights=False)
        self.assertIn("Боту нужно право", await self.send(f"!ban д 1 {USER.id}"))
        basic = Chat(id=-42, type="group", title="Basic")
        self.assertIn(
            "супергруппе", await self.send(f"!mute м 2 {USER.id}", chat=basic)
        )

    async def test_owner_claim_uses_private_id_and_cannot_rebind(self):
        owner_chat = Chat(id=OWNER.id, type="private")
        fake_chat = Chat(id=FAKE_OWNER.id, type="private")
        alice_chat = Chat(id=USER.id, type="private")
        self.assertIsNone(await self.send(f"/claim {CODE}", OWNER))
        self.assertIn(
            "недоступна", await self.send(f"/claim {CODE}", USER, chat=alice_chat)
        )
        self.assertIn(
            "недоступна", await self.send("/claim неправильный", OWNER, chat=owner_chat)
        )
        self.assertIn(
            "привязан по Telegram ID",
            await self.send(f"/claim {CODE}", OWNER, chat=owner_chat),
        )
        self.assertIn(
            "недоступна", await self.send(f"/claim {CODE}", FAKE_OWNER, chat=fake_chat)
        )
        renamed = OWNER.model_copy(update={"username": "new_owner_name"})
        self.assertEqual(await self.db.owner_id(), renamed.id)
        self.assertIn("бан", await self.send(f"!ban д 1 {OTHER.id}", renamed))
        self.assertIn("защищён", await self.send(f"!mute м 2 {renamed.id}"))
        self.assertIn("защищён", await self.send(f"!warn д 1 {renamed.id}"))

    async def test_warnings_and_auto_mute_once_then_remove_by_id_and_last(self):
        commands = (
            ("!варн м 30 причина", USER),
            (f"!warn н 2 {USER.id} причина 2", None),
            (f"!warn perm {USER.id}", None),
        )
        for idx, (text, parent) in enumerate(commands, 1):
            reply = await self.send(text, reply=parent)
            self.assertIn(f"{idx}/3", reply)
        self.assertIn("мут 1 дн", reply)
        self.assertEqual(len(self.requests(RestrictChatMember)), 1)
        await self.send(f"!unmute {USER.id}")
        self.assertIn("4/3", await self.send(f"!warn perm {USER.id}"))
        self.assertEqual(len(self.requests(RestrictChatMember)), 2)
        self.assertIn("#2 снят", await self.send("!unwarn #2"))
        self.assertIn("#4 снят", await self.send("!снять варн", reply=USER))
        self.assertIn("#3, #1", await self.send("!варны", USER))
        history = await self.send("!history", USER)
        self.assertIn("#2 · снят — причина 2", history)
        self.assertIn("снятие варна", await self.send("!наказания", USER))

    async def test_lost_response_and_expired_restriction_reconciled(self):
        self.session.lose_next_restriction_reply = True
        self.assertIn("Повторю в фоне", await self.send(f"!mute m 30 {USER.id}"))
        pending = (await self.db.actions(CHAT.id, USER.id))[0]
        self.assertEqual(pending.status, "pending")
        async with Database(self.path) as reopened:
            restored = Services.create(self.config, self.bot, reopened, self.http)
            with patch(
                "bot.services.moderation.time", return_value=pending.retry_at + 1
            ):
                await restored.moderation.maintain_once()
        active = await self.db.restriction(CHAT.id, USER.id, "mute")
        self.assertEqual(active.action_id, pending.id)
        self.assertEqual(len(self.requests(RestrictChatMember)), 1)

        await self.send(f"!ban м 1 {OTHER.id}")
        ban = await self.db.restriction(CHAT.id, OTHER.id, "ban")
        self.assertIsNotNone(ban)
        with patch("bot.services.moderation.time", return_value=active.ends_at + 1):
            await self.services.moderation.maintain_once()
        self.assertIsNone(await self.db.restriction(CHAT.id, USER.id, "mute"))
        self.assertIsNone(await self.db.restriction(CHAT.id, OTHER.id, "ban"))
        self.assertEqual(self.session.members[USER.id].status, ChatMemberStatus.MEMBER)
        self.assertTrue(self.requests(UnbanChatMember)[-1].only_if_banned)
        self.assertEqual(
            (await self.db.actions(CHAT.id, OTHER.id))[0].action, "expire_ban"
        )

    async def test_expiry_does_not_overwrite_later_telegram_restriction(self):
        await self.send(f"!mute m 1 {USER.id}")
        active = await self.db.restriction(CHAT.id, USER.id, "mute")
        self.session.members[USER.id] = self.session.members[USER.id].model_copy(
            update={"until_date": datetime.fromtimestamp(active.ends_at + 120, UTC)}
        )
        with patch("bot.services.moderation.time", return_value=active.ends_at + 1):
            await self.services.moderation.maintain_once()
        self.assertEqual(len(self.requests(RestrictChatMember)), 1)
        self.assertIsNone(await self.db.restriction(CHAT.id, USER.id, "mute"))

    async def test_old_failed_action_cannot_replace_newer_successful_mute(self):
        self.session.fail_next_restriction = True
        await self.send(f"!mute m 2 {USER.id}")
        older = (await self.db.actions(CHAT.id, USER.id))[0]
        await self.send(f"!мут м 20 {USER.id}")
        with patch("bot.services.moderation.time", return_value=older.retry_at + 1):
            await self.services.moderation.maintain_once()
        events = await self.db.actions(CHAT.id, USER.id)
        self.assertEqual([event.status for event in events], ["applied", "skipped"])
        self.assertEqual(len(self.requests(RestrictChatMember)), 2)

    async def test_antispam_duplicates_exemptions_and_no_second_mute(self):
        await self.db.claim_owner(OWNER.id, int(time()))
        self.assertIn("включён", await self.send("!антиспам вкл"))
        await self.send("!antispam limit 5 10")
        await self.send("!antispam mute m 30")
        for person in (ADMIN, OWNER, BOT):
            for _ in range(6):
                self.assertIsNone(await self.send("same text", person))
        self.assertFalse(self.requests(RestrictChatMember))
        self.assertIsNone(await self.send("Flood Message", USER))
        self.assertIsNone(await self.send(" flood  message ", USER))
        self.assertIn("спам", await self.send("FLOOD MESSAGE", USER))
        await self.send("Flood Message", USER)
        self.assertEqual(len(self.requests(RestrictChatMember)), 1)
        self.assertEqual(
            (await self.db.actions(CHAT.id, USER.id))[0].source, "antispam"
        )
        self.assertIn("выключен", await self.send("!antispam off"))

    async def test_activity_ignores_commands_bots_and_spam_with_antispam_off(self):
        await self.send("!настройки статистика вкл")
        await self.send("!help", USER)
        await self.send("🧡🧡", USER)
        await self.send("Message from another bot", BOT)
        self.assertIsNone(await self.db.activity(CHAT.id, USER.id))
        await self.send("Content for people", USER)
        await self.send(" content   for people ", USER)
        await self.send("CONTENT FOR PEOPLE", USER)
        self.assertEqual(await self.db.stats(CHAT.id), (1, 1))
        self.assertIn("Огонёк: 1 день", await self.send("!огонёк", USER))
        self.assertIn("@alice — 1 день", await self.send("!streaks", USER))

    async def test_links_edits_purge_topics_and_welcome(self):
        await self.send("!links on")
        initial_deletions = len(self.requests(DeleteMessage))
        await self.send("https://example.com/a", USER, topic=10)
        self.assertEqual(len(self.requests(DeleteMessage)), initial_deletions + 1)
        self.assertIsNone(await self.db.activity(CHAT.id, USER.id))
        await self.send("https://example.com/admin", ADMIN)
        self.assertEqual(len(self.requests(DeleteMessage)), initial_deletions + 1)
        await self.send("A regular line", USER, topic=10)
        tracked = self.session.counter - 1
        await self.send("Changed to t.me/test", USER, topic=10, edited_id=tracked)
        self.assertEqual(self.requests(DeleteMessage)[-1].message_id, tracked)
        await self.send("An available message", USER, topic=10)
        candidate = self.session.counter - 1
        await self.send("Another topic", USER, topic=20)
        await self.send(
            "An old message",
            USER,
            topic=10,
            moment=datetime.now(UTC) - timedelta(hours=49),
        )
        self.assertIn("Очистка до 1", await self.send("!purge 100", topic=10))
        self.assertEqual(self.requests(DeleteMessages)[-1].message_ids, [candidate])
        await self.send("!welcome text Hi {name}, welcome to {chat}!")
        await self.send("!приветствие вкл")
        join = Message(
            message_id=self.session.next_id(),
            chat=CHAT,
            date=datetime.now(UTC),
            from_user=ADMIN,
            new_chat_members=[USER, BOT],
        )
        self.assertEqual(await self.feed(join), "Hi @alice, welcome to Test group!")
