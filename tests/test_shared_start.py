import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("DB", "postgresql+asyncpg://user:password@localhost/bistro")

from aiogram import Bot, Dispatcher, types
from aiogram.client.session.base import BaseSession
from aiogram.enums import ChatType
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import create_engine, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from bistro.database.models import Answer, Question, User
from bistro.handlers import admin, game, start
from bistro.handlers.states import GameStates, QuestionCreationStates
from bistro.ui.keyboards import get_main_menu
from bistro.ui.messages import MESSAGES
from bistro.ui.sharing import get_result_keyboard


USER_ID = 123456789
GROUP_ID = -100123456789
BOT_USERNAME = "shared_question_bot"


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if method.__api_method__ == "getMe":
            return types.User(
                id=bot.id, is_bot=True, first_name="Быстротест", username=BOT_USERNAME,
            )
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


class SharedStartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = RecordingSession()
        self.bot = Bot(token="123456:TEST_TOKEN", session=self.transport)
        self.dp = Dispatcher(storage=MemoryStorage())
        start.register_handlers_start(self.dp)
        admin.register_handlers_admin(self.dp)
        game.register_handlers_game(self.dp)

        self.engine = create_engine("sqlite://")
        for model in (User, Question, Answer):
            model.__table__.create(self.engine)
        self.database = Session(self.engine)
        self.user = User(id=1, tg_id=USER_ID)
        self.first = Question(id=1, word="цезарь", options="повар,император", associo="салат,Рим")
        self.shared = Question(
            id=42, word="наполеон & <тест>", options="кондитер,алкоголик", associo="торт,коньяк",
        )
        self.next = Question(id=43, word="замок", options="слесарь,рыцарь", associo="ключ,башня")
        self.database.add_all([self.user, self.first, self.shared, self.next])
        self.database.add_all([
            Answer(user_id=1, question_id=1, choice="повар"),
            Answer(user_id=1, question_id=42, choice="кондитер"),
        ])
        self.database.commit()

        self.session = AsyncMock(spec=AsyncSession)
        self.session.get.side_effect = self.database.get
        self.session.execute.side_effect = self.database.execute
        self.session.commit.side_effect = self.database.commit
        self.session.add.side_effect = self.database.add
        self.factory = MagicMock()
        self.factory.return_value.__aenter__.return_value = self.session
        self.menu = AsyncMock(return_value=get_main_menu())
        self.user_lookup = AsyncMock(return_value=self.user)
        self.patches = [
            patch.object(game, "async_session", self.factory),
            patch.object(game, "get_or_create_user", self.user_lookup),
            patch.object(start, "get_main_menu_for_chat", self.menu),
        ]
        for active_patch in self.patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)

    async def asyncTearDown(self):
        await self.dp.storage.close()
        await self.bot.session.close()
        self.database.close()
        self.engine.dispose()

    def state(self, chat_type=ChatType.PRIVATE):
        return self.dp.fsm.get_context(
            bot=self.bot, user_id=USER_ID,
            chat_id=USER_ID if chat_type == ChatType.PRIVATE else GROUP_ID,
        )

    async def send(self, payload="question_42", *, chat_type=ChatType.PRIVATE,
                   with_mention=False, update_id=1):
        command = f"/start@{BOT_USERNAME}" if with_mention else "/start"
        text = command if payload is None else f"{command} {payload}"
        message = types.Message.model_validate({
            "message_id": update_id + 10,
            "date": 1700000000,
            "chat": {
                "id": USER_ID if chat_type == ChatType.PRIVATE else GROUP_ID,
                "type": chat_type,
            },
            "from": {"id": USER_ID, "is_bot": False, "first_name": "Игрок"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(command)}],
        }, context={"bot": self.bot})
        await self.dp.feed_update(self.bot, types.Update(update_id=update_id, message=message))

    def responses(self):
        return [method for method in self.transport.requests if method.__api_method__ == "sendMessage"]

    def saved_answers(self):
        return self.database.execute(
            select(Answer.question_id, Answer.choice).where(Answer.user_id == 1).order_by(Answer.id)
        ).all()

    async def assert_question(self, expected, *, response_index=0):
        method = self.responses()[response_index]
        self.assertEqual(method.text, game.question_text(expected))
        self.assertEqual(method.parse_mode, "HTML")
        buttons = [button for row in method.reply_markup.inline_keyboard for button in row]
        self.assertEqual([button.callback_data for button in buttons], [
            f"answer:{expected.id}:{index}" for index in range(len(expected.associo.split(",")))
        ])
        self.assertEqual(await self.state().get_state(), GameStates.waiting_for_answer.state)
        self.assertEqual((await self.state().get_data())["qid"], expected.id)

    async def test_new_recipient_gets_linked_question_before_lower_numbered_questions(self):
        self.database.execute(delete(Answer))
        self.database.commit()
        keyboard = await get_result_keyboard(self.bot, "алкоголик", self.shared.id)
        draft = parse_qs(urlsplit(keyboard.inline_keyboard[0][0].url).query)["url"][0]
        self.assertEqual(draft, (
            "Оказывается я алкоголик, узнай кто ты в Быстроботе\n"
            f"https://t.me/{BOT_USERNAME}?start=question_42"
        ))
        payload = parse_qs(urlsplit(draft.splitlines()[-1]).query)["start"][0]

        await self.send(payload)

        self.assertEqual(len(self.responses()), 1)
        await self.assert_question(self.shared)
        self.menu.assert_not_awaited()
        self.assertEqual(self.saved_answers(), [])

    async def test_existing_recipient_gets_linked_question_even_when_already_answered(self):
        before = self.saved_answers()

        await self.send(with_mention=True)

        self.assertEqual(len(self.responses()), 1)
        await self.assert_question(self.shared)
        self.assertEqual(self.saved_answers(), before)
        self.session.add.assert_not_called()
        self.menu.assert_not_awaited()

    async def test_reopening_shared_link_keeps_history_and_same_question(self):
        before = self.saved_answers()
        await self.send()
        await self.send(update_id=2)

        self.assertEqual(len(self.responses()), 2)
        await self.assert_question(self.shared, response_index=0)
        await self.assert_question(self.shared, response_index=1)
        self.assertEqual(self.saved_answers(), before)
        self.session.add.assert_not_called()

    async def test_missing_question_explains_unavailability_then_starts_normal_unanswered_question(self):
        before = self.saved_answers()

        await self.send("question_999")

        self.assertEqual(len(self.responses()), 2)
        self.assertIn("недоступен", self.responses()[0].text.casefold())
        await self.assert_question(self.next, response_index=1)
        self.assertEqual(self.saved_answers(), before)
        self.menu.assert_not_awaited()

    async def test_incomplete_shared_question_falls_back_without_showing_broken_choices(self):
        self.shared.associo = "торт"
        self.database.commit()
        before = self.saved_answers()

        await self.send()

        self.assertEqual(len(self.responses()), 2)
        self.assertIn("недоступен", self.responses()[0].text.casefold())
        await self.assert_question(self.next, response_index=1)
        self.assertEqual(self.saved_answers(), before)

    async def test_unavailable_shared_question_with_no_new_words_reports_game_over(self):
        self.database.add(Answer(user_id=1, question_id=43, choice="рыцарь"))
        self.database.commit()

        await self.send("question_999")

        self.assertEqual(len(self.responses()), 2)
        self.assertIn("недоступен", self.responses()[0].text.casefold())
        self.assertEqual(self.responses()[1].text, MESSAGES["game_over"])

    async def test_plain_unknown_or_malformed_start_keeps_menu_and_active_draft(self):
        draft = {"word": "черновик", "confirmation_message_id": 77}
        payloads = (
            None, "", "other_42", "question_", "question_0", "question_-1", "question_+42",
            "question_2.5", "question_2147483648", "question_٤٢", "question_４２",
            "question_42_extra", "question_42 extra", "question_" + "1" * 100,
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                state = self.state()
                await state.set_state(QuestionCreationStates.waiting_for_confirmation)
                await state.set_data(draft)
                self.transport.requests.clear()
                self.factory.reset_mock()

                await self.send(payload)

                self.assertEqual(len(self.responses()), 1)
                self.assertEqual(self.responses()[0].text, MESSAGES["start"])
                self.assertIsInstance(self.responses()[0].reply_markup, types.ReplyKeyboardMarkup)
                self.assertEqual(await state.get_state(), QuestionCreationStates.waiting_for_confirmation.state)
                self.assertEqual(await state.get_data(), draft)
                self.factory.assert_not_called()

    async def test_group_start_payload_restores_menu_without_posting_a_personal_question(self):
        for chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
            with self.subTest(chat_type=chat_type):
                self.transport.requests.clear()

                await self.send(chat_type=chat_type)

                self.assertEqual(len(self.responses()), 1)
                self.assertEqual(self.responses()[0].text, MESSAGES["start"])
                self.assertIsNone(await self.state(chat_type).get_state())
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
