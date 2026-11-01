import os
import unittest
from html.parser import HTMLParser
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, unquote, urlsplit

os.environ.setdefault("DB", "postgresql+asyncpg://user:password@localhost/bistro")

from aiogram import Bot, Dispatcher, types
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import create_engine, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from bistro.database.models import Answer, Question, User
from bistro.handlers import sharing
from bistro.handlers.states import QuestionCreationStates
from bistro.ui.sharing import get_result_keyboard


USER_ID = 123456789
OTHER_USER_ID = USER_ID + 1
QUESTION_ID = 42
BOT_USERNAME = "current_shared_question_bot"


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.payloads = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        self.payloads.append({
            key: self.prepare_value(value, bot=bot, files={})
            for key, value in method.model_dump(warnings=False).items()
            if value is not None
        })
        if method.__api_method__ == "getMe":
            return types.User(
                id=bot.id, is_bot=True, first_name="Быстротест", username=BOT_USERNAME,
            )
        return True

    async def stream_content(self, url, **kwargs):
        yield b""


class SharedText(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.visible = []
        self.links = []
        self.current_link = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.current_link = [dict(attrs)["href"], ""]
            self.links.append(self.current_link)

    def handle_endtag(self, tag):
        if tag == "a":
            self.current_link = None

    def handle_data(self, text):
        self.visible.append(text)
        if self.current_link is not None:
            self.current_link[1] += text


class SharingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = RecordingSession()
        self.bot = Bot(token="123456:TEST_TOKEN", session=self.transport)
        self.dp = Dispatcher(storage=MemoryStorage())
        sharing.register_handlers_sharing(self.dp)
        self.engine = create_engine("sqlite://")
        for model in (User, Question, Answer):
            model.__table__.create(self.engine)
        self.database = Session(self.engine)
        self.database.add_all([
            User(id=1, tg_id=USER_ID), User(id=2, tg_id=OTHER_USER_ID),
            Question(id=QUESTION_ID, word="наполеон", options="алкоголик,император", associo="коньяк,трон"),
            Question(id=7, word="камера", options="фотограф,преступник", associo="снимок,тюрьма"),
        ])
        self.database.add_all([
            Answer(id=1, user_id=1, question_id=QUESTION_ID, choice="алкоголик"),
            Answer(id=2, user_id=2, question_id=QUESTION_ID, choice="император"),
            Answer(id=3, user_id=1, question_id=7, choice="фотограф"),
        ])
        self.database.commit()
        self.session = AsyncMock(spec=AsyncSession)
        self.session.execute.side_effect = self.database.execute
        self.factory = MagicMock()
        self.factory.return_value.__aenter__.return_value = self.session
        active_patch = patch.object(sharing, "async_session", self.factory)
        active_patch.start()
        self.addCleanup(active_patch.stop)

    async def asyncTearDown(self):
        await self.dp.storage.close()
        await self.bot.session.close()
        self.database.close()
        self.engine.dispose()

    async def send(self, query=f"share:{QUESTION_ID}", *, user_id=USER_ID, update_id=1):
        inline = types.InlineQuery.model_validate({
            "id": f"inline_{update_id}",
            "from": {"id": user_id, "is_bot": False, "first_name": "Игрок"},
            "query": query,
            "offset": "",
            "chat_type": "private",
        }, context={"bot": self.bot})
        await self.dp.feed_update(self.bot, types.Update(update_id=update_id, inline_query=inline))

    def answers(self):
        return [method for method in self.transport.requests
                if method.__api_method__ == "answerInlineQuery"]

    def assert_personal_answer(self, method):
        self.assertEqual(method.cache_time, 0)
        self.assertIs(method.is_personal, True)

    def assert_shared_result(self, method, choice, question_id=QUESTION_ID):
        self.assert_personal_answer(method)
        self.assertEqual(len(method.results), 1)
        result = method.results[0]
        self.assertIsInstance(result, types.InlineQueryResultArticle)
        content = result.input_message_content
        self.assertIsInstance(content, types.InputTextMessageContent)
        self.assertEqual(content.parse_mode, "HTML")
        self.assertIs(content.link_preview_options.is_disabled, False)
        self.assertEqual(content.link_preview_options.url, (
            f"https://t.me/{BOT_USERNAME}?start=question_{question_id}"
        ))
        rendered = SharedText(content.message_text)
        self.assertEqual("".join(rendered.visible), (
            f"Оказывается я {choice}, узнай кто ты в Быстротесте"
        ))
        self.assertEqual(rendered.links, [[
            f"https://t.me/{BOT_USERNAME}?start=question_{question_id}", "Быстротесте",
        ]])

    async def test_share_button_prepares_plain_message_with_visible_original_question_link(self):
        for choice, question_id in (("алкоголик", QUESTION_ID), ('художник C++ & "тест" 🧑‍🎨', 7)):
            with self.subTest(choice=choice, question_id=question_id):
                keyboard = await get_result_keyboard(self.bot, choice, question_id)
                button = keyboard.inline_keyboard[0][0]
                self.assertEqual(button.text, "📤 Поделиться")
                self.assertIsNone(button.callback_data)
                self.assertIsNone(button.switch_inline_query)
                self.assertIsNone(button.switch_inline_query_current_chat)
                self.assertIsNone(button.switch_inline_query_chosen_chat)
                link = urlsplit(button.url)
                self.assertEqual((link.scheme, link.netloc, link.path), ("https", "t.me", "/share/url"))
                expected = (
                    f"Оказывается я {choice}, узнай кто ты в Быстротесте\n"
                    f"https://t.me/{BOT_USERNAME}?start=question_{question_id}"
                )
                self.assertEqual(parse_qs(link.query), {"url": [expected]})
                # iOS decodes percent escapes without treating '+' as a space.
                self.assertNotIn("+", link.query)
                self.assertEqual(unquote(link.query.removeprefix("url=")), expected)
                bot_link = urlsplit(expected.splitlines()[-1])
                self.assertEqual(bot_link.path, f"/{BOT_USERNAME}")
                self.assertEqual(parse_qs(bot_link.query), {"start": [f"question_{question_id}"]})

    async def test_legacy_inline_result_keeps_clickable_word_and_enables_original_question_preview(self):
        await self.send()

        self.assertEqual(len(self.answers()), 1)
        self.assert_shared_result(self.answers()[0], "алкоголик")
        self.session.add.assert_not_called()
        self.session.commit.assert_not_awaited()

    async def test_each_sender_shares_their_own_saved_result_with_personal_cache(self):
        await self.send(user_id=USER_ID)
        await self.send(user_id=OTHER_USER_ID, update_id=2)

        self.assertEqual(len(self.answers()), 2)
        self.assert_shared_result(self.answers()[0], "алкоголик")
        self.assert_shared_result(self.answers()[1], "император")
        self.assertNotEqual(self.answers()[0].results[0].id, self.answers()[1].results[0].id)

    async def test_special_characters_in_saved_choice_remain_text_without_changing_link(self):
        choice = 'художник C++ & <a href="https://example.invalid">🧑‍🎨</a>'
        answer = self.database.get(Answer, 1)
        answer.choice = choice
        self.database.commit()

        await self.send()

        self.assert_shared_result(self.answers()[0], choice)

    async def test_duplicate_historical_answers_share_the_first_saved_choice(self):
        self.database.add(Answer(user_id=1, question_id=QUESTION_ID, choice="другой результат"))
        self.database.commit()

        await self.send()

        self.assert_shared_result(self.answers()[0], "алкоголик")

    async def test_missing_or_reset_own_answer_never_uses_another_players_result(self):
        self.database.execute(delete(Answer).where(Answer.user_id == 1, Answer.question_id == QUESTION_ID))
        self.database.commit()
        for user_id in (USER_ID, USER_ID + 99):
            with self.subTest(user_id=user_id):
                self.transport.requests.clear()

                await self.send(user_id=user_id)

                self.assertEqual(len(self.answers()), 1)
                self.assertEqual(self.answers()[0].results, [])
                self.assert_personal_answer(self.answers()[0])

    async def test_unknown_or_deleted_question_returns_empty_result(self):
        await self.send("share:999")
        self.assertEqual(self.answers()[0].results, [])
        self.assert_personal_answer(self.answers()[0])

    async def test_invalid_queries_return_empty_results_without_database_access(self):
        invalid = (
            "", "other:42", "share:", "share:0", "share:-42", "share:+42", "share:٤٢",
            "share:４２", "share:2.5", "share:2147483648", "share:" + "1" * 100,
            f"share:{QUESTION_ID}:{OTHER_USER_ID}", "share:42:император",
        )
        for query in invalid:
            with self.subTest(query=query):
                self.transport.requests.clear()
                self.factory.reset_mock()

                await self.send(query)

                self.assertEqual(len(self.answers()), 1)
                self.assertEqual(self.answers()[0].results, [])
                self.assert_personal_answer(self.answers()[0])
                self.factory.assert_not_called()

    async def test_sharing_keeps_admin_draft_and_saved_answers_unchanged(self):
        state = self.dp.fsm.get_context(bot=self.bot, chat_id=USER_ID, user_id=USER_ID)
        await state.set_state(QuestionCreationStates.waiting_for_options)
        draft = {"word": "новый вопрос"}
        await state.set_data(draft)
        before = self.database.execute(select(Answer.id, Answer.choice).order_by(Answer.id)).all()

        await self.send()

        self.assert_shared_result(self.answers()[0], "алкоголик")
        self.assertEqual(await state.get_state(), QuestionCreationStates.waiting_for_options.state)
        self.assertEqual(await state.get_data(), draft)
        self.assertEqual(
            self.database.execute(select(Answer.id, Answer.choice).order_by(Answer.id)).all(), before,
        )


if __name__ == "__main__":
    unittest.main()
