import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DB", "postgresql+asyncpg://user:password@localhost/bistro")

from aiogram import Bot, types
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bistro.charts import updates as chart_updates


PNG = b"\x89PNG\r\n\x1a\nnew statistics"
CHAT_ID = -100123456789
OTHER_CHAT_ID = -100987654321


class ChartTransport(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.failures = {}
        self.on_request = None

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if self.on_request is not None:
            await self.on_request(method)
        receiver = getattr(method, "receiver_user_id", method.chat_id)
        if receiver in self.failures:
            raise self.failures[receiver](method)
        return True

    async def stream_content(self, url, **kwargs):
        yield b""


class LiveChartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = ChartTransport()
        self.bot = Bot(token="123456:TEST_TOKEN", session=self.transport)
        self.targets = []
        self.statistics = {"фотограф": 50.0, "преступник": 50.0}
        self.get_statistics = AsyncMock(side_effect=lambda qid, session: self.statistics.copy())
        self.session = AsyncMock(spec=AsyncSession)
        factory = MagicMock()
        factory.return_value.__aenter__.return_value = self.session
        self.list_charts = AsyncMock(side_effect=lambda bot_id, qid, *, chat_id: list(self.targets))
        self.get_chart = AsyncMock(side_effect=lambda chart_id: next(
            (chart for chart in self.targets if chart.id == chart_id), None,
        ))
        self.remove_chart = AsyncMock(side_effect=lambda chart: self.targets.remove(chart))

        async def mark_updated(chart, key):
            chart.statistics_key = key

        self.mark_updated = AsyncMock(side_effect=mark_updated)
        self.renderer = MagicMock(return_value=PNG)
        self.patches = [
            patch.object(chart_updates, "async_session", factory),
            patch.object(chart_updates.chart_messages, "list_charts", self.list_charts),
            patch.object(chart_updates.chart_messages, "get_chart", self.get_chart),
            patch.object(chart_updates.chart_messages, "remove_chart", self.remove_chart),
            patch.object(chart_updates.chart_messages, "mark_chart_updated", self.mark_updated),
            patch.object(chart_updates, "render_statistics_chart", self.renderer),
        ]
        for active_patch in self.patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)

    async def asyncTearDown(self):
        await chart_updates.stop_chart_updates()
        await self.bot.session.close()

    def add_chart(self, receiver, caption=None, *, regular=False, key="old", reply_markup=None,
                  selected_choice=None, chat_id=CHAT_ID, live_updates=True):
        chart = SimpleNamespace(
            id=len(self.targets) + 1,
            bot_id=self.bot.id,
            question_id=1,
            chat_id=receiver if regular else chat_id,
            receiver_user_id=0 if regular else receiver,
            message_id=50 + len(self.targets) if regular else 0,
            ephemeral_message_id=0 if regular else 1000 + len(self.targets),
            caption=caption,
            parse_mode="HTML" if caption is not None else None,
            reply_markup=reply_markup.model_dump_json(exclude_none=True) if reply_markup else None,
            statistics_key=key,
            selected_choice=selected_choice,
            live_updates=live_updates,
        )
        self.targets.append(chart)
        return chart

    async def refresh(self, *, chat_id=CHAT_ID, **kwargs):
        await chart_updates.refresh_question_charts(
            self.bot, 1, self.get_statistics, chat_id=chat_id, **kwargs,
        )

    async def test_group_game_results_keep_original_personal_captions(self):
        first = self.add_chart(111, "<b>Камера</b>\n\nВы фотограф")
        second = self.add_chart(222, "<b>Камера</b>\n\nВы преступник")
        separate_photo = self.add_chart(444)

        await self.refresh()

        self.list_charts.assert_awaited_once_with(self.bot.id, 1, chat_id=CHAT_ID)
        self.renderer.assert_called_once_with(self.statistics, selected_choice=None)
        self.assertEqual(len(self.transport.requests), 3)
        for method, target in zip(self.transport.requests, (first, second, separate_photo)):
            self.assertEqual(method.chat_id, target.chat_id)
            self.assertEqual(method.media.caption, target.caption)
            self.assertEqual(method.media.parse_mode, target.parse_mode)
            self.assertEqual(method.media.media.data, PNG)
            self.assertIsNone(method.reply_markup)
            self.assertEqual(method.__api_method__, "editEphemeralMessageMedia")
            self.assertEqual(method.receiver_user_id, target.receiver_user_id)
            self.assertEqual(method.ephemeral_message_id, target.ephemeral_message_id)
        self.assertEqual(self.mark_updated.await_count, 3)
        self.assertTrue(all(target.statistics_key == chart_updates.statistics_key(self.statistics)
                            for target in self.targets))

    async def test_updates_are_limited_to_live_results_in_the_originating_group(self):
        eligible = self.add_chart(111, "Игровой результат")
        statistics_snapshot = self.add_chart(222, "Статистика", live_updates=False)
        other_group = self.add_chart(333, "Другая группа", chat_id=OTHER_CHAT_ID)
        private = self.add_chart(444, "Личный чат", regular=True)
        old_group_card = self.add_chart(555, "Старая карточка", live_updates=False)
        other_bot = self.add_chart(666)
        other_bot.bot_id += 1
        other_question = self.add_chart(777)
        other_question.question_id = 2

        await self.refresh()

        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(self.transport.requests[0].chat_id, CHAT_ID)
        self.assertEqual(self.transport.requests[0].receiver_user_id, eligible.receiver_user_id)
        self.mark_updated.assert_awaited_once_with(
            eligible, chart_updates.statistics_key(self.statistics),
        )
        self.assertTrue(all(target.statistics_key == "old" for target in (
            statistics_snapshot, other_group, private, old_group_card, other_bot, other_question,
        )))

    async def test_private_or_invalid_origin_does_not_load_or_update_charts(self):
        self.add_chart(111)
        for chat_id in (111, 0, True, None):
            with self.subTest(chat_id=chat_id):
                await self.refresh(chat_id=chat_id)

        self.list_charts.assert_not_awaited()
        self.get_statistics.assert_not_awaited()
        self.assertEqual(self.transport.requests, [])

    async def test_reloaded_group_results_keep_each_players_share_button(self):
        keyboards = [
            types.InlineKeyboardMarkup(inline_keyboard=[[
                types.InlineKeyboardButton(
                    text="Поделиться",
                    switch_inline_query_chosen_chat=types.SwitchInlineQueryChosenChat(
                        query="share:1", allow_user_chats=True,
                    ),
                ),
            ]]),
            types.InlineKeyboardMarkup(inline_keyboard=[[
                types.InlineKeyboardButton(
                    text="Поделиться",
                    url="https://t.me/share/url?url=https%3A%2F%2Ft.me%2Ftest_bistro_bot%3Fstart%3Dquestion_1",
                ),
            ]]),
        ]
        for (receiver, choice), keyboard in zip(
            ((111, "фотограф"), (222, "император")), keyboards,
        ):
            self.add_chart(receiver, f"Вы {choice}", reply_markup=keyboard)
        # Discard the original keyboard objects; refresh must reconstruct from stored JSON.
        self.targets = [SimpleNamespace(**chart.__dict__) for chart in self.targets]

        await self.refresh()

        self.assertEqual([method.reply_markup for method in self.transport.requests], keyboards)
        self.assertEqual([method.__api_method__ for method in self.transport.requests], [
            "editEphemeralMessageMedia", "editEphemeralMessageMedia",
        ])

    async def test_each_player_keeps_their_highlight_and_matching_choices_share_a_render(self):
        self.add_chart(111, "Вы фотограф", selected_choice="фотограф")
        self.add_chart(222, "Вы преступник", selected_choice="преступник")
        self.add_chart(333, "Вы фотограф", selected_choice="фотограф")
        self.add_chart(444, "Старая карточка")
        self.renderer.side_effect = lambda stats, *, selected_choice: (
            f"chart:{selected_choice}".encode()
        )

        await self.refresh()

        self.assertEqual(self.renderer.call_args_list, [
            unittest.mock.call(self.statistics, selected_choice="фотограф"),
            unittest.mock.call(self.statistics, selected_choice="преступник"),
            unittest.mock.call(self.statistics, selected_choice=None),
        ])
        self.assertEqual([method.media.media.data for method in self.transport.requests], [
            "chart:фотограф".encode(), "chart:преступник".encode(),
            "chart:фотограф".encode(), b"chart:None",
        ])
        self.assertEqual([method.media.caption for method in self.transport.requests], [
            "Вы фотограф", "Вы преступник", "Вы фотограф", "Старая карточка",
        ])

    async def test_supplied_personal_image_is_never_sent_to_a_different_choice(self):
        self.add_chart(111, selected_choice="фотограф")
        self.add_chart(222, selected_choice="преступник")
        self.add_chart(333, selected_choice="фотограф")

        await self.refresh(statistics=self.statistics.copy(), chart=b"photographer image",
                           selected_choice="фотограф")

        self.renderer.assert_called_once_with(self.statistics, selected_choice="преступник")
        self.assertEqual([method.media.media.data for method in self.transport.requests], [
            b"photographer image", PNG, b"photographer image",
        ])

    async def test_replaced_registration_uses_its_current_saved_choice(self):
        snapshot = self.add_chart(111, "Вы фотограф", selected_choice="фотограф")
        replacement = SimpleNamespace(**{
            **snapshot.__dict__, "caption": "Вы преступник", "selected_choice": "преступник",
        })
        self.get_chart.side_effect = None
        self.get_chart.return_value = replacement

        await self.refresh()

        self.renderer.assert_called_once_with(self.statistics, selected_choice="преступник")
        self.assertEqual(self.transport.requests[0].media.caption, "Вы преступник")
        self.mark_updated.assert_awaited_once_with(
            replacement, chart_updates.statistics_key(self.statistics),
        )

    async def test_changed_statistics_keeps_card_highlight_with_its_personal_caption(self):
        self.add_chart(111, "Вы фотограф", selected_choice="фотограф")
        self.statistics = {"фотограф": 0.0, "преступник": 0.0}

        await self.refresh()

        self.renderer.assert_called_once_with(self.statistics, selected_choice="фотограф")
        self.assertEqual(self.transport.requests[0].media.caption, "Вы фотограф")

    async def test_unchanged_charts_are_not_uploaded_again(self):
        current_key = chart_updates.statistics_key(self.statistics)
        self.add_chart(111, key=current_key)
        stale = self.add_chart(222)

        await self.refresh(statistics=self.statistics.copy(), chart=PNG)

        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(self.transport.requests[0].receiver_user_id, stale.receiver_user_id)
        self.assertEqual(self.transport.requests[0].media.media.data, PNG)
        self.renderer.assert_not_called()
        await self.refresh()
        self.assertEqual(len(self.transport.requests), 1)
        self.renderer.assert_not_called()

    async def test_stale_answer_snapshot_cannot_overwrite_current_percentages(self):
        self.add_chart(111)
        old_statistics = {"фотограф": 100.0, "преступник": 0.0}

        await self.refresh(statistics=old_statistics, chart=b"stale chart")

        self.get_statistics.assert_awaited_once_with(1, self.session)
        self.renderer.assert_called_once_with(self.statistics, selected_choice=None)
        self.assertEqual(self.transport.requests[0].media.media.data, PNG)

    async def test_reused_ephemeral_id_does_not_overwrite_another_question(self):
        old_chart = self.add_chart(111, "Первый вопрос")
        replacement = SimpleNamespace(**{
            **old_chart.__dict__, "question_id": 2, "caption": "Следующий вопрос",
        })
        self.get_chart.side_effect = None
        self.get_chart.return_value = replacement

        await self.refresh()

        self.assertEqual(self.transport.requests, [])
        self.mark_updated.assert_not_awaited()
        self.remove_chart.assert_not_awaited()

    async def test_target_is_rechecked_if_it_became_static_or_changed_chat(self):
        snapshot = self.add_chart(111, "Игровой результат")
        for changes in (
            {"live_updates": False},
            {"chat_id": OTHER_CHAT_ID},
            {"receiver_user_id": 0, "ephemeral_message_id": 0, "message_id": 17},
        ):
            with self.subTest(changes=changes):
                self.get_chart.side_effect = None
                self.get_chart.return_value = SimpleNamespace(**{**snapshot.__dict__, **changes})

                await self.refresh()

        self.assertEqual(self.transport.requests, [])
        self.renderer.assert_not_called()
        self.mark_updated.assert_not_awaited()

    async def test_concurrent_refreshes_read_statistics_in_order_and_leave_latest_chart(self):
        target = self.add_chart(111)
        self.statistics = {"фотограф": 100.0, "преступник": 0.0}
        self.renderer.side_effect = lambda stats, **kwargs: f"chart:{stats['фотограф']}".encode()
        first_edit = asyncio.Event()
        release_edit = asyncio.Event()

        async def pause_first_edit(method):
            if len(self.transport.requests) == 1:
                first_edit.set()
                await release_edit.wait()

        self.transport.on_request = pause_first_edit
        first = asyncio.create_task(self.refresh())
        second = None
        try:
            await asyncio.wait_for(first_edit.wait(), timeout=2)
            self.statistics = {"фотограф": 50.0, "преступник": 50.0}
            second = asyncio.create_task(self.refresh())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            # The second reader must wait until the previous broadcast completes.
            self.assertEqual(self.get_statistics.await_count, 1)
        finally:
            release_edit.set()
            await asyncio.wait_for(asyncio.gather(first, *([second] if second else [])), timeout=2)

        self.assertEqual(len(self.transport.requests), 2)
        self.assertEqual(self.transport.requests[-1].media.media.data, b"chart:50.0")
        self.assertEqual(target.statistics_key, chart_updates.statistics_key(self.statistics))

    async def test_one_unavailable_message_does_not_stop_other_participants_updates(self):
        missing = self.add_chart(111)
        forbidden = self.add_chart(222)
        unchanged = self.add_chart(333)
        temporary_failure = self.add_chart(444)
        healthy = self.add_chart(555)
        self.transport.failures = {
            111: lambda method: TelegramBadRequest(method, "Bad Request: message to edit not found"),
            222: lambda method: TelegramForbiddenError(method, "Forbidden: bot was blocked by the user"),
            333: lambda method: TelegramBadRequest(method, "Bad Request: message is not modified"),
            444: lambda method: TelegramNetworkError(method, "Connection reset"),
        }

        with self.assertLogs(level="WARNING"):
            await self.refresh()

        self.assertEqual(len(self.transport.requests), 5)
        self.assertEqual(self.remove_chart.await_args_list,
                         [unittest.mock.call(missing), unittest.mock.call(forbidden)])
        marked_targets = [call.args[0] for call in self.mark_updated.await_args_list]
        self.assertEqual(marked_targets, [unchanged, healthy])
        self.assertIn(temporary_failure, self.targets)
        self.assertEqual(temporary_failure.statistics_key, "old")

    async def test_rate_limit_preserves_pending_targets_and_does_not_sleep(self):
        first = self.add_chart(111)
        second = self.add_chart(222)
        self.transport.failures[111] = lambda method: TelegramRetryAfter(
            method, "Too many requests", retry_after=30,
        )

        with self.assertLogs(level="WARNING"):
            await asyncio.wait_for(self.refresh(), timeout=1)

        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(self.targets, [first, second])
        self.mark_updated.assert_not_awaited()
        self.remove_chart.assert_not_awaited()

    async def test_rate_limited_last_vote_retries_once_with_latest_statistics(self):
        target = self.add_chart(111)
        self.transport.failures[111] = lambda method: TelegramRetryAfter(
            method, "Too many requests", retry_after=30,
        )
        waiting = asyncio.Event()
        release_retry = asyncio.Event()

        async def delayed_sleep(delay):
            waiting.set()
            await release_retry.wait()

        self.renderer.side_effect = lambda stats, **kwargs: f"chart:{stats['фотограф']}".encode()
        with patch.object(chart_updates, "sleep", side_effect=delayed_sleep) as retry_sleep:
            with self.assertLogs(level="WARNING"):
                await self.refresh()
                await asyncio.wait_for(waiting.wait(), timeout=2)
                scheduled_task = chart_updates._scheduled_retries[(self.bot.id, CHAT_ID, 1)]
                # Another failed attempt for the same question shares the pending retry.
                await self.refresh()

            self.assertEqual(len(chart_updates._scheduled_retries), 1)
            self.assertIs(chart_updates._scheduled_retries[(self.bot.id, CHAT_ID, 1)], scheduled_task)
            self.assertEqual(len(chart_updates._retry_tasks), 1)
            self.assertEqual(target.statistics_key, "old")
            self.transport.failures.clear()
            self.statistics = {"фотограф": 75.0, "преступник": 25.0}
            release_retry.set()
            await asyncio.wait_for(scheduled_task, timeout=2)

        retry_sleep.assert_awaited_once_with(30)
        self.assertEqual(self.get_statistics.await_count, 3)
        self.assertEqual(self.transport.requests[-1].media.media.data, b"chart:75.0")
        self.assertEqual(target.statistics_key, chart_updates.statistics_key(self.statistics))
        self.mark_updated.assert_awaited_once()
        self.assertEqual(chart_updates._scheduled_retries, {})
        self.assertEqual(chart_updates._retry_tasks, set())

    async def test_retries_for_the_same_question_remain_independent_between_groups(self):
        first = self.add_chart(111)
        second = self.add_chart(222, chat_id=OTHER_CHAT_ID)
        self.transport.failures = {
            receiver: lambda method: TelegramNetworkError(method, "Connection reset")
            for receiver in (111, 222)
        }
        release_retry = asyncio.Event()

        async def pending_sleep(delay):
            await release_retry.wait()

        with patch.object(chart_updates, "sleep", side_effect=pending_sleep):
            with self.assertLogs(level="WARNING"):
                await self.refresh()
                await self.refresh(chat_id=OTHER_CHAT_ID)
            self.assertEqual(set(chart_updates._scheduled_retries), {
                (self.bot.id, CHAT_ID, 1), (self.bot.id, OTHER_CHAT_ID, 1),
            })
            retries = list(chart_updates._scheduled_retries.values())
            self.transport.failures.clear()
            release_retry.set()
            await asyncio.wait_for(asyncio.gather(*retries), timeout=2)

        self.assertEqual([method.chat_id for method in self.transport.requests[:2]],
                         [CHAT_ID, OTHER_CHAT_ID])
        self.assertCountEqual([method.chat_id for method in self.transport.requests[2:]],
                              [CHAT_ID, OTHER_CHAT_ID])
        self.assertTrue(all(target.statistics_key == chart_updates.statistics_key(self.statistics)
                            for target in (first, second)))
        self.assertEqual(chart_updates._scheduled_retries, {})

    async def test_shutdown_cancels_pending_retries(self):
        self.add_chart(111)
        self.transport.failures[111] = lambda method: TelegramNetworkError(method, "Connection reset")
        waiting = asyncio.Event()
        never_released = asyncio.Event()

        async def pending_sleep(delay):
            waiting.set()
            await never_released.wait()

        with patch.object(chart_updates, "sleep", side_effect=pending_sleep):
            with self.assertLogs(level="WARNING"):
                await self.refresh()
            await asyncio.wait_for(waiting.wait(), timeout=2)
            retry_task = chart_updates._scheduled_retries[(self.bot.id, CHAT_ID, 1)]

            await chart_updates.stop_chart_updates()

        self.assertTrue(retry_task.cancelled())
        self.assertEqual(chart_updates._scheduled_retries, {})
        self.assertEqual(chart_updates._retry_tasks, set())
        self.assertEqual(len(self.transport.requests), 1)


if __name__ == "__main__":
    unittest.main()
