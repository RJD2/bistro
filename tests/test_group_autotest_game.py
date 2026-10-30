import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DB", "postgresql+asyncpg://user:password@localhost/bistro")

from aiogram import Bot, Dispatcher, types
from aiogram.client.session.base import BaseSession
from aiogram.enums import ChatType
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession

from handlers import game
from models import Answer, Question, User


CHAT_ID = -100123456789
QUESTION_ID = 7
SHARED_MESSAGE_ID = 88
PNG = b"\x89PNG\r\n\x1a\ndaily chart"


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        files = {}
        payload = {
            key: self.prepare_value(value, bot=bot, files=files)
            for key, value in method.model_dump(warnings=False).items()
            if value is not None
        }
        self.requests.append((method, payload, files))
        if method.__returning__ is bool:
            return True
        ephemeral = getattr(method, "ephemeral_message_parameters", None)
        response = {
            "message_id": 0 if ephemeral else len(self.requests) + 100,
            "date": 1700000000,
            "chat": {"id": CHAT_ID, "type": "supergroup"},
        }
        if ephemeral:
            response["ephemeral_message_id"] = len(self.requests) + 1000
        return types.Message.model_validate(response, context={"bot": bot})

    async def stream_content(self, url, **kwargs):
        yield b""


def scalar_result(value):
    result = MagicMock()
    result.scalars.return_value.first.return_value = value
    return result


class GroupAutotestAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = RecordingSession()
        self.bot = Bot(token="123456:TEST_TOKEN", session=self.transport)
        self.dp = Dispatcher(storage=MemoryStorage())
        game.register_handlers_game(self.dp)
        self.session = AsyncMock(spec=AsyncSession)
        factory = MagicMock()
        factory.return_value.__aenter__.return_value = self.session
        self.question = Question(
            id=QUESTION_ID, word="камера SQL & <тест>", options="фотограф,преступник", associo="снимок,тюрьма",
        )
        self.lookup = AsyncMock(side_effect=lambda tg_id, session: SimpleNamespace(id=tg_id + 1000))
        self.remember = AsyncMock()
        self.refresh = AsyncMock()
        self.patches = [
            patch.object(game, "async_session", factory),
            patch.object(game, "get_or_create_user", self.lookup),
            patch.object(game, "get_question_statistics", AsyncMock(return_value={
                "фотограф": 50.0, "преступник": 50.0,
            })),
            patch.object(game, "render_statistics_chart", return_value=PNG),
            patch.object(game.chart_messages, "remember_chart", self.remember),
            patch.object(game.chart_messages, "forget_message", AsyncMock()),
            patch.object(game, "refresh_question_charts", self.refresh),
        ]
        for active_patch in self.patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)

    async def asyncTearDown(self):
        await self.dp.storage.close()
        await self.bot.session.close()

    def callback(self, user_id, option, chat_type):
        return types.CallbackQuery.model_validate({
            "id": f"daily_{user_id}_{option}",
            "from": {"id": user_id, "is_bot": False, "first_name": f"Игрок {user_id}"},
            "chat_instance": "shared_daily_question",
            "data": f"autotest_answer:{QUESTION_ID}:{option}",
            "message": {
                "message_id": SHARED_MESSAGE_ID,
                "date": 1700000000,
                "chat": {"id": CHAT_ID, "type": chat_type},
                "from": {"id": self.bot.id, "is_bot": True, "first_name": "Быстротест"},
                "text": "Общий ежедневный Быстротест",
            },
        }, context={"bot": self.bot})

    async def answer(self, user_id, option, chat_type, *, existing=None, update_id=1):
        self.session.execute.reset_mock()
        self.session.execute.side_effect = [
            scalar_result(self.question), scalar_result(existing),
        ]
        callback = self.callback(user_id, option, chat_type)
        await self.dp.feed_update(
            self.bot, types.Update(update_id=update_id, callback_query=callback),
        )
        self.assertEqual(self.session.execute.await_count, 2)
        return callback

    def photos(self):
        return [request for request in self.transport.requests
                if request[0].__api_method__ == "sendPhoto"]

    def assert_shared_question_untouched(self):
        for method, _, _ in self.transport.requests:
            self.assertIn(method.__api_method__, {"answerCallbackQuery", "sendPhoto"})
            if method.__api_method__ == "sendPhoto":
                self.assertEqual(method.chat_id, CHAT_ID)
                self.assertIsNone(method.reply_markup)

    async def test_two_members_answer_same_daily_question_and_only_see_their_own_result(self):
        for chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
            with self.subTest(chat_type=chat_type):
                self.transport.requests.clear()
                self.session.add.reset_mock()
                self.session.get.reset_mock()
                self.remember.reset_mock()
                self.refresh.reset_mock()
                callbacks = []
                for update_id, (user_id, option) in enumerate(((111, 0), (222, 1)), 1):
                    state = self.dp.fsm.get_context(
                        bot=self.bot, chat_id=CHAT_ID, user_id=user_id,
                    )
                    self.assertIsNone(await state.get_state())
                    callbacks.append(await self.answer(
                        user_id, option, chat_type, update_id=update_id,
                    ))
                    self.assertIsNone(await state.get_state())
                    self.assertEqual(await state.get_data(), {})

                saved = [call.args[0] for call in self.session.add.call_args_list]
                self.assertTrue(all(isinstance(answer, Answer) for answer in saved))
                self.assertEqual(
                    [(answer.user_id, answer.question_id, answer.choice) for answer in saved],
                    [(1111, QUESTION_ID, "фотограф"), (1222, QUESTION_ID, "преступник")],
                )
                self.session.get.assert_any_await(User, 1111, with_for_update=True)
                self.session.get.assert_any_await(User, 1222, with_for_update=True)
                photos = self.photos()
                self.assertEqual(len(photos), 2)
                for request, callback, choice in zip(photos, callbacks, ("фотограф", "преступник")):
                    method, payload, files = request
                    self.assertEqual(method.parse_mode, "HTML")
                    self.assertIn("Камера SQL &amp; &lt;тест&gt;", method.caption)
                    self.assertIn(f"вы {choice}", method.caption)
                    self.assertEqual(json.loads(payload["ephemeral_message_parameters"]), {
                        "receiver_user_id": callback.from_user.id,
                        "callback_query_id": callback.id,
                        "replace_callback_query_message": True,
                    })
                    self.assertEqual(next(iter(files.values())).data, PNG)
                    self.assertEqual(callback.message.message_id, SHARED_MESSAGE_ID)
                    self.assertIsNone(getattr(callback.message, "receiver_user", None))
                self.assertEqual(
                    [call.kwargs["receiver_user_id"] for call in self.remember.await_args_list],
                    [111, 222],
                )
                self.assertEqual(self.refresh.await_count, 2)
                self.assert_shared_question_untouched()

    async def test_repeat_daily_click_retains_saved_choice_without_another_vote_or_question(self):
        await self.answer(111, 0, ChatType.SUPERGROUP)
        first_answer = self.session.add.call_args.args[0]

        await self.answer(
            111, 1, ChatType.SUPERGROUP,
            existing=Answer(
                id=9, user_id=1111, question_id=QUESTION_ID, choice=first_answer.choice,
            ),
            update_id=2,
        )

        self.session.add.assert_called_once()
        photos = self.photos()
        self.assertEqual(len(photos), 2)
        self.assertIn("вы фотограф", photos[-1][0].caption)
        self.assertNotIn("вы преступник", photos[-1][0].caption)
        self.assert_shared_question_untouched()


if __name__ == "__main__":
    unittest.main()
