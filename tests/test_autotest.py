import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DB", "postgresql+asyncpg://user:password@localhost/bistro")

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from bistro.services import autotest
from bistro.database.models import AutotestDelivery, AutotestSubscription, Question, User


NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
TG_ID = 222333444
USER_ID = 17


def result_with(value):
    result = MagicMock()
    result.first.return_value = value
    result.scalar.return_value = value
    result.scalar_one.return_value = value
    result.scalar_one_or_none.return_value = value
    result.scalars.return_value.first.return_value = value
    result.scalars.return_value.one.return_value = value
    result.scalars.return_value.one_or_none.return_value = value
    return result


def sql(statement):
    return statement.compile(dialect=postgresql.dialect())


class AutotestSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = AsyncMock(spec=AsyncSession)
        factory = MagicMock()
        factory.return_value.__aenter__.return_value = self.session
        self.user = User(id=USER_ID, tg_id=TG_ID)
        self.lookup = AsyncMock(return_value=self.user)
        self.patches = [
            patch.object(autotest, "async_session", factory),
            patch.object(autotest, "utc_now", return_value=NOW),
            patch.object(autotest, "get_or_create_user", self.lookup),
        ]
        for active_patch in self.patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)

    def prepare_subscription(self, next_send_at=None):
        subscription = AutotestSubscription(user_id=USER_ID, next_send_at=next_send_at)
        self.session.execute.side_effect = [MagicMock(), result_with(subscription)]
        return subscription

    async def test_first_click_enables_daily_delivery_after_a_full_day(self):
        subscription = self.prepare_subscription()

        enabled = await autotest.toggle_subscription(TG_ID)

        self.assertTrue(enabled)
        self.assertEqual(subscription.next_send_at, NOW + timedelta(days=1))
        self.lookup.assert_awaited_once_with(TG_ID, self.session)
        self.session.commit.assert_awaited_once()
        statements = [sql(call.args[0]) for call in self.session.execute.await_args_list]
        self.assertIn("ON CONFLICT", str(statements[0]))
        self.assertIn("DO NOTHING", str(statements[0]))
        self.assertIn("FOR UPDATE", str(statements[1]))
        self.assertIn(USER_ID, statements[1].params.values())

    async def test_second_click_disables_and_reenabling_preserves_delivery_history(self):
        subscription = self.prepare_subscription(NOW + timedelta(hours=5))

        self.assertFalse(await autotest.toggle_subscription(TG_ID))
        self.assertIsNone(subscription.next_send_at)

        self.session.execute.side_effect = [MagicMock(), result_with(subscription)]
        self.assertTrue(await autotest.toggle_subscription(TG_ID))
        self.assertEqual(subscription.next_send_at, NOW + timedelta(days=1))
        self.assertEqual(self.session.commit.await_count, 2)
        self.session.delete.assert_not_awaited()
        for call in self.session.execute.await_args_list:
            statement = str(sql(call.args[0]))
            self.assertNotIn("DELETE", statement)
            self.assertNotIn("autotest_deliveries", statement)


class AutotestDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = AsyncMock(spec=AsyncSession)
        factory = MagicMock()
        factory.return_value.__aenter__.return_value = self.session
        self.bot = AsyncMock(spec=Bot)
        self.subscription = AutotestSubscription(
            user_id=USER_ID, next_send_at=NOW - timedelta(hours=1),
        )
        self.question = Question(id=8, word="камера & SQL", options="фотограф,преступник", associo="снимок,тюрьма")
        self.patches = [
            patch.object(autotest, "async_session", factory),
            patch.object(autotest, "utc_now", return_value=NOW),
        ]
        for active_patch in self.patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)

    def prepare_due(self, question=True):
        self.session.execute.side_effect = [
            result_with((self.subscription, TG_ID)),
            result_with(self.question if question else None),
        ]

    async def test_no_due_subscription_sends_nothing_and_skips_rows_locked_by_other_workers(self):
        self.session.execute.return_value = result_with(None)

        processed = await autotest.send_one_due_autotest(self.bot, now=NOW)

        self.assertFalse(processed)
        self.bot.send_message.assert_not_awaited()
        self.session.add.assert_not_called()
        statement = sql(self.session.execute.await_args.args[0])
        query = str(statement)
        self.assertIn("FOR UPDATE", query)
        self.assertIn("SKIP LOCKED", query)
        self.assertIn("autotest_subscriptions.next_send_at <=", query)
        self.assertIn(NOW, statement.params.values())
        self.assertIn("LIMIT", query)

    async def test_due_delivery_is_private_answerable_and_saved_only_after_send(self):
        self.prepare_due()

        async def check_before_send(**kwargs):
            self.session.add.assert_not_called()
            self.session.commit.assert_not_awaited()

        self.bot.send_message.side_effect = check_before_send
        processed = await autotest.send_one_due_autotest(self.bot, now=NOW)

        self.assertTrue(processed)
        kwargs = self.bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], TG_ID)
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
        saved = [call.args[0] for call in self.session.add.call_args_list]
        self.assertEqual(len(saved), 1)
        self.assertIsInstance(saved[0], AutotestDelivery)
        self.assertEqual((saved[0].user_id, saved[0].question_id), (USER_ID, 8))
        self.assertEqual(self.subscription.next_send_at, NOW + timedelta(days=1))
        self.session.commit.assert_awaited_once()

        query = sql(self.session.execute.await_args_list[1].args[0])
        query_text = str(query)
        self.assertIn("answers.question_id", query_text)
        self.assertIn("autotest_deliveries.question_id", query_text)
        self.assertIn("answers.user_id =", query_text)
        self.assertIn("autotest_deliveries.user_id =", query_text)
        self.assertIn("questions.associo", query_text.partition("WHERE")[2])
        self.assertIn(USER_ID, query.params.values())
        self.assertTrue("NOT IN" in query_text or "NOT (EXISTS" in query_text)

    async def test_missed_days_do_not_produce_a_catchup_burst(self):
        self.prepare_due()
        self.subscription.next_send_at = NOW - timedelta(days=7)

        self.assertTrue(await autotest.send_one_due_autotest(self.bot, now=NOW))

        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.subscription.next_send_at, NOW + timedelta(days=1))

    async def test_next_delivery_is_scheduled_from_actual_send_completion(self):
        self.prepare_due()
        completed_at = NOW + timedelta(minutes=2)
        with patch.object(autotest, "utc_now", return_value=completed_at):
            self.assertTrue(await autotest.send_one_due_autotest(self.bot, now=NOW))

        self.assertEqual(self.subscription.next_send_at, completed_at + timedelta(days=1))

    async def test_no_new_word_waits_another_day_without_daily_completion_messages(self):
        self.prepare_due(question=False)

        self.assertTrue(await autotest.send_one_due_autotest(self.bot, now=NOW))

        self.bot.send_message.assert_not_awaited()
        self.session.add.assert_not_called()
        self.assertEqual(self.subscription.next_send_at, NOW + timedelta(days=1))
        self.session.commit.assert_awaited_once()

    async def assert_send_failure(self, error, *, next_send_at):
        self.prepare_due()
        self.bot.send_message.side_effect = error

        self.assertTrue(await autotest.send_one_due_autotest(self.bot, now=NOW))

        self.session.add.assert_not_called()
        self.assertEqual(self.subscription.next_send_at, next_send_at)
        self.session.commit.assert_awaited_once()

    async def test_blocked_user_is_unsubscribed_without_marking_word_delivered(self):
        method = SendMessage(chat_id=TG_ID, text="test")
        await self.assert_send_failure(
            TelegramForbiddenError(method, "Forbidden: bot was blocked by the user"),
            next_send_at=None,
        )

    async def test_missing_private_chat_is_unsubscribed(self):
        method = SendMessage(chat_id=TG_ID, text="test")
        await self.assert_send_failure(
            TelegramBadRequest(method, "Bad Request: chat not found"),
            next_send_at=None,
        )

    async def test_network_error_retries_same_word_without_waiting_a_full_day(self):
        method = SendMessage(chat_id=TG_ID, text="test")
        await self.assert_send_failure(
            TelegramNetworkError(method, "Connection failed"),
            next_send_at=NOW + timedelta(minutes=5),
        )

    async def test_unrelated_bad_request_keeps_subscription_for_retry(self):
        method = SendMessage(chat_id=TG_ID, text="test")
        await self.assert_send_failure(
            TelegramBadRequest(method, "Bad Request: temporary delivery failure"),
            next_send_at=NOW + timedelta(minutes=5),
        )

    async def test_flood_limit_respects_retry_after_and_avoids_tight_retry_loop(self):
        method = SendMessage(chat_id=TG_ID, text="test")
        for seconds in (5, 180):
            with self.subTest(retry_after=seconds):
                self.session.reset_mock()
                await self.assert_send_failure(
                    TelegramRetryAfter(method, "Too Many Requests", retry_after=seconds),
                    next_send_at=NOW + timedelta(seconds=max(60, seconds)),
                )


if __name__ == "__main__":
    unittest.main()
