import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DB", "postgresql+asyncpg://user:password@localhost/bistro")

from aiogram import Bot, Dispatcher, types
from aiogram.client.session.base import BaseSession
from aiogram.enums import ChatType
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.exc import SQLAlchemyError

from bistro.handlers import admin, autotest
from bistro.handlers.states import QuestionCreationStates


USER_ID = 123456789
GROUP_ID = -100123456789


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
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


class AutotestHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = RecordingSession()
        self.bot = Bot(token="123456:TEST_TOKEN", session=self.transport)
        self.dp = Dispatcher(storage=MemoryStorage())
        autotest.register_handlers_autotest(self.dp)
        # The subscription button must work while the owner is adding questions.
        admin.register_handlers_admin(self.dp)
        self.toggle = AsyncMock()
        self.toggle_patch = patch.object(autotest, "toggle_subscription", self.toggle)
        self.toggle_patch.start()
        self.addCleanup(self.toggle_patch.stop)
        self.group_toggle = AsyncMock()
        self.group_toggle_patch = patch.object(autotest, "toggle_group_subscription", self.group_toggle)
        self.group_toggle_patch.start()
        self.addCleanup(self.group_toggle_patch.stop)

    async def asyncTearDown(self):
        await self.dp.storage.close()
        await self.bot.session.close()

    def message(self, text="Автотест", *, chat_type=ChatType.PRIVATE, with_user=True, user_id=USER_ID):
        payload = {
            "message_id": 10,
            "date": 1700000000,
            "chat": {
                "id": user_id if chat_type == ChatType.PRIVATE else GROUP_ID,
                "type": chat_type,
            },
            "text": text,
        }
        if with_user:
            payload["from"] = {"id": user_id, "is_bot": False, "first_name": "Игрок"}
        if text.startswith("/"):
            payload["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text)}]
        return types.Message.model_validate(payload, context={"bot": self.bot})

    async def feed_message(self, text="Автотест", *, chat_type=ChatType.PRIVATE, update_id=1, user_id=USER_ID):
        await self.dp.feed_update(self.bot, types.Update(
            update_id=update_id, message=self.message(text, chat_type=chat_type, user_id=user_id),
        ))

    async def test_button_and_command_toggle_and_report_both_states(self):
        for text in ("Автотест", "⏰ Автотест", "🚫 Автотест", "/autotest"):
            with self.subTest(text=text):
                self.toggle.reset_mock(side_effect=True)
                self.toggle.side_effect = [True, False]
                self.transport.requests.clear()

                await self.feed_message(text)
                await self.feed_message(text, update_id=2)

                self.assertEqual(self.toggle.await_count, 2)
                self.toggle.assert_awaited_with(USER_ID)
                self.group_toggle.assert_not_awaited()
                self.assertEqual([method.text for method in self.transport.requests], [
                    "Вы будете получать новый Быстротест каждый день",
                    "Автотест отключен. Вы больше не будете получать ежедневные Быстротесты.",
                ])
                self.assertTrue(all(method.chat_id == USER_ID for method in self.transport.requests))
                self.assertEqual([
                    method.reply_markup.keyboard[1][0].text for method in self.transport.requests
                ], ["🚫 Автотест", "⏰ Автотест"])

    async def test_group_button_and_command_toggle_one_shared_schedule_for_different_members(self):
        for chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
            for text in ("Автотест", "/autotest"):
                with self.subTest(chat_type=chat_type, text=text):
                    self.group_toggle.reset_mock(side_effect=True)
                    self.group_toggle.side_effect = [True, False]
                    self.transport.requests.clear()

                    await self.feed_message(text, chat_type=chat_type)
                    await self.feed_message(text, chat_type=chat_type, user_id=USER_ID + 1, update_id=2)

                    self.toggle.assert_not_awaited()
                    self.assertEqual(self.group_toggle.await_count, 2)
                    self.assertEqual([call.args for call in self.group_toggle.await_args_list], [
                        (GROUP_ID,), (GROUP_ID,),
                    ])
                    self.assertEqual([method.text for method in self.transport.requests], [
                        "Автотест включен для группы. Новый Быстротест будет приходить сюда каждый день.",
                        "Автотест отключен для группы. Ежедневная отправка остановлена.",
                    ])
                    self.assertTrue(all(method.chat_id == GROUP_ID for method in self.transport.requests))

    async def test_toggle_preserves_admin_draft_and_game_state(self):
        self.toggle.return_value = True
        state = self.dp.fsm.get_context(bot=self.bot, chat_id=USER_ID, user_id=USER_ID)
        draft = {"word": "Камера", "options": ["фотограф", "преступник"]}
        await state.set_state(QuestionCreationStates.waiting_for_word)
        await state.set_data(draft)

        await self.feed_message()

        self.toggle.assert_awaited_once_with(USER_ID)
        self.assertEqual(await state.get_state(), QuestionCreationStates.waiting_for_word.state)
        self.assertEqual(await state.get_data(), draft)
        self.assertEqual(self.transport.requests[-1].text, autotest.AUTOTEST_ENABLED)

    async def test_database_failure_does_not_confirm_subscription(self):
        self.toggle.side_effect = SQLAlchemyError("Database unavailable")
        with self.assertLogs(autotest.logger, level="ERROR"):
            await self.feed_message()

        self.assertEqual([method.text for method in self.transport.requests], [
            "Не удалось изменить настройку Автотеста. Попробуйте еще раз позже.",
        ])

    async def test_missing_sender_is_ignored(self):
        for chat_type in (ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP):
            await autotest.toggle_autotest(self.message(chat_type=chat_type, with_user=False))

        self.toggle.assert_not_awaited()
        self.group_toggle.assert_not_awaited()
        self.assertEqual(self.transport.requests, [])

    async def test_group_database_failure_does_not_claim_shared_schedule_changed(self):
        self.group_toggle.side_effect = SQLAlchemyError("Database unavailable")
        with self.assertLogs(autotest.logger, level="ERROR"):
            await self.feed_message(chat_type=ChatType.SUPERGROUP)

        self.toggle.assert_not_awaited()
        self.assertEqual([method.text for method in self.transport.requests], [
            "Не удалось изменить настройку Автотеста. Попробуйте еще раз позже.",
        ])
        self.assertEqual(self.transport.requests[0].chat_id, GROUP_ID)

    async def test_channel_is_ignored(self):
        await autotest.toggle_autotest(self.message(chat_type=ChatType.CHANNEL))

        self.toggle.assert_not_awaited()
        self.group_toggle.assert_not_awaited()
        self.assertEqual(self.transport.requests, [])


if __name__ == "__main__":
    unittest.main()
