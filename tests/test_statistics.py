import os
import unittest
from html import escape
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DB", "postgresql+asyncpg://user:password@localhost/bistro")

from aiogram import types
from aiogram.fsm.context import FSMContext
from PIL import Image
from matplotlib.patches import Patch
from sqlalchemy.ext.asyncio import AsyncSession

from handlers import game
from models import Question
from statistics_chart import render_statistics_chart


class QuestionStatisticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_percentages_include_unselected_options_in_question_order(self):
        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.all.return_value = [("преступник", 1), ("фотограф", 3)]
        session.execute.return_value = result
        session.get.return_value = Question(
            id=1, word="Камера", options="фотограф,велосипедист,преступник"
        )

        statistics = await game.get_question_statistics(1, session)

        self.assertEqual(list(statistics), ["фотограф", "велосипедист", "преступник"])
        self.assertEqual(statistics, {"фотограф": 75.0, "велосипедист": 0.0, "преступник": 25.0})
        self.assertAlmostEqual(sum(statistics.values()), 100.0)

    async def test_no_answers_returns_empty_statistics(self):
        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.all.return_value = []
        session.execute.return_value = result

        self.assertEqual(await game.get_question_statistics(1, session), {})

    async def test_historical_choices_remain_in_percentages(self):
        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.all.return_value = [("салат", 1), ("старый вариант", 1)]
        session.execute.return_value = result
        session.get.return_value = Question(id=1, word="Цезарь", options="салат,император")

        statistics = await game.get_question_statistics(1, session)

        self.assertEqual(statistics, {"салат": 50.0, "император": 0.0, "старый вариант": 50.0})


class StatisticsChartTests(unittest.TestCase):
    def test_legend_preserves_exact_choice_case_and_statistics(self):
        statistics = {"плотник": 0.0, "ВОЕНАЧАЛЬНИК NASA": 100.0, "пРоводник SQL": 0.0}
        original = statistics.copy()
        with patch("statistics_chart.Patch", wraps=Patch) as legend_patch:
            render_statistics_chart(statistics)

        self.assertEqual(
            [call.kwargs["label"] for call in legend_patch.call_args_list],
            ["плотник: 0%", "ВОЕНАЧАЛЬНИК NASA: 100%", "пРоводник SQL: 0%"],
        )
        self.assertEqual(statistics, original)

    def test_chart_is_a_valid_png_for_normal_and_empty_statistics(self):
        cases = (
            {"фотограф": 50.0, "велосипедист": 30.0, "преступник": 20.0},
            {"салат": 100.0, "император": 0.0},
            {"салат": 0.0, "император": 0.0},
            {},
        )
        for statistics in cases:
            with self.subTest(statistics=statistics):
                image_data = render_statistics_chart(statistics)
                with Image.open(BytesIO(image_data)) as image:
                    self.assertEqual(image.format, "PNG")
                    self.assertGreater(image.width, 300)
                    self.assertGreater(image.height, 300)
                    image.verify()


class ProcessAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def process(self, question, statistics, next_question=None):
        session = AsyncMock(spec=AsyncSession)
        question_result = MagicMock()
        question_result.scalars.return_value.first.return_value = question
        answered_result = MagicMock()
        answered_result.all.return_value = [(1,)]
        next_result = MagicMock()
        next_result.scalars.return_value.first.return_value = next_question
        session.execute.side_effect = [question_result, answered_result, next_result]
        session_factory = MagicMock()
        session_factory.return_value.__aenter__.return_value = session

        message = MagicMock(spec=types.Message)
        message.edit_media = AsyncMock()
        message.edit_text = AsyncMock()
        message.answer_photo = AsyncMock()
        message.answer = AsyncMock()
        callback = MagicMock(spec=types.CallbackQuery)
        callback.from_user = SimpleNamespace(id=123)
        callback.data = "answer:1:0"
        callback.message = message
        callback.answer = AsyncMock()
        state = AsyncMock(spec=FSMContext)
        png = b"\x89PNG\r\n\x1a\nchart"

        async def get_saved_statistics(question_id, current_session):
            self.assertEqual(question_id, 1)
            self.assertIs(current_session, session)
            session.commit.assert_awaited()
            return statistics

        with (
            patch.object(game, "async_session", session_factory),
            patch.object(game, "get_or_create_user", AsyncMock(return_value=SimpleNamespace(id=7))),
            patch.object(game, "get_question_statistics", side_effect=get_saved_statistics),
            patch.object(game, "render_statistics_chart", return_value=png) as renderer,
        ):
            await game.process_answer(callback, state)
            renderer.assert_called_once_with(statistics)

        callback.answer.assert_awaited_once()
        return session, message, state, png

    async def test_result_contains_chart_and_exact_selected_choice_without_duplicate_statistics(self):
        for choice in ("фотограф SQL & художник", "ФОТОГРАФ SQL & художник", "фОтоГраф SQL & художник"):
            with self.subTest(choice=choice):
                question = Question(id=1, word="камера NASA & <тест>", options=f"{choice},велосипедист")
                statistics = {choice: 75.0, "велосипедист": 25.0}
                session, message, state, png = await self.process(question, statistics)

                saved_answer = session.add.call_args.args[0]
                self.assertEqual(saved_answer.choice, choice)
                self.assertEqual(saved_answer.question_id, 1)
                message.edit_media.assert_awaited_once()
                media = message.edit_media.call_args.kwargs["media"]
                self.assertIsInstance(media, types.InputMediaPhoto)
                self.assertIsInstance(media.media, types.BufferedInputFile)
                self.assertEqual(media.media.data, png)
                self.assertEqual(media.parse_mode, "HTML")
                self.assertEqual(
                    media.caption,
                    "<b>Камера NASA &amp; &lt;тест&gt;</b>\n\n"
                    f"Это был быстротест, вы {escape(choice)}",
                )
                self.assertNotIn("Статистика ответов", media.caption)
                self.assertNotIn("%", media.caption)
                self.assertIsNone(message.edit_media.call_args.kwargs["reply_markup"])
                message.answer.assert_awaited_once_with(game.MESSAGES["game_over"])
                state.clear.assert_awaited_once()

    async def test_long_selected_choice_keeps_exact_text_and_chart_then_shows_next_question(self):
        choice = "длинный вариант " * 70
        question = Question(id=1, word="камера NASA", options=f"{choice},велосипедист")
        statistics = {choice: 100.0, "велосипедист": 0.0}
        next_question = Question(id=2, word="замок SQL", options="слесарь NASA & мастер,рЫцарь SQL")

        _, message, state, png = await self.process(question, statistics, next_question)

        message.edit_media.assert_not_awaited()
        message.edit_text.assert_awaited_once()
        text = message.edit_text.call_args.kwargs["text"]
        self.assertGreater(len(text), 1024)
        self.assertEqual(text, f"<b>Камера NASA</b>\n\nЭто был быстротест, вы {choice}")
        self.assertNotIn("Статистика ответов", text)
        self.assertNotIn("%", text)
        self.assertIsNone(message.edit_text.call_args.kwargs["reply_markup"])
        message.answer_photo.assert_awaited_once()
        self.assertEqual(message.answer_photo.call_args.kwargs["photo"].data, png)
        self.assertEqual(message.answer_photo.call_args.kwargs["caption"], "📊 Камера NASA")
        state.update_data.assert_awaited_once_with(qid=2)
        state.clear.assert_not_awaited()
        message.answer.assert_awaited_once()
        self.assertIn("Замок SQL", message.answer.call_args.args[0])
        self.assertIn("слесарь NASA &amp; мастер", message.answer.call_args.args[0])
        self.assertIn("рЫцарь SQL", message.answer.call_args.args[0])
        keyboard = message.answer.call_args.kwargs["reply_markup"]
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "answer:2:0")
        self.assertEqual(
            [button.text for row in keyboard.inline_keyboard for button in row],
            ["слесарь NASA & мастер", "рЫцарь SQL"],
        )


if __name__ == "__main__":
    unittest.main()
