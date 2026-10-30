import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DB", "postgresql+asyncpg://user:password@localhost/bistro")

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ChatType
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession

import config
from handlers import admin, game, start
from models import Answer, Question, User
from states import GameStates, QuestionCreationStates


USER_ID = 123456789
PNG = b"\x89PNG\r\n\x1a\nchart"


def scalar_result(value):
    result = MagicMock()
    result.scalars.return_value.first.return_value = value
    return result


class AutotestGameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = AsyncMock(return_value=True)
        self.bot = Bot(token="123456:TEST_TOKEN", session=self.transport)
        self.dp = Dispatcher(storage=MemoryStorage())
        admin.register_handlers_admin(self.dp)
        game.register_handlers_game(self.dp)
        self.state = self.dp.fsm.get_context(
            bot=self.bot, chat_id=USER_ID, user_id=USER_ID,
        )
        self.question = Question(id=17, word="камера NASA & <тест>", options="фотограф,преступник")
        self.session = AsyncMock(spec=AsyncSession)
        self.session_factory = MagicMock()
        self.session_factory.return_value.__aenter__.return_value = self.session
        self.refresh = AsyncMock()
        self.remember = AsyncMock()
        self.statistics = {"фотограф": 0.0, "преступник": 100.0}
        self.patches = [
            patch.object(game, "async_session", self.session_factory),
            patch.object(game, "get_or_create_user", AsyncMock(return_value=SimpleNamespace(id=7))),
            patch.object(game, "get_question_statistics", AsyncMock(return_value=self.statistics)),
            patch.object(game, "render_statistics_chart", return_value=PNG),
            patch.object(game.chart_messages, "remember_chart", self.remember),
            patch.object(game, "refresh_question_charts", self.refresh),
        ]
        for active_patch in self.patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)

    async def asyncTearDown(self):
        await self.dp.storage.close()
        await self.bot.session.close()

    def callback(self, *, option=1):
        return types.CallbackQuery.model_validate({
            "id": "daily_callback",
            "from": {"id": USER_ID, "is_bot": False, "first_name": "Игрок"},
            "chat_instance": "private_chat",
            "data": f"autotest_answer:17:{option}",
            "message": {
                "message_id": 18,
                "date": 1700000000,
                "chat": {"id": USER_ID, "type": "private"},
                "from": {"id": self.bot.id, "is_bot": True, "first_name": "Быстротест"},
                "text": "Ежедневный вопрос",
            },
        }, context={"bot": self.bot})

    async def answer_daily_question(self, *, previous=None, option=1, update_id=1):
        self.session.execute.side_effect = [scalar_result(self.question), scalar_result(previous)]
        await self.dp.feed_update(self.bot, types.Update(
            update_id=update_id, callback_query=self.callback(option=option),
        ))

    def api_methods(self):
        return [call.args[1] for call in self.transport.await_args_list]

    async def test_daily_answer_works_after_restart_without_fsm_and_does_not_start_a_game(self):
        self.assertIsNone(await self.state.get_state())

        await self.answer_daily_question()

        self.session.add.assert_called_once()
        answer = self.session.add.call_args.args[0]
        self.assertIsInstance(answer, Answer)
        self.assertEqual((answer.user_id, answer.question_id, answer.choice), (7, 17, "преступник"))
        self.session.get.assert_awaited_once_with(User, 7, with_for_update=True)
        self.session.commit.assert_awaited_once()
        # Only the current question and the prior vote are queried. There is no
        # request for another question, even though no FSM state existed.
        self.assertEqual(self.session.execute.await_count, 2)
        methods = self.api_methods()
        self.assertEqual([method.__api_method__ for method in methods], [
            "answerCallbackQuery", "editMessageMedia",
        ])
        result = methods[-1]
        self.assertEqual(result.chat_id, USER_ID)
        self.assertEqual(result.message_id, 18)
        self.assertIn("Камера NASA &amp; &lt;тест&gt;", result.media.caption)
        self.assertIn("вы преступник", result.media.caption)
        self.assertEqual(result.media.media.data, PNG)
        self.assertIsNone(result.reply_markup)
        self.remember.assert_awaited_once()
        self.refresh.assert_awaited_once_with(
            self.bot, 17, game.get_question_statistics, statistics=self.statistics, chart=PNG,
        )
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})

    async def test_daily_answer_preserves_existing_game_or_admin_draft(self):
        for current_state, data in (
            (GameStates.waiting_for_answer, {"qid": 29}),
            (QuestionCreationStates.waiting_for_options, {"word": "Замок", "options": ["рыцарь", "слесарь"]}),
        ):
            with self.subTest(state=current_state.state):
                await self.state.set_state(current_state)
                await self.state.set_data(data)
                self.transport.reset_mock()

                await self.answer_daily_question()

                self.assertEqual(await self.state.get_state(), current_state.state)
                self.assertEqual(await self.state.get_data(), data)
                self.assertEqual([method.__api_method__ for method in self.api_methods()], [
                    "answerCallbackQuery", "editMessageMedia",
                ])

    async def test_repeated_daily_answer_keeps_saved_choice_without_another_vote(self):
        await self.answer_daily_question()
        saved = self.session.add.call_args.args[0]
        self.transport.reset_mock()

        await self.answer_daily_question(previous=saved, option=0, update_id=2)

        self.session.add.assert_called_once()
        result = self.api_methods()[-1]
        self.assertIn("вы преступник", result.media.caption)
        self.assertNotIn("вы фотограф", result.media.caption)


class AutotestMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_shows_autotest_in_private_and_group_chats(self):
        with patch.object(config, "ADMIN_ID", USER_ID):
            for user_id, chat_type, visible in (
                (USER_ID, ChatType.PRIVATE, True),
                (USER_ID + 1, ChatType.PRIVATE, True),
                (USER_ID, ChatType.GROUP, True),
                (USER_ID + 1, ChatType.SUPERGROUP, True),
            ):
                with self.subTest(user_id=user_id, chat_type=chat_type):
                    message = MagicMock(spec=types.Message)
                    message.from_user = SimpleNamespace(id=user_id)
                    message.chat = SimpleNamespace(type=chat_type)
                    message.answer = AsyncMock()

                    await start.cmd_start(message)

                    keyboard = message.answer.call_args.kwargs["reply_markup"]
                    texts = [button.text for row in keyboard.keyboard for button in row]
                    self.assertEqual("Автотест" in texts, visible)

    async def test_help_explains_daily_subscription_switch_in_private_chat(self):
        message = MagicMock(spec=types.Message)
        message.from_user = SimpleNamespace(id=USER_ID)
        message.chat = SimpleNamespace(type=ChatType.PRIVATE)
        message.answer = AsyncMock()

        await start.cmd_help(message)

        text = message.answer.call_args.args[0]
        self.assertIn("/autotest", text)
        self.assertIn("включить или отключить", text)


if __name__ == "__main__":
    unittest.main()
