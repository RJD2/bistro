import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DB", "postgresql+asyncpg://user:password@localhost/bistro")

from aiogram import types
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from bistro.charts import messages as chart_messages
from bistro.database.models import ChartMessage


BOT_ID = 123456
CHAT_ID = -100123456789


def message(message_id=17, *, ephemeral_id=None, chat_id=CHAT_ID):
    data = {
        "message_id": message_id,
        "date": 1700000000,
        "chat": {"id": chat_id, "type": "supergroup"},
    }
    if ephemeral_id is not None:
        data["ephemeral_message_id"] = ephemeral_id
    return types.Message.model_validate(data)


def compiled(statement):
    return statement.compile(dialect=postgresql.dialect())


class ChartMessageRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = AsyncMock(spec=AsyncSession)
        self.factory = MagicMock()
        self.factory.return_value.__aenter__.return_value = self.session
        active_patch = patch.object(chart_messages, "async_session", self.factory)
        active_patch.start()
        self.addCleanup(active_patch.stop)

    async def remember(self, source, *, question_id=1, receiver=111, caption="Вы салат", key="first",
                       reply_markup=None, selected_choice=None):
        await chart_messages.remember_chart(
            BOT_ID, question_id, source, receiver, caption, "HTML", key,
            reply_markup=reply_markup, selected_choice=selected_choice,
        )
        return compiled(self.session.execute.call_args.args[0])

    async def test_regular_and_personal_messages_have_distinct_persistent_targets(self):
        regular = await self.remember(message(message_id=17))
        first_player = await self.remember(message(message_id=0, ephemeral_id=17), receiver=111)
        second_player = await self.remember(message(message_id=0, ephemeral_id=17), receiver=222)

        identities = [
            tuple(statement.params[name] for name in (
                "bot_id", "chat_id", "receiver_user_id", "message_id", "ephemeral_message_id",
            ))
            for statement in (regular, first_player, second_player)
        ]
        self.assertEqual(identities, [
            (BOT_ID, CHAT_ID, 0, 17, 0),
            (BOT_ID, CHAT_ID, 111, 0, 17),
            (BOT_ID, CHAT_ID, 222, 0, 17),
        ])
        self.assertEqual(len(set(identities)), 3)
        self.assertEqual(self.session.commit.await_count, 3)

    async def test_reused_target_upserts_new_question_caption_and_statistics(self):
        source = message(message_id=0, ephemeral_id=72)
        old = await self.remember(source, question_id=1, caption="Вы салат", key="old",
                                  selected_choice="салат")
        replacement = await self.remember(source, question_id=2, caption="Вы рыцарь &amp; маг", key="new",
                                          selected_choice="рыцарь & маг")

        for name in ("bot_id", "chat_id", "receiver_user_id", "message_id", "ephemeral_message_id"):
            self.assertEqual(old.params[name], replacement.params[name])
        self.assertEqual(replacement.params["question_id"], 2)
        self.assertEqual(replacement.params["caption"], "Вы рыцарь &amp; маг")
        self.assertEqual(replacement.params["statistics_key"], "new")
        self.assertEqual(replacement.params["selected_choice"], "рыцарь & маг")
        sql = str(replacement)
        self.assertIn(
            "ON CONFLICT (bot_id, chat_id, receiver_user_id, message_id, ephemeral_message_id) DO UPDATE",
            sql,
        )
        for name in ("question_id", "caption", "parse_mode", "reply_markup", "statistics_key", "selected_choice"):
            self.assertIn(f"{name} = excluded.{name}", sql)

    async def test_personal_keyboards_round_trip_through_database_and_reused_target_clears_old_keyboard(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        ChartMessage.__table__.create(engine)
        first = types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(text="Поделиться", url="https://t.me/share/url?text=фотограф"),
        ]])
        second = types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(text="Поделиться", url="https://t.me/share/url?text=император"),
        ]])
        with Session(engine) as database:
            self.session.execute.side_effect = database.execute
            self.session.commit.side_effect = database.commit
            for receiver, keyboard, choice in ((111, first, "фотограф"), (222, second, "император")):
                await self.remember(message(message_id=0, ephemeral_id=72),
                                    receiver=receiver, reply_markup=keyboard, selected_choice=choice)

        # Use a new SQLAlchemy session, like a new bot process loading stored targets.
        with Session(engine) as database:
            self.session.execute.side_effect = database.execute
            self.session.commit.side_effect = database.commit
            stored = await chart_messages.list_charts(BOT_ID, 1)
            self.assertEqual([chart.receiver_user_id for chart in stored], [111, 222])
            self.assertEqual([chart.selected_choice for chart in stored], ["фотограф", "император"])
            self.assertEqual([
                types.InlineKeyboardMarkup.model_validate_json(chart.reply_markup) for chart in stored
            ], [first, second])
            await self.remember(message(message_id=0, ephemeral_id=72), receiver=111,
                                question_id=2, reply_markup=None)
            database.expire_all()
            replacement = (await chart_messages.list_charts(BOT_ID, 2))[0]
            self.assertIsNone(replacement.reply_markup)
            self.assertIsNone(replacement.selected_choice)
            self.assertEqual(len(await chart_messages.list_charts(BOT_ID, 1)), 1)

    async def test_next_ephemeral_invalidates_only_that_receivers_exact_target(self):
        await chart_messages.forget_message(BOT_ID, message(message_id=0, ephemeral_id=72), 111)

        statement = compiled(self.session.execute.call_args.args[0])
        self.assertEqual(statement.params, {
            "bot_id_1": BOT_ID,
            "chat_id_1": CHAT_ID,
            "receiver_user_id_1": 111,
            "message_id_1": 0,
            "ephemeral_message_id_1": 72,
        })
        self.assertIn("DELETE FROM chart_messages WHERE", str(statement))
        self.session.commit.assert_awaited_once()

    async def test_invalid_targets_never_open_a_database_session(self):
        cases = (
            (None, 111),
            (SimpleNamespace(message_id=17), 111),
            (message(message_id=0), 111),
            (message(message_id=-1), 111),
            (message(message_id=0, ephemeral_id=0), 111),
            (message(message_id=0, ephemeral_id=-1), 111),
            (message(message_id=0, ephemeral_id=72), 0),
            (message(message_id=0, ephemeral_id=72), True),
            (message(chat_id=0), 111),
        )
        for source, receiver in cases:
            with self.subTest(source=source, receiver=receiver):
                await chart_messages.remember_chart(BOT_ID, 1, source, receiver, None, None, "new")
                await chart_messages.forget_message(BOT_ID, source, receiver)
        self.factory.assert_not_called()

    async def test_updates_and_removals_cannot_match_a_replacement_question_or_snapshot(self):
        old = ChartMessage(
            id=9, bot_id=BOT_ID, question_id=1, chat_id=CHAT_ID,
            receiver_user_id=111, message_id=0, ephemeral_message_id=72,
            caption="Вы салат", parse_mode="HTML", statistics_key="old", reply_markup="{}",
            selected_choice="салат",
        )
        await chart_messages.mark_chart_updated(old, "new")
        update = compiled(self.session.execute.call_args.args[0])
        await chart_messages.remove_chart(old)
        deletion = compiled(self.session.execute.call_args.args[0])

        for statement in (update, deletion):
            with self.subTest(sql=str(statement)):
                self.assertEqual(statement.params["id_1"], 9)
                self.assertEqual(statement.params["question_id_1"], 1)
                self.assertEqual(statement.params["statistics_key_1"], "old")
                self.assertEqual(statement.params["caption_1"], "Вы салат")
                self.assertEqual(statement.params["parse_mode_1"], "HTML")
                self.assertEqual(statement.params["reply_markup_1"], "{}")
                self.assertEqual(statement.params["selected_choice_1"], "салат")
                self.assertIn("chart_messages.question_id =", str(statement))
                self.assertIn("chart_messages.statistics_key =", str(statement))
        self.assertEqual(update.params["statistics_key"], "new")
        self.assertEqual(self.session.commit.await_count, 2)

    async def test_loading_targets_is_scoped_to_bot_and_question_and_can_reread_a_target(self):
        reference = ChartMessage(id=9, bot_id=BOT_ID, question_id=1, statistics_key="latest")
        result = MagicMock()
        result.scalars.return_value.all.return_value = [reference]
        self.session.execute.return_value = result
        self.session.get.return_value = reference

        self.assertEqual(await chart_messages.list_charts(BOT_ID, 1), [reference])
        statement = compiled(self.session.execute.call_args.args[0])
        self.assertEqual(statement.params, {"bot_id_1": BOT_ID, "question_id_1": 1})
        self.assertIn("ORDER BY chart_messages.id", str(statement))
        self.assertIs(await chart_messages.get_chart(9), reference)
        self.session.get.assert_awaited_once_with(ChartMessage, 9)


if __name__ == "__main__":
    unittest.main()
