import unittest
from unittest.mock import AsyncMock

from sqlalchemy import create_engine, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from bistro.database.models import Answer, Question, User
from bistro.services.statistics import get_bot_statistics


class BotStatisticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        for model in (User, Question, Answer):
            model.__table__.create(self.engine)
        self.database = Session(self.engine)
        self.addCleanup(self.database.close)
        self.session = AsyncMock(spec=AsyncSession)
        # Execute the actual aggregation SQL without requiring a running server.
        self.session.execute.side_effect = self.database.execute

    async def test_empty_bot_returns_zeroes_and_no_popular_question(self):
        statistics = await get_bot_statistics(self.session)

        self.assertEqual(statistics.total_participants, 0)
        self.assertEqual(statistics.total_questions, 0)
        self.assertEqual(statistics.total_answers, 0)
        self.assertEqual(statistics.average_answers_per_participant, 0.0)
        self.assertIsNone(statistics.most_popular_question)
        self.session.execute.assert_awaited_once()

    async def test_unplayed_users_are_excluded_and_all_questions_are_counted(self):
        self.database.add_all([
            User(id=1, tg_id=111),
            User(id=2, tg_id=222),
            Question(id=1, word="готовый", options="a,b", associo="x,y"),
            Question(id=2, word="без ассоциаций", options="a,b", associo=""),
        ])
        self.database.commit()

        statistics = await get_bot_statistics(self.session)

        self.assertEqual(statistics.total_participants, 0)
        self.assertEqual(statistics.total_questions, 2)
        self.assertEqual(statistics.total_answers, 0)
        self.assertEqual(statistics.average_answers_per_participant, 0.0)
        self.assertIsNone(statistics.most_popular_question)

    async def test_counts_unique_participant_question_pairs_and_ranks_by_participants(self):
        self.database.connection().exec_driver_sql("DROP INDEX uq_answers_user_question")
        self.database.add_all([
            User(id=1, tg_id=111),
            User(id=2, tg_id=222),
            User(id=3, tg_id=333),
            User(id=4, tg_id=444),
            Question(id=1, word="камера", options="a,b", associo="x,y"),
            Question(id=2, word="цезарь", options="a,b", associo="x,y"),
            Question(id=3, word="неотвеченный", options="a,b", associo=""),
            Answer(user_id=1, question_id=1, choice="a"),
            Answer(user_id=1, question_id=1, choice="a"),
            Answer(user_id=1, question_id=1, choice="b"),
            Answer(user_id=1, question_id=2, choice="a"),
            Answer(user_id=2, question_id=2, choice="b"),
            Answer(user_id=3, question_id=2, choice="a"),
        ])
        self.database.commit()

        statistics = await get_bot_statistics(self.session)

        self.assertEqual(statistics.total_participants, 3)
        self.assertEqual(statistics.total_questions, 3)
        self.assertEqual(statistics.total_answers, 4)
        self.assertAlmostEqual(statistics.average_answers_per_participant, 4 / 3)
        popular = statistics.most_popular_question
        self.assertEqual((popular.id, popular.word, popular.answer_count), (2, "цезарь", 3))
        self.session.execute.assert_awaited_once()

    async def test_ties_select_the_lowest_question_id(self):
        self.database.add_all([
            User(id=1, tg_id=111),
            Question(id=5, word="второй", options="a,b"),
            Question(id=2, word="первый", options="a,b"),
            Answer(user_id=1, question_id=5, choice="a"),
            Answer(user_id=1, question_id=2, choice="b"),
        ])
        self.database.commit()

        statistics = await get_bot_statistics(self.session)

        self.assertEqual(statistics.most_popular_question.id, 2)
        self.assertEqual(statistics.most_popular_question.answer_count, 1)

    async def test_answers_to_removed_questions_still_contribute_to_saved_activity(self):
        self.database.add_all([
            User(id=1, tg_id=111),
            Answer(user_id=1, question_id=99, choice="a"),
        ])
        self.database.commit()

        statistics = await get_bot_statistics(self.session)

        self.assertEqual(statistics.total_participants, 1)
        self.assertEqual(statistics.total_questions, 0)
        self.assertEqual(statistics.total_answers, 1)
        self.assertEqual(statistics.average_answers_per_participant, 1.0)
        self.assertIsNone(statistics.most_popular_question)

    async def test_reset_progress_updates_totals_average_and_popular_question(self):
        self.database.add_all([
            User(id=1, tg_id=111),
            User(id=2, tg_id=222),
            Question(id=1, word="первый", options="a,b"),
            Question(id=2, word="второй", options="a,b"),
            Question(id=3, word="третий", options="a,b"),
            Answer(user_id=1, question_id=1, choice="a"),
            Answer(user_id=1, question_id=2, choice="b"),
            Answer(user_id=1, question_id=3, choice="a"),
            Answer(user_id=2, question_id=2, choice="a"),
            Answer(user_id=2, question_id=3, choice="b"),
        ])
        self.database.commit()

        initial = await get_bot_statistics(self.session)

        self.assertEqual(initial.total_participants, 2)
        self.assertEqual(initial.total_answers, 5)
        self.assertEqual(initial.average_answers_per_participant, 2.5)
        self.assertEqual(initial.most_popular_question.id, 2)
        self.assertEqual(initial.most_popular_question.answer_count, 2)

        # reset_progress deletes this participant's answers, keeping their User.
        self.database.execute(delete(Answer).where(Answer.user_id == 2))
        self.database.commit()

        after_reset = await get_bot_statistics(self.session)

        self.assertEqual(after_reset.total_participants, 1)
        self.assertEqual(after_reset.total_questions, 3)
        self.assertEqual(after_reset.total_answers, 3)
        self.assertEqual(after_reset.average_answers_per_participant, 3.0)
        self.assertEqual(after_reset.most_popular_question.id, 1)
        self.assertEqual(after_reset.most_popular_question.answer_count, 1)

        self.database.execute(delete(Answer).where(Answer.user_id == 1))
        self.database.commit()

        after_all_resets = await get_bot_statistics(self.session)

        self.assertEqual(after_all_resets.total_participants, 0)
        self.assertEqual(after_all_resets.total_questions, 3)
        self.assertEqual(after_all_resets.total_answers, 0)
        self.assertEqual(after_all_resets.average_answers_per_participant, 0.0)
        self.assertIsNone(after_all_resets.most_popular_question)


if __name__ == "__main__":
    unittest.main()
