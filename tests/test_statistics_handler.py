import json
import os
import unittest
from contextlib import asynccontextmanager
from html import escape
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, unquote, urlsplit

os.environ.setdefault("DB", "postgresql+asyncpg://user:password@localhost/bistro")

from aiogram import Bot, Dispatcher, types
from aiogram.client.session.base import BaseSession
from aiogram.enums import ChatType
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from bistro.handlers import admin, statistics
from bistro.charts import updates as chart_updates
from bistro.ui.keyboards import get_main_menu
from bistro.database.models import Answer, Question, User
from bistro.handlers.states import GameStates, QuestionCreationStates


USER_ID = 222
GROUP_ID = -100123456789
PNG = b"\x89PNG\r\n\x1a\nstatistics"


class RecordingSession(BaseSession):
    """Record real aiogram requests without contacting Telegram"""

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
        chat_id = method.chat_id
        response = {
            "message_id": 0 if ephemeral else len(self.requests) + 100,
            "date": 1700000000,
            "chat": {
                "id": chat_id,
                "type": "private" if chat_id > 0 else "supergroup",
            },
        }
        if ephemeral:
            response["ephemeral_message_id"] = len(self.requests) + 1000
        return types.Message.model_validate(response, context={"bot": bot})

    async def stream_content(self, url, **kwargs):
        yield b""


class AnsweredQuestionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_database_lists_each_own_answered_word_once_across_pages(self):
        engine = create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        for model in (User, Question, Answer):
            model.__table__.create(engine)
        with Session(engine) as database:
            database.connection().exec_driver_sql("DROP INDEX uq_answers_user_question")
            database.add_all([
                User(id=1, tg_id=USER_ID), User(id=2, tg_id=USER_ID + 1),
                *[
                    Question(id=qid, word=f"Слово {qid}", options="первый,второй")
                    for qid in range(1, 15)
                ],
            ])
            database.add_all([
                Answer(user_id=1, question_id=qid, choice="первый")
                for qid in range(2, 14)
            ])
            database.add_all([
                Answer(user_id=1, question_id=2, choice="первый"),
                Answer(user_id=2, question_id=1, choice="второй"),
                Answer(user_id=2, question_id=2, choice="второй"),
            ])
            database.commit()
            session = AsyncMock(spec=AsyncSession)
            session.execute.side_effect = database.execute

            first, count, page = await statistics.get_answered_questions(USER_ID, 0, session)
            self.assertEqual([question.id for question in first], list(range(2, 12)))
            self.assertEqual((count, page), (12, 0))
            last, count, page = await statistics.get_answered_questions(USER_ID, 999, session)
            self.assertEqual([question.id for question in last], [12, 13])
            self.assertEqual((count, page), (12, 1))
            other, count, page = await statistics.get_answered_questions(USER_ID + 1, 0, session)
            self.assertEqual([question.id for question in other], [1, 2])
            self.assertEqual((count, page), (2, 0))
            self.assertEqual(
                await statistics.get_answered_questions(USER_ID + 2, 0, session), ([], 0, 0),
            )

    async def test_queries_count_unique_words_and_are_scoped_to_telegram_user(self):
        session = AsyncMock(spec=AsyncSession)
        count = MagicMock()
        count.scalar_one.return_value = 21
        questions = [Question(id=21, word="камера", options="фотограф,преступник")]
        rows = MagicMock()
        rows.scalars.return_value.all.return_value = questions
        session.execute.side_effect = [count, rows]

        self.assertEqual(
            await statistics.get_answered_questions(USER_ID, 999, session),
            (questions, 21, 2),
        )

        statements = [
            call.args[0].compile(dialect=postgresql.dialect())
            for call in session.execute.await_args_list
        ]
        for statement in statements:
            sql = str(statement)
            self.assertIn("EXISTS", sql)
            self.assertIn("users.tg_id =", sql)
            self.assertIn(USER_ID, statement.params.values())
            self.assertIn("answers.question_id = questions.id", sql)
        self.assertIn("ORDER BY questions.id", str(statements[1]))
        self.assertIn(10, statements[1].params.values())
        self.assertIn(20, statements[1].params.values())
        session.add.assert_not_called()
        session.commit.assert_not_awaited()

    async def test_empty_history_has_no_second_query_and_does_not_create_a_user(self):
        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one.return_value = 0
        session.execute.return_value = result

        self.assertEqual(
            await statistics.get_answered_questions(USER_ID, 5, session), ([], 0, 0),
        )
        session.execute.assert_awaited_once()
        session.add.assert_not_called()
        session.commit.assert_not_awaited()


class StatisticsHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = RecordingSession()
        self.bot = Bot(token="123456:TEST_TOKEN", session=self.transport)
        self.bot.me = AsyncMock(return_value=types.User(
            id=self.bot.id, is_bot=True, first_name="Быстротест", username="test_bistro_bot",
        ))
        self.dp = Dispatcher(storage=MemoryStorage())
        statistics.register_handlers_statistics(self.dp)
        admin.register_handlers_admin(self.dp)
        self.session = AsyncMock(spec=AsyncSession)
        self.factory = MagicMock()
        self.factory.return_value.__aenter__.return_value = self.session
        self.question = Question(
            id=1, word="камера NASA & <тест>", options="фотограф SQL & художник,преступник",
        )
        self.questions = AsyncMock(return_value=([self.question], 1, 0))
        self.get_statistics = AsyncMock(return_value={
            "фотограф SQL & художник": 75.0, "преступник": 25.0,
        })
        self.renderer = MagicMock(return_value=PNG)
        self.remember = AsyncMock()
        self.forget = AsyncMock()
        self.refresh = AsyncMock()
        self.patches = [
            patch.object(statistics, "async_session", self.factory),
            patch.object(statistics, "get_answered_questions", self.questions),
            patch.object(statistics, "get_question_statistics", self.get_statistics),
            patch.object(statistics, "render_statistics_chart", self.renderer),
            patch.object(statistics.chart_messages, "remember_chart", self.remember),
            patch.object(statistics.chart_messages, "forget_message", self.forget),
            patch.object(chart_updates, "refresh_question_charts", self.refresh),
        ]
        for active_patch in self.patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)

    def assert_share_button(self, markup, choice):
        self.assertIsNotNone(markup)
        buttons = [button for row in markup.inline_keyboard for button in row]
        self.assertEqual(len(buttons), 1)
        button = buttons[0]
        self.assertEqual(button.text, "📤 Поделиться")
        self.assertIsNone(button.callback_data)
        self.assertIsNone(button.switch_inline_query)
        self.assertIsNone(button.switch_inline_query_current_chat)
        self.assertIsNone(button.switch_inline_query_chosen_chat)
        link = urlsplit(button.url)
        self.assertEqual((link.scheme, link.netloc, link.path), ("https", "t.me", "/share/url"))
        expected = (
            f"Оказывается я {choice}, узнай кто ты в Быстротесте\n"
            f"https://t.me/test_bistro_bot?start=question_{self.question.id}"
        )
        self.assertEqual(parse_qs(link.query), {"url": [expected]})
        # iOS decodes percent escapes without converting plus signs to spaces.
        self.assertNotIn("+", link.query)
        self.assertEqual(unquote(link.query.removeprefix("url=")), expected)

    async def asyncTearDown(self):
        await self.dp.storage.close()
        await self.bot.session.close()

    def message(self, text="📊 Статистика", *, chat_type=ChatType.PRIVATE, user_id=USER_ID,
                with_user=True, ephemeral_id=None):
        payload = {
            "message_id": 17 if ephemeral_id is None else 0,
            "date": 1700000000,
            "chat": {
                "id": user_id if chat_type == ChatType.PRIVATE else GROUP_ID,
                "type": chat_type,
            },
            "text": text,
        }
        if with_user:
            payload["from"] = {"id": user_id, "is_bot": False, "first_name": "Игрок"}
        if ephemeral_id is not None:
            payload["ephemeral_message_id"] = ephemeral_id
        if text.startswith("/"):
            payload["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text)}]
        return types.Message.model_validate(payload, context={"bot": self.bot})

    def callback(self, data="statistics:open", *, chat_type=ChatType.PRIVATE,
                 user_id=USER_ID, ephemeral_id=None):
        return types.CallbackQuery.model_validate({
            "id": f"callback_{user_id}",
            "from": {"id": user_id, "is_bot": False, "first_name": "Игрок"},
            "chat_instance": "statistics_chat",
            "data": data,
            "message": self.message(
                chat_type=chat_type, user_id=user_id, ephemeral_id=ephemeral_id,
            ),
        }, context={"bot": self.bot})

    async def feed_message(self, text="📊 Статистика", **kwargs):
        await self.dp.feed_update(self.bot, types.Update(
            update_id=1, message=self.message(text, **kwargs),
        ))

    async def feed_callback(self, data="statistics:open", **kwargs):
        await self.dp.feed_update(self.bot, types.Update(
            update_id=2, callback_query=self.callback(data, **kwargs),
        ))

    def requests(self, api_method):
        return [item for item in self.transport.requests if item[0].__api_method__ == api_method]

    def configure_saved_choice(self, choice="фотограф SQL & художник"):
        result = MagicMock()
        result.first.return_value = (self.question, choice)
        self.session.execute.return_value = result

    async def test_menu_button_and_commands_list_saved_words_without_changing_state(self):
        for is_admin in (False, True):
            menu = get_main_menu(is_admin=is_admin)
            self.assertIn("📊 Статистика", [button.text for row in menu.keyboard for button in row])

        state = self.dp.fsm.get_context(bot=self.bot, chat_id=USER_ID, user_id=USER_ID)
        for current_state in (GameStates.waiting_for_answer, QuestionCreationStates.waiting_for_word):
            for text in ("📊 Статистика", "Статистика", "/statistics"):
                with self.subTest(state=current_state, text=text):
                    await state.set_state(current_state)
                    await state.set_data({"qid": 42, "word": "Черновик"})
                    self.transport.requests.clear()
                    self.questions.reset_mock()

                    await self.feed_message(text)

                    self.questions.assert_awaited_once_with(USER_ID, 0, self.session)
                    method = self.requests("sendMessage")[0][0]
                    button = method.reply_markup.inline_keyboard[0][0]
                    self.assertEqual(button.text, "Камера NASA & <тест>")
                    self.assertEqual(button.callback_data, f"statistics:word:{USER_ID}:1")
                    self.assertEqual(await state.get_state(), current_state.state)
                    self.assertEqual(await state.get_data(), {"qid": 42, "word": "Черновик"})
                    self.session.add.assert_not_called()
                    self.session.commit.assert_not_awaited()

    async def test_empty_history_explains_how_to_start_without_word_buttons(self):
        self.questions.return_value = ([], 0, 0)

        await self.feed_message()

        method = self.requests("sendMessage")[0][0]
        self.assertTrue(method.text)
        self.assertFalse(method.reply_markup and method.reply_markup.inline_keyboard)
        self.renderer.assert_not_called()

    async def test_pagination_replaces_list_and_keeps_word_selection_available(self):
        self.questions.return_value = ([self.question], 21, 1)

        await self.feed_callback(f"statistics:page:{USER_ID}:1")

        self.questions.assert_awaited_once_with(USER_ID, 1, self.session)
        self.assertEqual(len(self.requests("answerCallbackQuery")), 1)
        method = self.requests("editMessageText")[0][0]
        callbacks = [button.callback_data for row in method.reply_markup.inline_keyboard for button in row]
        self.assertIn(f"statistics:word:{USER_ID}:1", callbacks)
        self.assertIn(f"statistics:page:{USER_ID}:0", callbacks)
        self.assertIn(f"statistics:page:{USER_ID}:2", callbacks)
        self.assertEqual(self.requests("sendPhoto"), [])

    async def test_selected_word_displays_exact_saved_answer_as_a_static_snapshot(self):
        choice = "фОтоГраф SQL & <художник>"
        self.configure_saved_choice(choice)

        await self.feed_callback(f"statistics:word:{USER_ID}:1")

        self.assertEqual(len(self.requests("answerCallbackQuery")), 1)
        photo, payload, files = self.requests("sendPhoto")[0]
        self.assertEqual(photo.caption, (
            "<b>Камера NASA &amp; &lt;тест&gt;</b>\n\n"
            "Это был быстротест фотограф SQL &amp; художник вы или преступник\n\n"
            f"И вы {escape(choice)}"
        ))
        self.assertEqual(photo.parse_mode, "HTML")
        self.assert_share_button(photo.reply_markup, choice)
        self.assertEqual(next(iter(files.values())).data, PNG)
        self.assertNotIn("ephemeral_message_parameters", payload)
        self.assertEqual(self.requests("editMessageText"), [])
        self.assertEqual(self.requests("editMessageMedia"), [])
        self.get_statistics.assert_awaited_once_with(1, self.session)
        self.renderer.assert_called_once_with(self.get_statistics.return_value, selected_choice=choice)
        self.remember.assert_not_awaited()
        self.refresh.assert_not_awaited()
        self.forget.assert_not_awaited()
        statement = self.session.execute.await_args.args[0].compile(dialect=postgresql.dialect())
        self.assertIn("users.tg_id =", str(statement))
        self.assertIn("questions.id =", str(statement))
        self.assertIn(USER_ID, statement.params.values())
        self.assertIn(1, statement.params.values())
        self.session.add.assert_not_called()
        self.session.commit.assert_not_awaited()

    async def test_reopening_statistics_renders_current_percentages_without_editing_old_snapshots(self):
        self.configure_saved_choice()
        first = {"фотограф SQL & художник": 75.0, "преступник": 25.0}
        latest = {"фотограф SQL & художник": 50.0, "преступник": 50.0}
        self.get_statistics.side_effect = [first, latest]
        self.renderer.side_effect = [b"first chart", b"latest chart"]

        await self.feed_callback(f"statistics:word:{USER_ID}:1")
        await self.feed_callback(f"statistics:word:{USER_ID}:1")

        self.assertEqual(self.get_statistics.await_count, 2)
        self.assertEqual([call.args[0] for call in self.renderer.call_args_list], [first, latest])
        self.assertEqual([
            next(iter(files.values())).data
            for _, _, files in self.requests("sendPhoto")
        ], [b"first chart", b"latest chart"])
        self.assertEqual(self.requests("editMessageMedia"), [])
        self.assertEqual(self.requests("editEphemeralMessageMedia"), [])
        self.remember.assert_not_awaited()
        self.refresh.assert_not_awaited()

    async def test_group_statistics_forgets_reused_game_target_while_recipient_is_locked(self):
        self.configure_saved_choice()
        live_targets = {1002: "old game chart"}
        locked = False

        @asynccontextmanager
        async def guard(bot_id, chat_id, receiver_id):
            nonlocal locked
            self.assertEqual((bot_id, chat_id, receiver_id), (self.bot.id, GROUP_ID, USER_ID))
            locked = True
            try:
                yield
            finally:
                locked = False

        async def forget(bot_id, message, receiver_id):
            self.assertTrue(locked)
            self.assertEqual((bot_id, receiver_id), (self.bot.id, USER_ID))
            live_targets.pop(message.ephemeral_message_id, None)

        self.forget.side_effect = forget
        with patch.object(statistics, "recipient_guard", guard):
            await self.feed_callback(f"statistics:word:{USER_ID}:1", chat_type=ChatType.SUPERGROUP)

        self.assertEqual(live_targets, {})
        self.assertFalse(locked)
        self.forget.assert_awaited_once()
        self.remember.assert_not_awaited()
        self.refresh.assert_not_awaited()

    async def test_group_launcher_reveals_no_saved_words_and_is_available_to_each_player(self):
        for chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
            with self.subTest(chat_type=chat_type):
                self.transport.requests.clear()
                await self.feed_message(chat_type=chat_type)
                self.questions.assert_not_awaited()
                self.factory.assert_not_called()
                method = self.requests("sendMessage")[0][0]
                self.assertNotIn(self.question.word, method.text)
                self.assertEqual(method.reply_markup.inline_keyboard[0][0].callback_data, "statistics:open")

        self.transport.requests.clear()
        for user_id in (USER_ID, USER_ID + 1):
            await self.feed_callback(chat_type=ChatType.SUPERGROUP, user_id=user_id)
        personal_lists = self.requests("sendMessage")
        self.assertEqual(len(personal_lists), 2)
        for (method, payload, _), user_id in zip(personal_lists, (USER_ID, USER_ID + 1)):
            parameters = json.loads(payload["ephemeral_message_parameters"])
            self.assertEqual(parameters, {
                "receiver_user_id": user_id, "callback_query_id": f"callback_{user_id}",
            })
            self.assertEqual(method.reply_markup.inline_keyboard[0][0].callback_data,
                             f"statistics:word:{user_id}:1")
        self.assertEqual(self.forget.await_count, 2)
        self.assertEqual(self.requests("editMessageText"), [])

    async def test_group_pages_edit_only_personal_list_and_graph_is_personal(self):
        await self.feed_callback(
            f"statistics:page:{USER_ID}:1", chat_type=ChatType.SUPERGROUP, ephemeral_id=812,
        )

        edit = self.requests("editEphemeralMessageText")[0][0]
        self.assertEqual(edit.chat_id, GROUP_ID)
        self.assertEqual(edit.receiver_user_id, USER_ID)
        self.assertEqual(edit.ephemeral_message_id, 812)
        self.forget.assert_awaited_once()
        self.assertEqual(self.requests("editMessageText"), [])
        self.transport.requests.clear()
        self.question = Question(
            id=1, word="массив", options="программист,лесник,хайкер,плотник",
        )
        self.get_statistics.return_value = {
            "программист": 50.0, "лесник": 25.0, "хайкер": 25.0, "плотник": 0.0,
        }
        self.configure_saved_choice("программист")

        await self.feed_callback(
            f"statistics:word:{USER_ID}:1", chat_type=ChatType.SUPERGROUP, ephemeral_id=812,
        )

        photo, payload, _ = self.requests("sendPhoto")[0]
        self.assertEqual(photo.caption, (
            "<b>Массив</b>\n\n"
            "Это был быстротест программист вы, лесник, хайкер или плотник\n\n"
            "И вы программист"
        ))
        self.assertEqual(photo.parse_mode, "HTML")
        self.assert_share_button(photo.reply_markup, "программист")
        self.assertEqual(json.loads(payload["ephemeral_message_parameters"]), {
            "receiver_user_id": USER_ID, "callback_query_id": f"callback_{USER_ID}",
        })
        self.assertEqual(self.requests("editEphemeralMessageMedia"), [])
        self.assertEqual(self.requests("editEphemeralMessageText"), [])
        self.remember.assert_not_awaited()
        self.refresh.assert_not_awaited()
        self.assertEqual(self.forget.await_count, 2)
        forgotten = self.forget.await_args.args
        self.assertEqual((forgotten[0], forgotten[2]), (self.bot.id, USER_ID))
        self.assertEqual(forgotten[1].ephemeral_message_id, len(self.transport.requests) + 1000)
        self.renderer.assert_called_once_with(
            self.get_statistics.return_value, selected_choice="программист",
        )

    async def test_long_saved_choice_keeps_full_text_with_a_separate_chart(self):
        choice = "старый вариант SQL & <тест> " * 60
        expected = (
            "<b>Камера NASA &amp; &lt;тест&gt;</b>\n\n"
            "Это был быстротест фотограф SQL &amp; художник вы или преступник\n\n"
            f"И вы {escape(choice)}"
        )
        for chat_type in (ChatType.PRIVATE, ChatType.SUPERGROUP):
            with self.subTest(chat_type=chat_type):
                self.configure_saved_choice(choice)
                self.transport.requests.clear()
                self.forget.reset_mock()

                await self.feed_callback(f"statistics:word:{USER_ID}:1", chat_type=chat_type)

                text, text_payload, _ = self.requests("sendMessage")[0]
                photo, photo_payload, _ = self.requests("sendPhoto")[0]
                self.assertEqual(text.text, expected)
                self.assertEqual(text.parse_mode, "HTML")
                self.assertIsNone(photo.caption)
                self.assert_share_button(photo.reply_markup, choice)
                self.remember.assert_not_awaited()
                self.refresh.assert_not_awaited()
                if chat_type == ChatType.SUPERGROUP:
                    for payload in (text_payload, photo_payload):
                        self.assertEqual(json.loads(payload["ephemeral_message_parameters"]), {
                            "receiver_user_id": USER_ID, "callback_query_id": f"callback_{USER_ID}",
                        })
                    self.assertEqual(self.forget.await_count, 2)
                    self.assertEqual(
                        [call.args[1].ephemeral_message_id for call in self.forget.await_args_list],
                        [1002, 1003],
                    )
                else:
                    self.assertNotIn("ephemeral_message_parameters", text_payload)
                    self.assertNotIn("ephemeral_message_parameters", photo_payload)
                    self.forget.assert_not_awaited()

    async def test_invalid_or_other_users_callbacks_never_query_or_show_history(self):
        invalid = (
            "statistics:", "statistics:word", "statistics:page:222", "statistics:word:222:x",
            "statistics:word:222:0", "statistics:word:222:-1", "statistics:word:222:2147483648",
            "statistics:page:222:-1", "statistics:page:222:x", "statistics:word:222:1:extra",
            "statistics:page:223:0", "statistics:word:223:1", "statistics:other:222:1",
        )
        for data in invalid:
            with self.subTest(data=data):
                self.transport.requests.clear()
                await self.feed_callback(data)
                self.factory.assert_not_called()
                self.questions.assert_not_awaited()
                self.get_statistics.assert_not_awaited()
                self.assertEqual(len(self.transport.requests), 1)
                self.assertEqual(self.requests("answerCallbackQuery")[0][0].show_alert, True)

    async def test_deleted_or_reset_answer_does_not_open_a_chart(self):
        result = MagicMock()
        result.first.return_value = None
        self.session.execute.return_value = result

        await self.feed_callback(f"statistics:word:{USER_ID}:1")

        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(self.requests("answerCallbackQuery")[0][0].show_alert, True)
        self.get_statistics.assert_not_awaited()
        self.renderer.assert_not_called()
        self.remember.assert_not_awaited()

    async def test_channel_and_messages_without_sender_are_ignored(self):
        await statistics.show_statistics(self.message(chat_type=ChatType.CHANNEL))
        await statistics.show_statistics(self.message(with_user=False))

        self.factory.assert_not_called()
        self.assertEqual(self.transport.requests, [])


if __name__ == "__main__":
    unittest.main()
