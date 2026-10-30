import os
import unittest
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
from handlers import admin, start
from states import BulkQuestionCreationStates, QuestionCreationStates


OWNER_ID = 109252838


class BulkQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = MemoryStorage()
        self.state = FSMContext(
            storage=self.storage,
            key=StorageKey(bot_id=1, chat_id=OWNER_ID, user_id=OWNER_ID),
        )
        self.owner_config = patch.object(config, "ADMIN_ID", OWNER_ID)
        self.owner_config.start()
        self.addCleanup(self.owner_config.stop)
        self.sent = []
        self.next_message_id = 100
        self.bot = MagicMock()
        self.bot.edit_message_reply_markup = AsyncMock()
        self.session = AsyncMock(spec=AsyncSession)
        self.result = MagicMock()
        self.result.scalars.return_value.all.return_value = ["Цезарь", "Замок", "Камера"]
        self.session.execute.return_value = self.result
        self.session_factory = MagicMock()
        self.session_factory.return_value.__aenter__.return_value = self.session
        self.database = patch.object(admin, "async_session", self.session_factory)
        self.database.start()
        self.addCleanup(self.database.stop)

    async def asyncTearDown(self):
        await self.storage.close()

    def message(self, text=None, *, user_id=OWNER_ID, chat_type=ChatType.PRIVATE, message_id=10):
        message = MagicMock(spec=types.Message)
        message.text = text
        message.from_user = SimpleNamespace(id=user_id)
        message.chat = SimpleNamespace(id=OWNER_ID, type=chat_type)
        message.message_id = message_id
        message.bot = self.bot
        message.edit_reply_markup = AsyncMock()

        async def answer(text, **kwargs):
            self.next_message_id += 1
            reply = self.message(text, message_id=self.next_message_id)
            self.sent.append((text, kwargs, reply))
            return reply

        message.answer = AsyncMock(side_effect=answer)
        return message

    def callback(self, preview, *, action="confirm", user_id=OWNER_ID):
        callback = MagicMock(spec=types.CallbackQuery)
        callback.from_user = SimpleNamespace(id=user_id)
        callback.message = preview
        callback.data = f"admin_questions:{action}"
        callback.answer = AsyncMock()
        return callback

    async def prepare_draft(self, words=None, options=None):
        words = words or ["Цезарь", "Замок", "Камера"]
        options = options or [" салат , император ", "слесарь,рыцарь", "фотограф,велосипедист,преступник"]
        await admin.start_add_questions(self.message("/add_questions"), self.state)
        await admin.receive_question_words(self.message(", ".join(words)), self.state)
        for choices in options:
            await admin.receive_bulk_question_options(self.message(choices), self.state)
        data = await self.state.get_data()
        self.assertEqual(await self.state.get_state(), BulkQuestionCreationStates.waiting_for_confirmation.state)
        return next(reply for _, _, reply in self.sent if reply.message_id == data["confirmation_message_id"])

    async def test_question_order_and_answers_are_collected_before_one_atomic_save(self):
        await admin.start_add_questions(self.message("/add_questions"), self.state)
        self.assertEqual(await self.state.get_state(), BulkQuestionCreationStates.waiting_for_words.state)
        await admin.receive_question_words(self.message(" Цезарь , Замок , Камера "), self.state)
        self.assertEqual((await self.state.get_data())["bulk_index"], 0)
        self.assertIn("Цезарь", self.sent[-1][0])
        for index, choices in enumerate((" салат , император ", "слесарь,рыцарь", "фотограф,велосипедист,преступник")):
            await admin.receive_bulk_question_options(self.message(choices), self.state)
            data = await self.state.get_data()
            self.assertEqual(data["bulk_index"], index + 1)
            if index < 2:
                self.assertEqual(await self.state.get_state(), BulkQuestionCreationStates.waiting_for_options.state)
                self.assertIn(["Замок", "Камера"][index], self.sent[-1][0])
        self.session.execute.assert_not_awaited()
        self.assertEqual(data["bulk_questions"], [
            {"word": "Цезарь", "options": ["салат", "император"]},
            {"word": "Замок", "options": ["слесарь", "рыцарь"]},
            {"word": "Камера", "options": ["фотограф", "велосипедист", "преступник"]},
        ])
        preview = next(reply for _, _, reply in self.sent if reply.message_id == data["confirmation_message_id"])
        await admin.confirm_add_questions(self.callback(preview), self.state)

        self.session.execute.assert_awaited_once()
        self.session.commit.assert_awaited_once()
        statement = self.session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
        self.assertEqual(statement.params, {
            "word_m0": "Цезарь", "options_m0": "салат,император",
            "word_m1": "Замок", "options_m1": "слесарь,рыцарь",
            "word_m2": "Камера", "options_m2": "фотограф,велосипедист,преступник",
        })
        self.assertIn("ON CONFLICT (word) DO NOTHING", str(statement))
        self.assertNotIn("DO UPDATE", str(statement))
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})
        preview.edit_reply_markup.assert_awaited()

        await admin.confirm_add_questions(self.callback(preview), self.state)
        self.session.execute.assert_awaited_once()

    async def test_every_bulk_step_requires_exact_owner_and_private_chat(self):
        handlers = (
            (admin.start_add_questions, False),
            (admin.receive_question_words, False),
            (admin.receive_bulk_question_options, False),
            (admin.cancel_add_questions, False),
            (admin.confirm_add_questions, True),
            (admin.cancel_bulk_callback, True),
        )
        draft = {
            "bulk_words": ["Цезарь"], "bulk_questions": [{"word": "Цезарь", "options": ["салат", "император"]}],
            "bulk_index": 1, "confirmation_message_id": 99,
        }
        for user_id, chat_type, owner_id in ((123, ChatType.PRIVATE, OWNER_ID), (OWNER_ID, ChatType.GROUP, OWNER_ID), (OWNER_ID, ChatType.PRIVATE, 0)):
            for handler, is_callback in handlers:
                with self.subTest(handler=handler.__name__, user_id=user_id, chat_type=chat_type, owner_id=owner_id):
                    await self.state.set_state(BulkQuestionCreationStates.waiting_for_confirmation)
                    await self.state.set_data(draft.copy())
                    event = self.message("/add_questions", user_id=user_id, chat_type=chat_type, message_id=99)
                    if is_callback:
                        event = self.callback(event, user_id=user_id)
                    with patch.object(config, "ADMIN_ID", owner_id):
                        await handler(event, self.state)
                    self.assertEqual(await self.state.get_state(), BulkQuestionCreationStates.waiting_for_confirmation.state)
                    self.assertEqual(await self.state.get_data(), draft)
        self.session_factory.assert_not_called()

    async def test_invalid_lists_remain_at_list_input_without_database_changes(self):
        for words in (None, "", " , ", "Цезарь,", "Цезарь,цезарь", "/help,Камера", "а" * 101, "два\nслова,Камера", ",".join(f"Вопрос {i}" for i in range(51))):
            with self.subTest(words=words):
                await admin.start_add_questions(self.message("/add_questions"), self.state)
                await admin.receive_question_words(self.message(words), self.state)
                self.assertEqual(await self.state.get_state(), BulkQuestionCreationStates.waiting_for_words.state)
        self.session_factory.assert_not_called()

    async def test_invalid_options_do_not_advance_or_replace_collected_questions(self):
        await admin.start_add_questions(self.message("/add_questions"), self.state)
        await admin.receive_question_words(self.message("Цезарь,Замок"), self.state)
        await admin.receive_bulk_question_options(self.message("салат,император"), self.state)
        before = await self.state.get_data()
        for options in (None, "", "рыцарь", "рыцарь,", "Рыцарь,рыцарь", "а" * 65 + ",б", "/cancel,нет", "да,нет\nпотом", ",".join(str(i) for i in range(9))):
            with self.subTest(options=options):
                await admin.receive_bulk_question_options(self.message(options), self.state)
                self.assertEqual(await self.state.get_state(), BulkQuestionCreationStates.waiting_for_options.state)
                self.assertEqual(await self.state.get_data(), before)
        self.session_factory.assert_not_called()

    async def test_cancel_midway_and_final_confirmation_discards_entire_batch(self):
        await admin.start_add_questions(self.message("/add_questions"), self.state)
        await admin.receive_question_words(self.message("Цезарь,Замок"), self.state)
        await admin.receive_bulk_question_options(self.message("салат,император"), self.state)
        await admin.cancel_add_questions(self.message("/cancel"), self.state)
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})

        preview = await self.prepare_draft()
        await admin.cancel_bulk_callback(self.callback(preview, action="cancel"), self.state)
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})
        preview.edit_reply_markup.assert_awaited()
        self.session_factory.assert_not_called()

    async def test_registered_cancel_is_routed_before_bulk_option_receiver(self):
        dispatcher = Dispatcher(storage=self.storage)
        admin.register_handlers_admin(dispatcher)
        await admin.start_add_questions(self.message("/add_questions"), self.state)
        await admin.receive_question_words(self.message("Цезарь,Замок"), self.state)
        await dispatcher.message.trigger(self.message("/cancel"), state=self.state, raw_state=await self.state.get_state(), bot=self.bot)
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})
        self.session_factory.assert_not_called()

    async def test_existing_questions_are_skipped_and_other_questions_commit_together(self):
        preview = await self.prepare_draft()
        self.result.scalars.return_value.all.return_value = ["Цезарь", "Камера"]
        await admin.confirm_add_questions(self.callback(preview), self.state)
        self.session.execute.assert_awaited_once()
        self.session.commit.assert_awaited_once()
        self.assertIsNone(await self.state.get_state())
        text = self.sent[-1][0]
        self.assertIn("2", text)
        self.assertIn("1", text)

    async def test_failed_commit_retains_complete_draft_for_retry(self):
        preview = await self.prepare_draft()
        before = await self.state.get_data()
        self.session.commit.side_effect = SQLAlchemyError("database unavailable")
        with self.assertLogs(admin.logger, level="ERROR"):
            await admin.confirm_add_questions(self.callback(preview), self.state)
        self.assertEqual(await self.state.get_state(), BulkQuestionCreationStates.waiting_for_confirmation.state)
        self.assertEqual(await self.state.get_data(), before)
        preview.edit_reply_markup.assert_not_awaited()

    async def test_stale_confirmation_or_cancel_cannot_affect_current_batch(self):
        await self.prepare_draft()
        before = await self.state.get_data()
        stale = self.message(message_id=98)
        await admin.confirm_add_questions(self.callback(stale), self.state)
        await admin.cancel_bulk_callback(self.callback(stale, action="cancel"), self.state)
        self.assertEqual(await self.state.get_data(), before)
        self.assertEqual(await self.state.get_state(), BulkQuestionCreationStates.waiting_for_confirmation.state)
        self.session_factory.assert_not_called()

    async def test_switching_between_single_and_bulk_closes_previous_preview(self):
        await admin.start_add_question(self.message("/add_question"), self.state)
        await admin.receive_question_word(self.message("Цезарь"), self.state)
        await admin.receive_question_options(self.message("салат,император"), self.state)
        single_preview_id = (await self.state.get_data())["confirmation_message_id"]
        await admin.start_add_questions(self.message("/add_questions"), self.state)
        self.bot.edit_message_reply_markup.assert_awaited_with(chat_id=OWNER_ID, message_id=single_preview_id, reply_markup=None)
        self.assertEqual(await self.state.get_state(), BulkQuestionCreationStates.waiting_for_words.state)
        self.assertNotIn("word", await self.state.get_data())

        bulk_preview = await self.prepare_draft()
        await admin.start_add_question(self.message("/add_question"), self.state)
        self.bot.edit_message_reply_markup.assert_awaited_with(chat_id=OWNER_ID, message_id=bulk_preview.message_id, reply_markup=None)
        self.assertEqual(await self.state.get_state(), QuestionCreationStates.waiting_for_word.state)
        self.assertEqual(await self.state.get_data(), {})
        await admin.confirm_add_questions(self.callback(bulk_preview), self.state)
        self.assertEqual(await self.state.get_state(), QuestionCreationStates.waiting_for_word.state)
        self.assertEqual(await self.state.get_data(), {})
        self.session_factory.assert_not_called()

    async def test_bulk_menu_button_is_visible_only_to_owner_in_private_chat(self):
        for user_id, chat_type, expected in ((OWNER_ID, ChatType.PRIVATE, True), (123, ChatType.PRIVATE, False), (OWNER_ID, ChatType.GROUP, False)):
            with self.subTest(user_id=user_id, chat_type=chat_type):
                message = self.message("/start", user_id=user_id, chat_type=chat_type)
                await start.cmd_start(message)
                keyboard = message.answer.call_args.kwargs["reply_markup"]
                labels = [button.text for row in keyboard.keyboard for button in row]
                self.assertEqual("📚 Добавить вопросы" in labels, expected)

    async def test_fifty_questions_produce_complete_previews_within_telegram_message_limit(self):
        words = [f"Вопрос {index} " + "🧩" * 20 + "<&>" for index in range(50)]
        option_text = ",".join(chr(0x1F600 + index) * 64 for index in range(8))
        await self.prepare_draft(words, [option_text] * 50)
        draft = await self.state.get_data()
        self.assertEqual(len(draft["bulk_questions"]), 50)
        final_text = self.sent[-1][0]
        self.assertIn("50", final_text)
        preview_texts = [text for text, _, _ in self.sent if words[0] in text or words[-1] in text]
        self.assertTrue(preview_texts)
        for text, kwargs, _ in self.sent:
            self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
            if "😀" * 64 in text:
                self.assertIsNone(kwargs.get("parse_mode"))
        self.assertTrue(any("😀" * 64 in text for text, _, _ in self.sent))
        self.session_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
