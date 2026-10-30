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
from handlers import admin
from keyboards import get_main_menu
from models import Question
from states import AssocioCreationStates


OWNER_ID = 109252838


class AdminAssocioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = MemoryStorage()
        self.state = FSMContext(
            storage=self.storage,
            key=StorageKey(bot_id=1, chat_id=OWNER_ID, user_id=OWNER_ID),
        )
        self.session = AsyncMock(spec=AsyncSession)
        self.factory = MagicMock()
        self.factory.return_value.__aenter__.return_value = self.session
        self.question = Question(id=7, word="цезарь & <тест>", options="салат,Рим", associo="")
        self.result = MagicMock()
        self.result.scalar_one_or_none.return_value = self.question
        self.session.execute.return_value = self.result
        self.menu = AsyncMock(return_value=get_main_menu(is_admin=True))
        for patcher in (
            patch.object(config, "ADMIN_ID", OWNER_ID),
            patch.object(admin, "async_session", self.factory),
            patch.object(admin, "get_main_menu_for_chat", self.menu),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.storage.close()

    def message(self, text=None, *, user_id=OWNER_ID, chat_type=ChatType.PRIVATE, message_id=10):
        result = MagicMock(spec=types.Message)
        result.text = text
        result.from_user = SimpleNamespace(id=user_id)
        result.chat = SimpleNamespace(id=OWNER_ID, type=chat_type)
        result.message_id = message_id
        result.answer = AsyncMock()
        result.edit_reply_markup = AsyncMock()
        result.bot = MagicMock()
        result.bot.edit_message_reply_markup = AsyncMock()
        return result

    def callback(self, preview, *, action="confirm", user_id=OWNER_ID):
        result = MagicMock(spec=types.CallbackQuery)
        result.from_user = SimpleNamespace(id=user_id)
        result.message = preview
        result.data = f"admin_associo:{action}"
        result.answer = AsyncMock()
        return result

    async def prepare_draft(self, *, targeted=False):
        command = "/add_associo цезарь & <тест>" if targeted else "/add_associo"
        await admin.start_add_associo(self.message(command), self.state)
        source = self.message(" повар & шеф , император <Рим> ")
        preview = self.message(message_id=99)
        source.answer.return_value = preview
        await admin.receive_existing_question_associo(source, self.state)
        self.assertEqual(await self.state.get_state(), AssocioCreationStates.waiting_for_confirmation.state)
        return source, preview

    async def test_start_automatically_loads_missing_associations_and_explains_order(self):
        event = self.message(admin.ADD_ASSOCIO_BUTTON)
        await admin.start_add_associo(event, self.state)
        self.assertEqual(await self.state.get_state(), AssocioCreationStates.waiting_for_associo.state)
        text = event.answer.call_args.args[0]
        self.assertIn("1. салат\n2. Рим", text)
        self.assertIn("ровно 2 ассоциаций", text)
        self.assertIsNone(event.answer.call_args.kwargs["parse_mode"])
        sql = str(self.session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
        self.assertIn("trim(questions.associo)", sql)
        self.assertIn("ORDER BY questions.id", sql)
        self.session.commit.assert_not_awaited()

    async def test_confirm_saves_mapping_only_and_automatically_offers_next_question(self):
        source, preview = await self.prepare_draft()
        self.assertIn("1. повар &amp; шеф → салат", source.answer.call_args.args[0])
        self.assertIn("2. император &lt;Рим&gt; → Рим", source.answer.call_args.args[0])
        update_result = MagicMock()
        update_result.scalar_one_or_none.return_value = 7
        next_result = MagicMock()
        next_result.scalar_one_or_none.return_value = Question(
            id=8, word="замок", options="слесарь,рыцарь", associo="",
        )
        self.session.execute.side_effect = [update_result, next_result]
        await admin.confirm_add_associo(self.callback(preview), self.state)
        statement = self.session.execute.await_args_list[1].args[0].compile(dialect=postgresql.dialect())
        self.assertEqual(statement.params["associo"], "повар & шеф,император <Рим>")
        self.assertIn("SET associo=", str(statement))
        self.assertNotIn("SET options=", str(statement))
        self.assertIn("questions.options =", str(statement))
        self.assertIn("questions.associo =", str(statement))
        self.session.commit.assert_awaited_once()
        self.assertEqual(await self.state.get_state(), AssocioCreationStates.waiting_for_associo.state)
        self.assertEqual((await self.state.get_data())["question_id"], 8)
        self.assertEqual((await self.state.get_data())["options"], ["слесарь", "рыцарь"])
        preview.edit_reply_markup.assert_awaited_once()

        await admin.confirm_add_associo(self.callback(preview), self.state)
        self.assertEqual(self.session.execute.await_count, 3)
        self.session.commit.assert_awaited_once()

    async def test_targeted_command_edits_filled_question_and_finishes_after_save(self):
        self.question.associo = "старое,прежнее"
        _, preview = await self.prepare_draft(targeted=True)
        query = self.session.execute.await_args_list[0].args[0].compile(dialect=postgresql.dialect())
        self.assertEqual(query.params["word_1"], "цезарь & <тест>")
        self.assertNotIn("trim(questions.associo)", str(query))
        self.result.scalar_one_or_none.return_value = 7
        await admin.confirm_add_associo(self.callback(preview), self.state)
        self.assertEqual(self.session.execute.await_count, 2)
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})
        self.menu.assert_awaited_once_with(OWNER_ID, is_admin=True)

    async def test_no_missing_questions_or_unknown_word_returns_menu(self):
        self.result.scalar_one_or_none.return_value = None
        for command, expected in (("/add_associo", "У всех вопросов"), ("/add_associo неизвестный", "не найден")):
            with self.subTest(command=command):
                event = self.message(command)
                await admin.start_add_associo(event, self.state)
                self.assertIn(expected, event.answer.call_args.args[0])
                self.assertIsNone(await self.state.get_state())
                self.assertIn("reply_markup", event.answer.call_args.kwargs)
        self.session.commit.assert_not_awaited()

    async def test_last_confirmation_finishes_when_no_missing_questions_remain(self):
        _, preview = await self.prepare_draft()
        update_result, no_questions = MagicMock(), MagicMock()
        update_result.scalar_one_or_none.return_value = 7
        no_questions.scalar_one_or_none.return_value = None
        self.session.execute.side_effect = [update_result, no_questions]
        await admin.confirm_add_associo(self.callback(preview), self.state)
        self.assertIsNone(await self.state.get_state())
        self.assertIn("У всех вопросов", preview.answer.call_args.args[0])

    async def test_invalid_associations_do_not_modify_draft_or_database(self):
        await admin.start_add_associo(self.message("/add_associo"), self.state)
        before = await self.state.get_data()
        for associations in (None, "", "повар", "повар,", "повар,ПОВАР", "повар,император,третий", "/start,б"):
            with self.subTest(associations=associations):
                await admin.receive_existing_question_associo(self.message(associations), self.state)
                self.assertEqual(await self.state.get_data(), before)
                self.assertEqual(await self.state.get_state(), AssocioCreationStates.waiting_for_associo.state)
        self.session.execute.assert_awaited_once()
        self.session.commit.assert_not_awaited()

    async def test_every_step_is_owner_only_and_private(self):
        handlers = (
            (admin.start_add_associo, False), (admin.receive_existing_question_associo, False),
            (admin.confirm_add_associo, True), (admin.cancel_add_associo, False),
            (admin.cancel_associo_callback, True), (admin.remind_associo_confirmation, False),
        )
        for user_id, chat_type in ((123, ChatType.PRIVATE), (OWNER_ID, ChatType.GROUP)):
            for handler, is_callback in handlers:
                with self.subTest(handler=handler.__name__, user_id=user_id, chat_type=chat_type):
                    await self.state.set_state(AssocioCreationStates.waiting_for_confirmation)
                    await self.state.set_data({"confirmation_message_id": 99})
                    event = self.message("/add_associo", user_id=user_id, chat_type=chat_type, message_id=99)
                    if is_callback:
                        event = self.callback(event, user_id=user_id)
                    await handler(event, self.state)
                    self.assertEqual(await self.state.get_data(), {"confirmation_message_id": 99})
                    self.assertEqual(await self.state.get_state(), AssocioCreationStates.waiting_for_confirmation.state)
        self.factory.assert_not_called()

    async def test_stale_confirm_or_cancel_cannot_modify_current_draft(self):
        await self.prepare_draft()
        before = await self.state.get_data()
        stale = self.message(message_id=98)
        await admin.confirm_add_associo(self.callback(stale), self.state)
        await admin.cancel_associo_callback(self.callback(stale, action="cancel"), self.state)
        self.assertEqual(await self.state.get_data(), before)
        self.session.execute.assert_awaited_once()

    async def test_save_failure_keeps_draft_for_retry(self):
        _, preview = await self.prepare_draft()
        before = await self.state.get_data()
        self.session.execute.side_effect = SQLAlchemyError("database unavailable")
        with self.assertLogs(admin.logger, level="ERROR"):
            await admin.confirm_add_associo(self.callback(preview), self.state)
        self.assertEqual(await self.state.get_data(), before)
        self.assertEqual(await self.state.get_state(), AssocioCreationStates.waiting_for_confirmation.state)
        self.session.commit.assert_not_awaited()
        preview.edit_reply_markup.assert_not_awaited()

    async def test_concurrently_changed_or_deleted_question_is_not_overwritten(self):
        _, preview = await self.prepare_draft()
        self.result.scalar_one_or_none.return_value = None
        await admin.confirm_add_associo(self.callback(preview), self.state)
        self.assertEqual(self.session.execute.await_count, 2)
        self.assertIn("изменен или удален", preview.answer.call_args.args[0])
        self.assertIsNone(await self.state.get_state())

    async def test_cancel_by_command_or_callback_saves_nothing(self):
        for use_callback in (False, True):
            _, preview = await self.prepare_draft()
            if use_callback:
                await admin.cancel_associo_callback(self.callback(preview, action="cancel"), self.state)
            else:
                await admin.cancel_add_associo(self.message("/cancel"), self.state)
            self.assertIsNone(await self.state.get_state())
            self.assertEqual(await self.state.get_data(), {})
        self.session.commit.assert_not_awaited()

    async def test_registered_cancel_precedes_association_receiver(self):
        dispatcher = Dispatcher(storage=self.storage)
        admin.register_handlers_admin(dispatcher)
        await admin.start_add_associo(self.message("/add_associo"), self.state)
        await dispatcher.message.trigger(
            self.message("/cancel"), state=self.state,
            raw_state=await self.state.get_state(), bot=MagicMock(),
        )
        self.assertIsNone(await self.state.get_state())
        self.session.commit.assert_not_awaited()

    async def test_corrupted_draft_cannot_update_existing_question(self):
        _, preview = await self.prepare_draft()
        await self.state.update_data(associo=["только одна"])
        await admin.confirm_add_associo(self.callback(preview), self.state)
        self.session.execute.assert_awaited_once()
        self.session.commit.assert_not_awaited()
        self.assertIsNone(await self.state.get_state())


if __name__ == "__main__":
    unittest.main()
