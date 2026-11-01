import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DB", "postgresql+asyncpg://user:password@localhost/bistro")

from aiogram import Bot, Dispatcher, types
from aiogram.client.session.base import BaseSession
from aiogram.enums import ChatType
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession

import config
import menus
from handlers import admin, autotest, game, start
from keyboards import AUTOTEST_OFF_BUTTON, AUTOTEST_ON_BUTTON
from states import AssocioCreationStates, BulkQuestionCreationStates, GameStates, QuestionCreationStates


USER_ID = 123456789
GROUP_ID = -100123456789
BOT_USERNAME = "bistro_test_bot"


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if method.__api_method__ == "getMe":
            return types.User(id=bot.id, is_bot=True, first_name="Быстротест", username=BOT_USERNAME)
        return types.Message.model_validate({
            "message_id": len(self.requests) + 100,
            "date": 1700000000,
            "chat": {
                "id": method.chat_id,
                "type": "private" if method.chat_id > 0 else "supergroup",
            },
            "text": method.text,
        }, context={"bot": bot})

    async def stream_content(self, url, **kwargs):
        yield b""


class StartStopHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = RecordingSession()
        self.bot = Bot(token="123456:TEST_TOKEN", session=self.transport)
        self.dp = Dispatcher(storage=MemoryStorage())
        autotest.register_handlers_autotest(self.dp)
        # Menu commands must be registered before state-specific admin handlers.
        start.register_handlers_start(self.dp)
        admin.register_handlers_admin(self.dp)
        game.register_handlers_game(self.dp)

        self.subscription = AsyncMock()
        self.group_subscription = AsyncMock()
        self.session = AsyncMock(spec=AsyncSession)
        self.result = MagicMock()
        self.result.scalar_one.return_value = False
        self.session.execute.return_value = self.result
        self.menu_database = MagicMock()
        self.menu_database.return_value.__aenter__.return_value = self.session
        self.admin_database = MagicMock()
        self.game_database = MagicMock()
        self.patches = [
            patch.object(config, "ADMIN_ID", USER_ID),
            patch.object(autotest, "toggle_subscription", self.subscription),
            patch.object(autotest, "toggle_group_subscription", self.group_subscription),
            patch.object(menus, "async_session", self.menu_database),
            patch.object(admin, "async_session", self.admin_database),
            patch.object(game, "async_session", self.game_database),
        ]
        for active_patch in self.patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)

    async def asyncTearDown(self):
        await self.dp.storage.close()
        await self.bot.session.close()

    def chat_id(self, chat_type):
        return USER_ID if chat_type == ChatType.PRIVATE else GROUP_ID

    def state(self, chat_type):
        return self.dp.fsm.get_context(
            bot=self.bot, chat_id=self.chat_id(chat_type), user_id=USER_ID,
        )

    async def send(self, command, chat_type, *, update_id=1):
        message = types.Message.model_validate({
            "message_id": update_id + 10,
            "date": 1700000000,
            "chat": {"id": self.chat_id(chat_type), "type": chat_type},
            "from": {"id": USER_ID, "is_bot": False, "first_name": "Игрок"},
            "text": command,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(command)}],
        }, context={"bot": self.bot})
        await self.dp.feed_update(self.bot, types.Update(update_id=update_id, message=message))

    def responses(self):
        return [method for method in self.transport.requests if method.__api_method__ == "sendMessage"]

    def assert_subscriptions_unchanged(self):
        self.subscription.assert_not_awaited()
        self.group_subscription.assert_not_awaited()
        self.admin_database.assert_not_called()
        self.game_database.assert_not_called()

    async def test_stop_removes_keyboard_in_private_and_group_chats_with_optional_bot_mention(self):
        for chat_type in (ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP):
            for command in ("/stop", f"/stop@{BOT_USERNAME}"):
                with self.subTest(chat_type=chat_type, command=command):
                    self.transport.requests.clear()

                    await self.send(command, chat_type)

                    responses = self.responses()
                    self.assertEqual(len(responses), 1)
                    self.assertEqual(responses[0].chat_id, self.chat_id(chat_type))
                    self.assertIsInstance(responses[0].reply_markup, types.ReplyKeyboardRemove)
                    self.assertTrue(responses[0].reply_markup.remove_keyboard)
                    self.assertIn("/start", responses[0].text)
                    self.assertIsNone(await self.state(chat_type).get_state())
        self.menu_database.assert_not_called()
        self.assert_subscriptions_unchanged()

    async def test_stop_preserves_game_and_admin_drafts_without_being_consumed_as_input(self):
        drafts = (
            (GameStates.waiting_for_answer, {"qid": 7}),
            (QuestionCreationStates.waiting_for_word, {"word": "цезарь"}),
            (QuestionCreationStates.waiting_for_confirmation, {"word": "цезарь", "confirmation_message_id": 99}),
            (BulkQuestionCreationStates.waiting_for_options, {"bulk_words": ["цезарь"], "bulk_index": 0}),
            (AssocioCreationStates.waiting_for_associo, {"question_id": 7, "options": ["салат", "Рим"]}),
        )
        for chat_type in (ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP):
            for current_state, draft in drafts:
                with self.subTest(chat_type=chat_type, state=current_state.state):
                    state = self.state(chat_type)
                    await state.set_state(current_state)
                    await state.set_data(draft)
                    self.transport.requests.clear()

                    await self.send("/stop", chat_type)

                    self.assertEqual(await state.get_state(), current_state.state)
                    self.assertEqual(await state.get_data(), draft)
                    responses = self.responses()
                    self.assertEqual(len(responses), 1)
                    self.assertIsInstance(responses[0].reply_markup, types.ReplyKeyboardRemove)
        self.menu_database.assert_not_called()
        self.assert_subscriptions_unchanged()

    async def test_start_restores_menu_using_subscription_setting_and_preserves_active_draft(self):
        for chat_type in (ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP):
            for enabled in (False, True):
                with self.subTest(chat_type=chat_type, autotest_enabled=enabled):
                    state = self.state(chat_type)
                    draft = {"word": "цезарь", "confirmation_message_id": 99}
                    await state.set_state(QuestionCreationStates.waiting_for_confirmation)
                    await state.set_data(draft)
                    self.result.scalar_one.return_value = enabled
                    self.menu_database.reset_mock()
                    self.transport.requests.clear()

                    await self.send("/stop", chat_type)
                    self.menu_database.assert_not_called()
                    await self.send("/start", chat_type, update_id=2)

                    responses = self.responses()
                    self.assertEqual(len(responses), 2)
                    self.assertIsInstance(responses[0].reply_markup, types.ReplyKeyboardRemove)
                    keyboard = responses[1].reply_markup
                    self.assertIsInstance(keyboard, types.ReplyKeyboardMarkup)
                    buttons = [button.text for row in keyboard.keyboard for button in row]
                    self.assertIn("🎮 Быстротест", buttons)
                    self.assertIn(AUTOTEST_ON_BUTTON if enabled else AUTOTEST_OFF_BUTTON, buttons)
                    self.assertEqual("➕ Добавить вопрос" in buttons, chat_type == ChatType.PRIVATE)
                    self.menu_database.assert_called_once()
                    self.assertEqual(await state.get_state(), QuestionCreationStates.waiting_for_confirmation.state)
                    self.assertEqual(await state.get_data(), draft)
        self.session.add.assert_not_called()
        self.session.commit.assert_not_awaited()
        self.assert_subscriptions_unchanged()


if __name__ == "__main__":
    unittest.main()
