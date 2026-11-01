import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, unquote, urlsplit

os.environ.setdefault("DB", "postgresql+asyncpg://user:password@localhost/bistro")

from aiogram import Bot, Dispatcher, types
from aiogram.client.session.base import BaseSession
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from bistro.charts import updates as chart_updates
from bistro.handlers import game
from bistro.database.models import Answer, Question, User
from bistro.handlers.states import GameStates


CHAT_ID = -100123456789
PNG = b"\x89PNG\r\n\x1a\nchart"


class RecordingSession(BaseSession):
    """Exercise aiogram methods and serialization without contacting Telegram"""

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
        response = {
            "message_id": 0 if ephemeral else len(self.requests) + 100,
            "date": 1700000000,
            "chat": {"id": CHAT_ID, "type": "supergroup"},
        }
        if ephemeral:
            response["ephemeral_message_id"] = len(self.requests) + 1000
        return types.Message.model_validate(response, context={"bot": bot})

    async def stream_content(self, url, **kwargs):
        yield b""


def scalar_result(value):
    result = MagicMock()
    result.scalars.return_value.first.return_value = value
    return result


class GroupGameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = RecordingSession()
        self.bot = Bot(token="123456:TEST_TOKEN", session=self.transport)
        self.bot.me = AsyncMock(return_value=types.User(
            id=self.bot.id, is_bot=True, first_name="Быстротест", username="test_bistro_bot",
        ))
        self.dp = Dispatcher(storage=MemoryStorage())
        game.register_handlers_game(self.dp)
        self.session = AsyncMock(spec=AsyncSession)
        self.session_factory = MagicMock()
        self.session_factory.return_value.__aenter__.return_value = self.session
        self.user_lookup = AsyncMock(
            side_effect=lambda tg_id, session: SimpleNamespace(id=tg_id + 1000)
        )
        self.statistics = {"фотограф SQL & художник": 50.0, "преступник": 50.0}
        self.question = Question(
            id=1, word="камера NASA & <тест>",
            options="фотограф SQL & художник,преступник", associo="снимок,тюрьма",
        )
        self.remember_chart = AsyncMock()
        self.forget_message = AsyncMock()
        self.refresh_charts = AsyncMock()
        self.patches = [
            patch.object(game, "async_session", self.session_factory),
            patch.object(game, "get_or_create_user", self.user_lookup),
            patch.object(game, "get_question_statistics", AsyncMock(return_value=self.statistics)),
            patch.object(game, "render_statistics_chart", return_value=PNG),
            patch.object(game.chart_messages, "remember_chart", self.remember_chart),
            patch.object(game.chart_messages, "forget_message", self.forget_message),
            patch.object(game, "refresh_question_charts", self.refresh_charts),
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
            f"Оказывается я {choice}, узнай кто ты в Быстроботе\n"
            f"https://t.me/test_bistro_bot?start=question_{self.question.id}"
        )
        self.assertEqual(parse_qs(link.query), {"url": [expected]})
        # iOS decodes percent escapes without converting plus signs to spaces.
        self.assertNotIn("+", link.query)
        self.assertEqual(unquote(link.query.removeprefix("url=")), expected)

    async def asyncTearDown(self):
        await self.dp.storage.close()
        await self.bot.session.close()

    def configure_database(self, *, existing=None, next_question=None, question=True):
        answered = MagicMock()
        answered.all.return_value = [(1,)]
        self.session.execute.side_effect = [
            scalar_result(self.question if question else None),
            scalar_result(existing),
            answered,
            scalar_result(next_question),
        ]

    def callback(self, *, user_id=222, option=0, chat_type=ChatType.SUPERGROUP,
                 data=None, ephemeral_id=None):
        message = {
            "message_id": 17 if ephemeral_id is None else 0,
            "date": 1700000000,
            "chat": {"id": CHAT_ID, "type": chat_type},
            "from": {"id": self.bot.id, "is_bot": True, "first_name": "Быстротест"},
            "text": "Общий вопрос",
        }
        if ephemeral_id is not None:
            message["ephemeral_message_id"] = ephemeral_id
            message["receiver_user"] = {"id": user_id, "is_bot": False, "first_name": "Игрок"}
        return types.CallbackQuery.model_validate({
            "id": f"callback_{user_id}_{option}",
            "from": {"id": user_id, "is_bot": False, "first_name": f"Игрок {user_id}"},
            "chat_instance": "group_instance",
            "data": data if data is not None else f"answer:1:{option}",
            "message": message,
        }, context={"bot": self.bot})

    async def feed_callback(self, callback, update_id=1):
        await self.dp.feed_update(
            self.bot, types.Update(update_id=update_id, callback_query=callback)
        )

    def requests(self, api_method):
        return [request for request in self.transport.requests
                if request[0].__api_method__ == api_method]

    def ephemeral_parameters(self, request):
        return json.loads(request[1]["ephemeral_message_parameters"])

    async def test_play_command_posts_one_shared_question_without_group_fsm(self):
        for chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
            with self.subTest(chat_type=chat_type):
                self.transport.requests.clear()
                answered = MagicMock()
                answered.all.return_value = []
                self.session.execute.side_effect = [answered, scalar_result(self.question)]
                state = self.dp.fsm.get_context(
                    bot=self.bot, chat_id=CHAT_ID, user_id=222,
                )
                self.assertIsNone(await state.get_state())
                message = types.Message.model_validate({
                    "message_id": 16,
                    "date": 1700000000,
                    "chat": {"id": CHAT_ID, "type": chat_type},
                    "from": {"id": 222, "is_bot": False, "first_name": "Игрок"},
                    "text": "/play",
                    "entities": [{"type": "bot_command", "offset": 0, "length": 5}],
                }, context={"bot": self.bot})

                await self.dp.feed_update(self.bot, types.Update(update_id=1, message=message))

                self.assertEqual(len(self.transport.requests), 1)
                method, payload, _ = self.transport.requests[0]
                self.assertEqual(method.__api_method__, "sendMessage")
                self.assertEqual(method.chat_id, CHAT_ID)
                self.assertIn("Камера NASA &amp; &lt;тест&gt;", method.text)
                self.assertNotIn("фотограф", method.text)
                self.assertNotIn("преступник", method.text)
                self.assertEqual(
                    [row[0].text for row in method.reply_markup.inline_keyboard],
                    ["Снимок", "Тюрьма"],
                )
                self.assertEqual(method.reply_markup.inline_keyboard[0][0].callback_data,
                                 "answer:1:0")
                self.assertNotIn("ephemeral_message_parameters", payload)
                self.assertIsNone(await state.get_state())
                self.assertEqual(await state.get_data(), {})
                self.session.add.assert_not_called()
                query = str(self.session.execute.await_args.args[0].compile(
                    dialect=postgresql.dialect(),
                ))
                self.assertIn("questions.associo", query.partition("WHERE")[2])

    async def test_private_answers_still_require_game_state_and_use_regular_messages(self):
        for next_question in (None, Question(id=2, word="замок SQL", options="слесарь,рыцарь", associo="ключ,башня")):
            with self.subTest(next_question=next_question):
                self.transport.requests.clear()
                self.user_lookup.reset_mock()
                self.session_factory.reset_mock()
                state = self.dp.fsm.get_context(
                    bot=self.bot, chat_id=CHAT_ID, user_id=222,
                )
                await state.clear()
                callback = self.callback(chat_type=ChatType.PRIVATE)

                await self.feed_callback(callback)

                self.assertEqual(self.transport.requests, [])
                self.session_factory.assert_not_called()
                self.user_lookup.assert_not_awaited()

                await state.set_state(GameStates.waiting_for_answer)
                self.configure_database(next_question=next_question)
                await self.feed_callback(callback, update_id=2)

                edits = self.requests("editMessageMedia")
                self.assertEqual(len(edits), 1)
                self.assertIn("вы фотограф SQL &amp; художник", edits[0][0].media.caption)
                self.assert_share_button(edits[0][0].reply_markup, "фотограф SQL & художник")
                self.assertEqual(self.remember_chart.await_args.kwargs["reply_markup"],
                                 edits[0][0].reply_markup)
                messages = self.requests("sendMessage")
                self.assertEqual(len(messages), 1)
                if next_question is None:
                    self.assertEqual(messages[0][0].text, game.MESSAGES["game_over"])
                    self.assertIsNone(await state.get_state())
                    self.assertEqual(await state.get_data(), {})
                else:
                    self.assertIn("Замок SQL", messages[0][0].text)
                    self.assertEqual(messages[0][0].reply_markup.inline_keyboard[0][0].callback_data,
                                     "answer:2:0")
                    self.assertEqual(await state.get_state(), GameStates.waiting_for_answer.state)
                    self.assertEqual((await state.get_data())["qid"], 2)
                self.assertTrue(all(
                    "ephemeral_message_parameters" not in payload
                    and "Ephemeral" not in method.__api_method__
                    for method, payload, _ in self.transport.requests
                ))

    async def test_every_group_member_can_answer_without_starting_the_game(self):
        for chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
            with self.subTest(chat_type=chat_type):
                self.transport.requests.clear()
                self.session.add.reset_mock()
                # Only the author of the shared question started the game.
                author_state = self.dp.fsm.get_context(
                    bot=self.bot, chat_id=CHAT_ID, user_id=111,
                )
                await author_state.set_state(GameStates.waiting_for_answer)
                other_state = self.dp.fsm.get_context(
                    bot=self.bot, chat_id=CHAT_ID, user_id=222,
                )
                self.assertIsNone(await other_state.get_state())

                for update_id, (user_id, option) in enumerate(((111, 0), (222, 1)), 1):
                    self.configure_database()
                    await self.feed_callback(
                        self.callback(user_id=user_id, option=option, chat_type=chat_type),
                        update_id=update_id,
                    )

                photos = self.requests("sendPhoto")
                self.assertEqual(len(photos), 2)
                for request, user_id, selected in zip(
                    photos, (111, 222), ("фотограф SQL &amp; художник", "преступник")
                ):
                    parameters = self.ephemeral_parameters(request)
                    self.assertEqual(parameters["receiver_user_id"], user_id)
                    self.assertEqual(parameters["callback_query_id"], f"callback_{user_id}_{0 if user_id == 111 else 1}")
                    self.assertIs(parameters["replace_callback_query_message"], True)
                    self.assertIn(f"вы {selected}", request[0].caption)
                    self.assert_share_button(
                        request[0].reply_markup,
                        "фотограф SQL & художник" if user_id == 111 else "преступник",
                    )
                    self.assertIn("Камера NASA &amp; &lt;тест&gt;", request[0].caption)
                    self.assertEqual(len(request[2]), 1)
                    self.assertEqual(next(iter(request[2].values())).data, PNG)

                saved = [call.args[0] for call in self.session.add.call_args_list]
                self.assertEqual([answer.user_id for answer in saved], [1111, 1222])
                self.assertEqual([answer.choice for answer in saved], [
                    "фотограф SQL & художник", "преступник",
                ])
                self.session.get.assert_any_await(User, 1111, with_for_update=True)
                self.session.get.assert_any_await(User, 1222, with_for_update=True)
                self.assertFalse(any(
                    method.__api_method__.startswith("editMessage")
                    for method, _, _ in self.transport.requests
                ))
                self.assertEqual(self.requests("sendMessage"), [])

    async def test_group_waits_for_quicktest_button_before_posting_next_shared_question(self):
        for chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
            with self.subTest(chat_type=chat_type):
                self.transport.requests.clear()
                self.session.add.reset_mock()
                self.refresh_charts.reset_mock()
                next_question = Question(id=2, word="замок SQL", options="слесарь,рыцарь", associo="ключ,башня")

                for update_id, user_id in enumerate((111, 222), 1):
                    self.session.execute.reset_mock()
                    self.configure_database(next_question=next_question)
                    await self.feed_callback(
                        self.callback(user_id=user_id, chat_type=chat_type), update_id=update_id,
                    )
                    # Reading the current question and the previous vote is enough;
                    # the next question must not even be queried by this callback.
                    self.assertEqual(self.session.execute.await_count, 2)

                self.assertEqual(len(self.requests("sendPhoto")), 2)
                self.assertEqual(self.requests("sendMessage"), [])
                self.assertEqual(self.refresh_charts.await_count, 2)

                answered = MagicMock()
                answered.all.return_value = [(1,)]
                self.session.execute.side_effect = [answered, scalar_result(next_question)]
                press = types.Message.model_validate({
                    "message_id": 23,
                    "date": 1700000000,
                    "chat": {"id": CHAT_ID, "type": chat_type},
                    "from": {"id": 111, "is_bot": False, "first_name": "Игрок"},
                    "text": "🎮 Быстротест",
                }, context={"bot": self.bot})

                await self.dp.feed_update(self.bot, types.Update(update_id=3, message=press))

                questions = self.requests("sendMessage")
                self.assertEqual(len(questions), 1)
                method, payload, _ = questions[0]
                self.assertIn("Замок SQL", method.text)
                self.assertNotIn("ephemeral_message_parameters", payload)
                self.assertEqual(method.reply_markup.inline_keyboard[0][0].callback_data,
                                 "answer:2:0")
                self.assertEqual(self.user_lookup.await_args.args[0], 111)

                # The next shared question is available to the other member too.
                first_question = self.question
                try:
                    self.question = next_question
                    self.configure_database()
                    await self.feed_callback(
                        self.callback(user_id=222, data="answer:2:1", chat_type=chat_type),
                        update_id=4,
                    )
                finally:
                    self.question = first_question

                self.assertEqual(len(self.requests("sendMessage")), 1)
                latest_result = self.requests("sendPhoto")[-1][0]
                self.assertIn("Замок SQL", latest_result.caption)
                self.assertIn("вы рыцарь", latest_result.caption)
                self.assertEqual([call.args[0].question_id for call in self.session.add.call_args_list],
                                 [1, 1, 2])

    async def test_completed_game_is_reported_only_after_pressing_quicktest_again(self):
        self.configure_database()
        await self.feed_callback(self.callback())
        self.assertEqual(self.requests("sendMessage"), [])

        answered = MagicMock()
        answered.all.return_value = [(1,)]
        self.session.execute.side_effect = [answered, scalar_result(None)]
        press = types.Message.model_validate({
            "message_id": 24, "date": 1700000000,
            "chat": {"id": CHAT_ID, "type": "supergroup"},
            "from": {"id": 222, "is_bot": False, "first_name": "Игрок"},
            "text": "🎮 Быстротест",
        }, context={"bot": self.bot})
        await self.dp.feed_update(self.bot, types.Update(update_id=2, message=press))

        self.assertEqual(len(self.requests("sendMessage")), 1)
        self.assertEqual(self.requests("sendMessage")[0][0].text, game.MESSAGES["game_over"])

    async def test_second_vote_refreshes_first_players_chart_with_their_own_caption(self):
        targets = {}

        def message_key(bot_id, message, receiver):
            ephemeral_id = getattr(message, "ephemeral_message_id", 0)
            return (bot_id, message.chat.id, receiver if ephemeral_id else 0,
                    0 if ephemeral_id else message.message_id, ephemeral_id)

        async def remember(**kwargs):
            key = message_key(kwargs["bot_id"], kwargs["message"], kwargs["receiver_user_id"])
            targets[key] = SimpleNamespace(
                id=len(targets) + 1,
                bot_id=kwargs["bot_id"], chat_id=key[1], receiver_user_id=key[2],
                message_id=key[3], ephemeral_message_id=key[4],
                question_id=kwargs["question_id"], caption=kwargs["caption"],
                parse_mode=kwargs["parse_mode"], statistics_key=kwargs["statistics_key"],
                reply_markup=kwargs["reply_markup"].model_dump_json(exclude_none=True),
            )

        async def forget(bot_id, message, receiver):
            targets.pop(message_key(bot_id, message, receiver), None)

        async def mark_updated(target, key):
            target.statistics_key = key

        render = lambda stats: f"chart:{stats['фотограф SQL & художник']}".encode()
        self.remember_chart.side_effect = remember
        self.forget_message.side_effect = forget
        self.refresh_charts.side_effect = chart_updates.refresh_question_charts
        game.render_statistics_chart.side_effect = render
        with (
            patch.object(chart_updates, "async_session", self.session_factory),
            patch.object(chart_updates, "render_statistics_chart", side_effect=render),
            patch.object(chart_updates.chart_messages, "list_charts", AsyncMock(
                side_effect=lambda bot_id, qid: [
                    target for target in targets.values()
                    if target.bot_id == bot_id and target.question_id == qid
                ],
            )),
            patch.object(chart_updates.chart_messages, "get_chart", AsyncMock(
                side_effect=lambda chart_id: next(
                    (target for target in targets.values() if target.id == chart_id), None,
                ),
            )),
            patch.object(chart_updates.chart_messages, "mark_chart_updated", AsyncMock(side_effect=mark_updated)),
        ):
            self.statistics.update({"фотограф SQL & художник": 100.0, "преступник": 0.0})
            self.configure_database()
            await self.feed_callback(self.callback(user_id=111, option=0))
            self.assertEqual(self.requests("editEphemeralMessageMedia"), [])

            self.statistics.update({"фотограф SQL & художник": 50.0, "преступник": 50.0})
            self.configure_database()
            await self.feed_callback(self.callback(user_id=222, option=1), update_id=2)

        edits = self.requests("editEphemeralMessageMedia")
        self.assertEqual(len(edits), 1)
        old_result_edit = edits[0][0]
        self.assertEqual(old_result_edit.receiver_user_id, 111)
        self.assertIn("вы фотограф SQL &amp; художник", old_result_edit.media.caption)
        self.assertNotIn("вы преступник", old_result_edit.media.caption)
        self.assert_share_button(old_result_edit.reply_markup, "фотограф SQL & художник")
        new_result = self.requests("sendPhoto")[-1][0]
        self.assertIn("вы преступник", new_result.caption)
        self.assert_share_button(new_result.reply_markup, "преступник")
        self.assertEqual(old_result_edit.media.media.data, new_result.photo.data)
        self.assertEqual(new_result.photo.data, b"chart:50.0")
        self.assertEqual(self.requests("sendMessage"), [])
        self.assertEqual(self.transport.requests[-1][0].__api_method__, "editEphemeralMessageMedia")
        self.assertEqual(len(targets), 2)
        self.assertTrue(all(target.statistics_key == game.statistics_key(self.statistics)
                            for target in targets.values()))

    async def test_reset_refreshes_affected_questions_after_saving_and_confirming(self):
        affected = MagicMock()
        affected.all.return_value = [(1,), (3,)]
        self.session.execute.side_effect = [affected, MagicMock()]
        message = types.Message.model_validate({
            "message_id": 19,
            "date": 1700000000,
            "chat": {"id": CHAT_ID, "type": "supergroup"},
            "from": {"id": 222, "is_bot": False, "first_name": "Игрок"},
            "text": "/reset",
            "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
        }, context={"bot": self.bot})

        async def after_confirmation(bot, question_id, get_statistics):
            self.session.commit.assert_awaited_once()
            self.assertIn("Ваш прогресс сброшен", self.requests("sendMessage")[-1][0].text)

        self.refresh_charts.side_effect = after_confirmation
        await self.dp.feed_update(self.bot, types.Update(update_id=1, message=message))

        self.assertEqual([call.args[1] for call in self.refresh_charts.await_args_list], [1, 3])
        self.session.get.assert_awaited_once_with(User, 1222, with_for_update=True)

    async def test_committed_vote_refreshes_others_even_if_current_delivery_fails(self):
        cases = (
            ("answerCallbackQuery", TelegramBadRequest),
            ("sendPhoto", TelegramNetworkError),
        )
        original_request = self.transport.make_request
        for failed_method, error_type in cases:
            with self.subTest(failed_method=failed_method):
                self.transport.requests.clear()
                self.session.commit.reset_mock()
                self.remember_chart.reset_mock()
                self.refresh_charts.reset_mock()
                self.configure_database()

                async def failing_request(bot, method, timeout=None):
                    result = await original_request(bot, method, timeout=timeout)
                    if method.__api_method__ == failed_method:
                        raise error_type(method, "Delivery failed")
                    return result

                async def after_commit(*args, **kwargs):
                    self.session.commit.assert_awaited_once()

                self.refresh_charts.side_effect = after_commit
                with patch.object(self.transport, "make_request", side_effect=failing_request):
                    with self.assertRaises(error_type):
                        await self.feed_callback(self.callback())

                self.refresh_charts.assert_awaited_once()
                self.assertEqual(self.refresh_charts.await_args.args[1], 1)
                self.assertEqual(self.remember_chart.await_count, 0)
                for method, payload, _ in self.transport.requests:
                    if method.__api_method__ in {"sendPhoto", "sendMessage"}:
                        self.assertIn("ephemeral_message_parameters", payload)
                    self.assertFalse(method.__api_method__.startswith("editMessage"))

    async def test_repeat_click_keeps_saved_choice_and_does_not_add_another_vote(self):
        self.configure_database()
        await self.feed_callback(self.callback(option=0))
        self.session.add.assert_called_once()
        original_answer = self.session.add.call_args.args[0]

        self.configure_database(existing=Answer(
            id=9, user_id=1222, question_id=1, choice=original_answer.choice,
        ))
        await self.feed_callback(self.callback(option=1), update_id=2)

        self.session.add.assert_called_once()
        photos = self.requests("sendPhoto")
        self.assertEqual(len(photos), 2)
        self.assertIn("вы фотограф SQL &amp; художник", photos[-1][0].caption)
        self.assertNotIn("вы преступник", photos[-1][0].caption)
        self.assert_share_button(photos[-1][0].reply_markup, "фотограф SQL & художник")

    async def test_answer_to_ephemeral_question_edits_only_that_personal_message(self):
        self.configure_database()
        await self.feed_callback(self.callback(ephemeral_id=812))

        self.assertEqual(self.requests("sendPhoto"), [])
        edits = self.requests("editEphemeralMessageMedia")
        self.assertEqual(len(edits), 1)
        method, payload, files = edits[0]
        self.assertEqual(method.chat_id, CHAT_ID)
        self.assertEqual(method.receiver_user_id, 222)
        self.assertEqual(method.ephemeral_message_id, 812)
        self.assertIn("вы фотограф SQL &amp; художник", method.media.caption)
        self.assert_share_button(method.reply_markup, "фотограф SQL & художник")
        self.assertEqual(json.loads(payload["media"])["type"], "photo")
        self.assertEqual(next(iter(files.values())).data, PNG)
        self.assertEqual(self.requests("sendMessage"), [])
        self.forget_message.assert_not_awaited()
        self.assertFalse(any(
            method.__api_method__.startswith("editMessage")
            for method, _, _ in self.transport.requests
        ))
        registration = self.remember_chart.await_args.kwargs
        self.assertEqual(registration["message"].ephemeral_message_id, 812)
        self.assertEqual(registration["receiver_user_id"], 222)
        self.assertEqual(registration["caption"], method.media.caption)
        self.assertEqual(registration["question_id"], 1)
        self.assertEqual(registration["reply_markup"], method.reply_markup)

    async def test_long_result_keeps_full_text_and_chart_private_for_both_message_types(self):
        choice = "длинный вариант & тест " * 60
        self.question = Question(id=1, word="камера NASA", options=f"{choice},преступник", associo="снимок,тюрьма")
        expected_text = (
            "<b>Камера NASA</b>\n\nЭто был быстротест "
            + choice.replace("&", "&amp;")
            + " вы или преступник\n\nИ вы "
            + choice.replace("&", "&amp;")
        )
        self.assertGreater(len(expected_text), 1024)
        for ephemeral_id in (None, 812):
            with self.subTest(ephemeral_id=ephemeral_id):
                self.transport.requests.clear()
                self.configure_database()
                await self.feed_callback(self.callback(ephemeral_id=ephemeral_id))

                if ephemeral_id is None:
                    text_result = self.requests("sendMessage")[0]
                    self.assertEqual(text_result[0].text, expected_text)
                    self.assertEqual(text_result[0].parse_mode, "HTML")
                    self.assertEqual(self.ephemeral_parameters(text_result), {
                        "receiver_user_id": 222,
                        "callback_query_id": "callback_222_0",
                        "replace_callback_query_message": True,
                    })
                    self.assertEqual(self.requests("editEphemeralMessageText"), [])
                else:
                    edits = self.requests("editEphemeralMessageText")
                    self.assertEqual(len(edits), 1)
                    edit = edits[0][0]
                    self.assertEqual(edit.text, expected_text)
                    self.assertEqual(edit.parse_mode, "HTML")
                    self.assertEqual(edit.chat_id, CHAT_ID)
                    self.assertEqual(edit.receiver_user_id, 222)
                    self.assertEqual(edit.ephemeral_message_id, ephemeral_id)
                    self.assertIsNone(edit.reply_markup)

                photos = self.requests("sendPhoto")
                self.assertEqual(len(photos), 1)
                self.assertEqual(self.ephemeral_parameters(photos[0]), {
                    "receiver_user_id": 222,
                    "callback_query_id": "callback_222_0",
                })
                self.assertEqual(next(iter(photos[0][2].values())).data, PNG)
                self.assert_share_button(photos[0][0].reply_markup, choice)
                registration = self.remember_chart.await_args.kwargs
                self.assertNotEqual(registration["message"].ephemeral_message_id, ephemeral_id)
                self.assertEqual(registration["receiver_user_id"], 222)
                self.assertIsNone(registration["caption"])
                self.assertIsNone(registration["parse_mode"])
                self.assertEqual(registration["reply_markup"], photos[0][0].reply_markup)
                self.assertFalse(any(
                    method.__api_method__.startswith("editMessage")
                    for method, _, _ in self.transport.requests
                ))

    async def test_invalid_callback_payload_never_saves_a_vote(self):
        for data in ("answer", "answer:1", "answer:x:0", "answer:1:x", "answer:0:0",
                     "answer:-1:0", "answer:1:0:extra", "unexpected:1:0"):
            with self.subTest(data=data):
                self.transport.requests.clear()
                self.session.reset_mock()
                self.user_lookup.reset_mock()
                await game.process_answer(
                    self.callback(data=data), AsyncMock(spec=FSMContext),
                )
                self.session.add.assert_not_called()
                self.user_lookup.assert_not_awaited()
                self.assertEqual(len(self.requests("answerCallbackQuery")), 1)
                self.assertEqual(self.requests("sendPhoto"), [])

    async def test_invalid_option_or_deleted_question_does_not_save_a_vote(self):
        for option, question_exists in ((-1, True), (2, True), (0, False)):
            with self.subTest(option=option, question_exists=question_exists):
                self.transport.requests.clear()
                self.session.reset_mock()
                self.user_lookup.reset_mock()
                self.configure_database(question=question_exists)
                await game.process_answer(
                    self.callback(option=option), AsyncMock(spec=FSMContext),
                )
                self.session.add.assert_not_called()
                self.user_lookup.assert_not_awaited()
                self.assertEqual(len(self.requests("answerCallbackQuery")), 1)
                self.assertEqual(self.requests("sendPhoto"), [])


if __name__ == "__main__":
    unittest.main()
