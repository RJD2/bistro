import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DB", "postgresql+asyncpg://user:password@localhost/bistro")

from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ChatType
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from bistro import config
from bistro.handlers import admin, game, start, statistics
from bistro.handlers.states import (
    AssocioCreationStates, BulkQuestionCreationStates, GameStates, QuestionCreationStates,
)
from bistro.services.statistics import BotStatistics, PopularQuestion
from bistro.ui.keyboards import BOT_STATISTICS_BUTTON, get_main_menu


OWNER_ID = 123456789
GROUP_ID = -100123456789
BOT_USERNAME = "bot_statistics_test_bot"


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if method.__api_method__ == "getMe":
            return types.User(id=bot.id, is_bot=True, first_name="Бот", username=BOT_USERNAME)
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


class AdminBotStatisticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = RecordingSession()
        self.bot = Bot(
            token="123456:TEST_TOKEN", session=self.transport,
            default=DefaultBotProperties(parse_mode="HTML"),
        )
        self.dp = Dispatcher(storage=MemoryStorage())
        statistics.register_handlers_statistics(self.dp)
        start.register_handlers_start(self.dp)
        admin.register_handlers_admin(self.dp)
        game.register_handlers_game(self.dp)
        self.session = AsyncMock(spec=AsyncSession)
        self.database = MagicMock()
        self.database.return_value.__aenter__.return_value = self.session
        self.statistics = AsyncMock(return_value=BotStatistics(
            total_participants=3, total_questions=8, total_answers=10,
            most_popular_question=PopularQuestion(
                id=7, word="камера NASA <&> _тест_", answer_count=3,
            ),
        ))
        for active_patch in (
            patch.object(config, "ADMIN_ID", OWNER_ID),
            patch.object(admin, "async_session", self.database),
            patch.object(admin, "get_bot_statistics", self.statistics),
        ):
            active_patch.start()
            self.addCleanup(active_patch.stop)

    async def asyncTearDown(self):
        await self.dp.storage.close()
        await self.bot.session.close()

    async def send(self, text=BOT_STATISTICS_BUTTON, *, user_id=OWNER_ID,
                   chat_type=ChatType.PRIVATE, with_user=True):
        message = {
            "message_id": 10, "date": 1700000000,
            "chat": {
                "id": user_id if chat_type == ChatType.PRIVATE else GROUP_ID,
                "type": chat_type,
            },
            "text": text,
        }
        if with_user:
            message["from"] = {"id": user_id, "is_bot": False, "first_name": "Игрок"}
        if text.startswith("/"):
            message["entities"] = [{
                "type": "bot_command", "offset": 0, "length": len(text.split()[0]),
            }]
        await self.dp.feed_update(self.bot, types.Update(
            update_id=1, message=types.Message.model_validate(message, context={"bot": self.bot}),
        ))

    def response(self):
        messages = [item for item in self.transport.requests if item.__api_method__ == "sendMessage"]
        self.assertEqual(len(messages), 1)
        return messages[0]

    async def test_button_and_commands_show_all_metrics_with_literal_question_name(self):
        for text in (
            BOT_STATISTICS_BUTTON, "Статистика бота", "/bot_statistics",
            f"/bot_statistics@{BOT_USERNAME}",
        ):
            with self.subTest(text=text):
                self.transport.requests.clear()
                self.statistics.reset_mock()

                await self.send(text)

                self.statistics.assert_awaited_once_with(self.session)
                response = self.response()
                for expected in (
                    "Всего участников: 3", "Всего вопросов: 8", "Всего ответов на вопросы: 10",
                    "В среднем вопросов на участника: 3,33",
                    "Самый популярный вопрос: «Камера NASA <&> _тест_»",
                    "Ответивших участников: 3", "Сброшенные ответы не учитываются.",
                ):
                    self.assertIn(expected, response.text)
                self.assertIsNone(response.parse_mode)
                self.session.add.assert_not_called()
                self.session.commit.assert_not_awaited()

    async def test_unauthorized_requests_do_not_open_database(self):
        cases = (
            {"user_id": OWNER_ID + 1},
            {"chat_type": ChatType.GROUP},
            {"chat_type": ChatType.SUPERGROUP},
            {"with_user": False},
        )
        for kwargs in cases:
            for text in (BOT_STATISTICS_BUTTON, "/bot_statistics"):
                with self.subTest(kwargs=kwargs, text=text):
                    self.transport.requests.clear()
                    await self.send(text, **kwargs)
                    self.assertIn("только владельцу в личном чате", self.response().text)
        with patch.object(config, "ADMIN_ID", 0):
            self.transport.requests.clear()
            await self.send()
            self.assertIn("только владельцу в личном чате", self.response().text)
        self.database.assert_not_called()
        self.statistics.assert_not_awaited()

    async def test_empty_statistics_has_zero_average_and_no_popular_question(self):
        self.statistics.return_value = BotStatistics(0, 0, 0, None)

        await self.send()

        response = self.response()
        self.assertIn("Всего участников: 0", response.text)
        self.assertIn("Всего вопросов: 0", response.text)
        self.assertIn("Всего ответов на вопросы: 0", response.text)
        self.assertIn("В среднем вопросов на участника: 0,00", response.text)
        self.assertIn("Самый популярный вопрос: нет данных.", response.text)

    async def test_database_error_produces_readable_failure(self):
        self.statistics.side_effect = SQLAlchemyError("database unavailable")

        with self.assertLogs(admin.logger, level="ERROR"):
            await self.send()

        text = self.response().text
        self.assertIn("Не удалось загрузить статистику бота", text)
        self.assertIn("Попробуйте еще раз позже", text)
        self.assertNotIn("database unavailable", text)

    async def test_viewing_statistics_preserves_game_and_every_admin_wizard_state(self):
        states = (
            GameStates.waiting_for_answer,
            *QuestionCreationStates.__all_states__,
            *BulkQuestionCreationStates.__all_states__,
            *AssocioCreationStates.__all_states__,
        )
        state = self.dp.fsm.get_context(bot=self.bot, chat_id=OWNER_ID, user_id=OWNER_ID)
        draft = {"qid": 7, "word": "черновик", "bulk_words": ["камера"], "confirmation_message_id": 99}
        for current_state in states:
            for text in (BOT_STATISTICS_BUTTON, "/bot_statistics"):
                with self.subTest(state=current_state.state, text=text):
                    await state.set_state(current_state)
                    await state.set_data(draft)
                    self.transport.requests.clear()
                    self.statistics.reset_mock()

                    await self.send(text)

                    self.statistics.assert_awaited_once_with(self.session)
                    self.assertIn(BOT_STATISTICS_BUTTON, self.response().text)
                    self.assertEqual(await state.get_state(), current_state.state)
                    self.assertEqual(await state.get_data(), draft)

    async def test_menu_button_and_help_command_are_admin_only(self):
        for is_admin in (False, True):
            menu = get_main_menu(is_admin=is_admin)
            buttons = [button.text for row in menu.keyboard for button in row]
            self.assertEqual(BOT_STATISTICS_BUTTON in buttons, is_admin)
        for kwargs, expected in (
            ({}, True),
            ({"user_id": OWNER_ID + 1}, False),
            ({"chat_type": ChatType.GROUP}, False),
        ):
            with self.subTest(kwargs=kwargs):
                self.transport.requests.clear()
                await self.send("/help", **kwargs)
                self.assertEqual("/bot_statistics" in self.response().text, expected)
        self.database.assert_not_called()
        self.statistics.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
