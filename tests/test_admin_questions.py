import asyncio
import importlib.util
import os
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DB", "postgresql+asyncpg://user:password@localhost/bistro")

from aiogram import Dispatcher, types
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

import config
from handlers import admin, game, start
from models import Question
from states import QuestionCreationStates


OWNER_ID = 109252838


def message(text=None, *, user_id=OWNER_ID, chat_type=ChatType.PRIVATE, message_id=10):
    result = MagicMock(spec=types.Message)
    result.from_user = SimpleNamespace(id=user_id)
    result.chat = SimpleNamespace(id=OWNER_ID, type=chat_type)
    result.text = text
    result.message_id = message_id
    result.answer = AsyncMock()
    result.edit_reply_markup = AsyncMock()
    result.bot = MagicMock()
    result.bot.edit_message_reply_markup = AsyncMock()
    return result


def callback(preview, *, user_id=OWNER_ID, action="confirm"):
    result = MagicMock(spec=types.CallbackQuery)
    result.from_user = SimpleNamespace(id=user_id)
    result.message = preview
    result.data = f"admin_question:{action}"
    result.answer = AsyncMock()
    return result


class AdminQuestionsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = MemoryStorage()
        self.state = FSMContext(
            storage=self.storage,
            key=StorageKey(bot_id=1, chat_id=OWNER_ID, user_id=OWNER_ID),
        )
        self.owner_config = patch.object(config, "ADMIN_ID", OWNER_ID)
        self.owner_config.start()
        self.addCleanup(self.owner_config.stop)
        self.session = AsyncMock(spec=AsyncSession)
        self.result = MagicMock()
        self.result.scalar_one_or_none.return_value = 7
        self.session.execute.return_value = self.result
        self.session_factory = MagicMock()
        self.session_factory.return_value.__aenter__.return_value = self.session
        self.database = patch.object(admin, "async_session", self.session_factory)
        self.database.start()
        self.addCleanup(self.database.stop)

    async def asyncTearDown(self):
        await self.storage.close()

    async def prepare_draft(self, word="Камера & <тест>", options=" фотограф , велосипедист , преступник "):
        await admin.start_add_question(message("/add_question"), self.state)
        self.assertEqual(await self.state.get_state(), QuestionCreationStates.waiting_for_word.state)
        await admin.receive_question_word(message(word), self.state)
        self.assertEqual(await self.state.get_state(), QuestionCreationStates.waiting_for_options.state)
        options_message = message(options)
        preview = message(message_id=99)
        options_message.answer.return_value = preview
        await admin.receive_question_options(options_message, self.state)
        self.assertEqual(await self.state.get_state(), QuestionCreationStates.waiting_for_confirmation.state)
        return preview, options_message

    async def test_every_step_denies_other_users_and_owner_in_group(self):
        handlers = (
            (admin.start_add_question, False),
            (admin.receive_question_word, False),
            (admin.receive_question_options, False),
            (admin.cancel_add_question, False),
            (admin.confirm_add_question, True),
            (admin.cancel_question_callback, True),
        )
        draft = {"word": "Замок", "options": ["слесарь", "рыцарь"], "confirmation_message_id": 99}
        for user_id, chat_type in ((123, ChatType.PRIVATE), (OWNER_ID, ChatType.GROUP)):
            for handler, is_callback in handlers:
                with self.subTest(handler=handler.__name__, user_id=user_id, chat_type=chat_type):
                    await self.state.set_state(QuestionCreationStates.waiting_for_confirmation)
                    await self.state.set_data(draft.copy())
                    event = message("/add_question", user_id=user_id, chat_type=chat_type, message_id=99)
                    if is_callback:
                        event = callback(event, user_id=user_id)
                    await handler(event, self.state)
                    self.assertEqual(await self.state.get_data(), draft)
                    self.assertEqual(await self.state.get_state(), QuestionCreationStates.waiting_for_confirmation.state)
        self.session_factory.assert_not_called()

    async def test_unconfigured_owner_cannot_claim_access(self):
        with patch.object(config, "ADMIN_ID", 0):
            await admin.start_add_question(message("/add_question"), self.state)
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})
        self.session_factory.assert_not_called()

    async def test_owner_confirms_escaped_preview_and_saves_only_once(self):
        preview, source = await self.prepare_draft()
        self.session.execute.assert_not_awaited()
        preview_text = source.answer.call_args.args[0]
        self.assertIn("Камера &amp; &lt;тест&gt;", preview_text)
        self.assertEqual(source.answer.call_args.kwargs["parse_mode"], "HTML")

        event = callback(preview)
        await admin.confirm_add_question(event, self.state)

        self.session.execute.assert_awaited_once()
        statement = self.session.execute.call_args.args[0]
        values = statement.compile(dialect=postgresql.dialect()).params
        self.assertEqual(values["word"], "Камера & <тест>")
        self.assertEqual(values["options"], "фотограф,велосипедист,преступник")
        self.session.commit.assert_awaited_once()
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})
        preview.edit_reply_markup.assert_awaited()

        await admin.confirm_add_question(event, self.state)
        self.session.execute.assert_awaited_once()

    async def test_invalid_question_words_do_not_advance_wizard(self):
        cases = (None, "", "   ", "а" * 101, "/help", "два\nслова", "ноль\x00байт")
        for word in cases:
            with self.subTest(word=word):
                await admin.start_add_question(message("/add_question"), self.state)
                await admin.receive_question_word(message(word), self.state)
                self.assertEqual(await self.state.get_state(), QuestionCreationStates.waiting_for_word.state)
        self.session_factory.assert_not_called()

    async def test_invalid_options_do_not_advance_wizard(self):
        cases = (
            None, "", "один", "салат,", "Салат,салат", "а" * 65 + ",б",
            ",".join(str(index) for index in range(9)), "да,нет\nпотом", "/start,нет", "да,нет\x00",
        )
        for options in cases:
            with self.subTest(options=options):
                await admin.start_add_question(message("/add_question"), self.state)
                await admin.receive_question_word(message("Цезарь"), self.state)
                await admin.receive_question_options(message(options), self.state)
                self.assertEqual(await self.state.get_state(), QuestionCreationStates.waiting_for_options.state)
        self.session_factory.assert_not_called()

    async def test_cancel_by_command_or_button_discards_draft_without_saving(self):
        for use_callback in (False, True):
            with self.subTest(use_callback=use_callback):
                preview, _ = await self.prepare_draft()
                if use_callback:
                    await admin.cancel_question_callback(callback(preview, action="cancel"), self.state)
                else:
                    await admin.cancel_add_question(message("/cancel"), self.state)
                self.assertIsNone(await self.state.get_state())
                self.assertEqual(await self.state.get_data(), {})
        self.session_factory.assert_not_called()

    async def test_stale_confirmation_cannot_save_a_different_draft(self):
        await self.prepare_draft()
        before = await self.state.get_data()
        await admin.confirm_add_question(callback(message(message_id=98)), self.state)
        self.assertEqual(await self.state.get_data(), before)
        self.assertEqual(await self.state.get_state(), QuestionCreationStates.waiting_for_confirmation.state)
        self.session_factory.assert_not_called()

    async def test_duplicate_question_does_not_report_success_and_requests_new_word(self):
        self.result.scalar_one_or_none.return_value = None
        preview, _ = await self.prepare_draft()
        await admin.confirm_add_question(callback(preview), self.state)
        self.assertEqual(await self.state.get_state(), QuestionCreationStates.waiting_for_word.state)
        self.assertEqual(await self.state.get_data(), {})
        self.session.execute.assert_awaited_once()

    async def test_database_failure_keeps_draft_for_retry(self):
        preview, _ = await self.prepare_draft()
        before = await self.state.get_data()
        self.session.execute.side_effect = SQLAlchemyError("database unavailable")
        with self.assertLogs(admin.logger, level="ERROR"):
            await admin.confirm_add_question(callback(preview), self.state)
        self.assertEqual(await self.state.get_state(), QuestionCreationStates.waiting_for_confirmation.state)
        self.assertEqual(await self.state.get_data(), before)
        self.session.commit.assert_not_awaited()

    async def test_registered_cancel_command_does_not_become_question_text(self):
        dispatcher = Dispatcher(storage=self.storage)
        admin.register_handlers_admin(dispatcher)
        await admin.start_add_question(message("/add_question"), self.state)
        await dispatcher.message.trigger(
            message("/cancel"),
            state=self.state,
            raw_state=await self.state.get_state(),
            bot=MagicMock(),
        )
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})
        self.session_factory.assert_not_called()

    async def test_start_menu_shows_add_question_only_to_owner_in_private_chat(self):
        for user_id, chat_type, expected in (
            (OWNER_ID, ChatType.PRIVATE, True),
            (123, ChatType.PRIVATE, False),
            (OWNER_ID, ChatType.GROUP, False),
        ):
            with self.subTest(user_id=user_id, chat_type=chat_type):
                event = message("/start", user_id=user_id, chat_type=chat_type)
                await start.cmd_start(event)
                keyboard = event.answer.call_args.kwargs["reply_markup"]
                texts = [button.text for row in keyboard.keyboard for button in row]
                self.assertEqual(admin.ADD_QUESTION_BUTTON in texts, expected)

    async def test_concurrent_confirmation_updates_save_once_and_leave_no_draft(self):
        spec = importlib.util.spec_from_file_location(
            "bistro_test_application", Path(__file__).resolve().parents[1] / "bot.py"
        )
        application = importlib.util.module_from_spec(spec)
        with patch.object(config, "TOKEN", "123456:dummy-token-for-offline-tests"):
            spec.loader.exec_module(application)
        self.addAsyncCleanup(application.bot.session.close)
        self.addAsyncCleanup(application.dp.fsm.close)
        state = application.dp.fsm.get_context(
            bot=application.bot, chat_id=OWNER_ID, user_id=OWNER_ID
        )
        await state.set_state(QuestionCreationStates.waiting_for_confirmation)
        await state.set_data({
            "word": "Замок",
            "options": ["слесарь", "рыцарь"],
            "confirmation_message_id": 99,
        })
        owner = types.User(id=OWNER_ID, is_bot=False, first_name="Владелец")
        preview = types.Message(
            message_id=99,
            date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            chat=types.Chat(id=OWNER_ID, type=ChatType.PRIVATE),
            text="Черновик вопроса",
        )
        updates = [
            types.Update(update_id=index, callback_query=types.CallbackQuery(
                id=str(index), from_user=owner, chat_instance="offline-test",
                message=preview, data=admin.CONFIRM_CALLBACK,
            ))
            for index in (1, 2)
        ]
        insert_started = asyncio.Event()
        release_insert = asyncio.Event()
        second_update_arrived = asyncio.Event()
        original_lock = application.dp.fsm.events_isolation.lock
        arrivals = 0

        @asynccontextmanager
        async def observe_lock(key):
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                second_update_arrived.set()
            async with original_lock(key):
                yield

        async def delayed_insert(statement):
            insert_started.set()
            await release_insert.wait()
            return self.result

        self.session.execute.side_effect = delayed_insert
        with (
            patch.object(application.bot, "session", AsyncMock(return_value=True)),
            patch.object(application.dp.fsm.events_isolation, "lock", observe_lock),
        ):
            tasks = [asyncio.create_task(application.dp.feed_update(application.bot, updates[0]))]
            try:
                await asyncio.wait_for(insert_started.wait(), timeout=2)
                tasks.append(asyncio.create_task(application.dp.feed_update(application.bot, updates[1])))
                await asyncio.wait_for(second_update_arrived.wait(), timeout=2)
                self.session.execute.assert_awaited_once()
            finally:
                release_insert.set()
                await asyncio.gather(*tasks)

        self.session.execute.assert_awaited_once()
        self.session.commit.assert_awaited_once()
        self.assertIsNone(await state.get_state())
        self.assertEqual(await state.get_data(), {})


class AddedQuestionRenderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_question_with_markup_characters_is_displayed_as_text(self):
        question = Question(id=12, word="Замок <&> _тест_?", options="рыцарь & маг,слесарь <мастер>")
        session = AsyncMock(spec=AsyncSession)
        answered = MagicMock()
        answered.all.return_value = []
        found = MagicMock()
        found.scalars.return_value.first.return_value = question
        session.execute.side_effect = [answered, found]
        factory = MagicMock()
        factory.return_value.__aenter__.return_value = session
        event = message("🎮 Быстротест")
        state = AsyncMock(spec=FSMContext)
        with (
            patch.object(game, "async_session", factory),
            patch.object(game, "get_or_create_user", AsyncMock(return_value=SimpleNamespace(id=1))),
        ):
            await game.start_game(event, state)
        text = event.answer.call_args.args[0]
        self.assertEqual(event.answer.call_args.kwargs["parse_mode"], "HTML")
        self.assertIn("Замок &lt;&amp;&gt; _тест_?", text)
        self.assertIn("рыцарь &amp; маг", text)
        self.assertIn("слесарь &lt;мастер&gt;", text)


if __name__ == "__main__":
    unittest.main()
