import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DB", "postgresql+asyncpg://user:password@localhost/bistro")

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter,
)
from aiogram.methods import SendMessage
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

import autotest
from models import GroupAutotestDelivery, GroupAutotestSubscription, Question


NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
CHAT_ID = -100123456789


def result_with(value):
    result = MagicMock()
    result.first.return_value = value
    result.scalar_one.return_value = value
    result.scalars.return_value.first.return_value = value
    result.scalars.return_value.one.return_value = value
    return result


def sql(statement):
    return statement.compile(dialect=postgresql.dialect())


class GroupAutotestSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = AsyncMock(spec=AsyncSession)
        factory = MagicMock()
        factory.return_value.__aenter__.return_value = self.session
        self.patches = [
            patch.object(autotest, "async_session", factory),
            patch.object(autotest, "utc_now", return_value=NOW),
            patch.object(autotest, "get_or_create_user", AsyncMock()),
        ]
        self.user_lookup = self.patches[-1].start()
        self.addCleanup(self.patches[-1].stop)
        for active_patch in self.patches[:-1]:
            active_patch.start()
            self.addCleanup(active_patch.stop)

    def prepare_subscription(self, next_send_at=None):
        subscription = GroupAutotestSubscription(chat_id=CHAT_ID, next_send_at=next_send_at)
        self.session.execute.side_effect = [MagicMock(), result_with(subscription)]
        return subscription

    async def test_enable_schedules_one_shared_question_for_the_group_after_24_hours(self):
        subscription = self.prepare_subscription()

        self.assertTrue(await autotest.toggle_group_subscription(CHAT_ID))

        self.assertEqual(subscription.next_send_at, NOW + timedelta(days=1))
        self.user_lookup.assert_not_awaited()
        self.session.commit.assert_awaited_once()
        insert_query, select_query = [sql(call.args[0]) for call in self.session.execute.await_args_list]
        self.assertIn("ON CONFLICT", str(insert_query))
        self.assertIn("DO NOTHING", str(insert_query))
        self.assertIn(CHAT_ID, insert_query.params.values())
        self.assertIn("FOR UPDATE", str(select_query))
        self.assertIn("group_autotest_subscriptions.chat_id =", str(select_query))
        self.assertIn(CHAT_ID, select_query.params.values())

    async def test_second_toggle_stops_the_group_and_reenable_keeps_its_history(self):
        subscription = self.prepare_subscription(NOW + timedelta(hours=2))
        self.assertFalse(await autotest.toggle_group_subscription(CHAT_ID))
        self.assertIsNone(subscription.next_send_at)

        self.session.execute.side_effect = [MagicMock(), result_with(subscription)]
        self.assertTrue(await autotest.toggle_group_subscription(CHAT_ID))

        self.assertEqual(subscription.next_send_at, NOW + timedelta(days=1))
        self.assertEqual(self.session.commit.await_count, 2)
        self.session.delete.assert_not_awaited()
        for call in self.session.execute.await_args_list:
            statement = str(sql(call.args[0]))
            self.assertNotIn("DELETE", statement)
            self.assertNotIn("group_autotest_deliveries", statement)


class GroupAutotestDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = AsyncMock(spec=AsyncSession)
        factory = MagicMock()
        factory.return_value.__aenter__.return_value = self.session
        self.bot = AsyncMock(spec=Bot)
        self.subscription = GroupAutotestSubscription(
            chat_id=CHAT_ID, next_send_at=NOW - timedelta(days=3),
        )
        self.question = Question(id=8, word="камера & SQL", options="фотограф,преступник", associo="снимок,тюрьма")
        for active_patch in (
            patch.object(autotest, "async_session", factory),
            patch.object(autotest, "utc_now", return_value=NOW),
        ):
            active_patch.start()
            self.addCleanup(active_patch.stop)

    def prepare_due(self, question=True):
        self.session.execute.side_effect = [
            result_with(self.subscription),
            result_with(self.question if question else None),
        ]

    async def test_no_due_group_sends_nothing_and_skips_concurrent_worker_locks(self):
        self.session.execute.return_value = result_with(None)

        self.assertFalse(await autotest.send_one_due_group_autotest(self.bot, now=NOW))

        self.bot.send_message.assert_not_awaited()
        self.session.add.assert_not_called()
        query = sql(self.session.execute.await_args.args[0])
        self.assertIn("FOR UPDATE", str(query))
        self.assertIn("SKIP LOCKED", str(query))
        self.assertIn("group_autotest_subscriptions.next_send_at <=", str(query))
        self.assertIn(NOW, query.params.values())
        self.assertIn("LIMIT", str(query))

    async def test_daily_question_is_shared_and_does_not_depend_on_personal_answers(self):
        self.prepare_due()

        async def before_send(**kwargs):
            self.session.add.assert_not_called()
            self.session.commit.assert_not_awaited()

        self.bot.send_message.side_effect = before_send

        self.assertTrue(await autotest.send_one_due_group_autotest(self.bot, now=NOW))

        kwargs = self.bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], CHAT_ID)
        self.assertNotIn("ephemeral_message_parameters", kwargs)
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertIn("Камера &amp; SQL", kwargs["text"])
        self.assertNotIn("фотограф", kwargs["text"])
        self.assertNotIn("преступник", kwargs["text"])
        self.assertEqual(
            [row[0].text for row in kwargs["reply_markup"].inline_keyboard],
            ["Снимок", "Тюрьма"],
        )
        self.assertEqual(
            [row[0].callback_data for row in kwargs["reply_markup"].inline_keyboard],
            ["autotest_answer:8:0", "autotest_answer:8:1"],
        )
        self.session.add.assert_called_once()
        delivered = self.session.add.call_args.args[0]
        self.assertIsInstance(delivered, GroupAutotestDelivery)
        self.assertEqual((delivered.chat_id, delivered.question_id), (CHAT_ID, 8))
        self.assertEqual(self.subscription.next_send_at, NOW + timedelta(days=1))
        self.session.commit.assert_awaited_once()
        query = sql(self.session.execute.await_args_list[1].args[0])
        self.assertIn("group_autotest_deliveries.question_id", str(query))
        self.assertIn("group_autotest_deliveries.chat_id =", str(query))
        self.assertIn("questions.associo", str(query).partition("WHERE")[2])
        self.assertIn(CHAT_ID, query.params.values())
        self.assertNotIn("FROM answers", str(query))
        self.assertNotIn("FROM autotest_deliveries", str(query))
        self.assertNotIn("users", str(query))

    async def test_schedule_uses_completion_time_without_a_missed_days_burst(self):
        self.prepare_due()
        completion = NOW + timedelta(minutes=2)
        with patch.object(autotest, "utc_now", return_value=completion):
            self.assertTrue(await autotest.send_one_due_group_autotest(self.bot, now=NOW))

        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.subscription.next_send_at, completion + timedelta(days=1))

    async def test_no_new_word_waits_without_reposting_questions_or_completion_notices(self):
        self.prepare_due(question=False)

        self.assertTrue(await autotest.send_one_due_group_autotest(self.bot, now=NOW))

        self.bot.send_message.assert_not_awaited()
        self.session.add.assert_not_called()
        self.assertEqual(self.subscription.next_send_at, NOW + timedelta(days=1))
        self.session.commit.assert_awaited_once()

    async def assert_send_failure(self, error, *, next_send_at):
        self.prepare_due()
        self.bot.send_message.side_effect = error
        self.assertTrue(await autotest.send_one_due_group_autotest(self.bot, now=NOW))
        self.session.add.assert_not_called()
        self.assertEqual(self.subscription.next_send_at, next_send_at)
        self.session.commit.assert_awaited_once()

    async def test_removed_bot_stops_only_this_group_schedule(self):
        await self.assert_send_failure(
            TelegramForbiddenError(SendMessage(chat_id=CHAT_ID, text="test"), "Forbidden: bot was kicked"),
            next_send_at=None,
        )

    async def test_missing_group_stops_its_schedule(self):
        await self.assert_send_failure(
            TelegramBadRequest(SendMessage(chat_id=CHAT_ID, text="test"), "Bad Request: chat not found"),
            next_send_at=None,
        )

    async def test_network_failure_keeps_the_word_for_retry(self):
        await self.assert_send_failure(
            TelegramNetworkError(SendMessage(chat_id=CHAT_ID, text="test"), "Connection failed"),
            next_send_at=NOW + timedelta(minutes=5),
        )

    async def test_rate_limit_keeps_the_word_and_respects_retry_after(self):
        await self.assert_send_failure(
            TelegramRetryAfter(SendMessage(chat_id=CHAT_ID, text="test"), "Too Many Requests", retry_after=180),
            next_send_at=NOW + timedelta(seconds=180),
        )

    async def test_group_sender_runs_even_when_no_private_subscription_is_due(self):
        with (
            patch.object(autotest, "send_one_due_autotest", AsyncMock(return_value=False)) as private_sender,
            patch.object(autotest, "send_one_due_group_autotest", AsyncMock(side_effect=[True, False])) as group_sender,
            patch.object(autotest.asyncio, "sleep", AsyncMock()),
        ):
            await autotest.send_due_autotests(self.bot)

        private_sender.assert_awaited()
        self.assertEqual(group_sender.await_count, 2)
        group_sender.assert_awaited_with(self.bot)


if __name__ == "__main__":
    unittest.main()
