import unittest
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from models import Question
from questions import available_questions, next_available_question, question_choices


def scalar_result(value):
    result = MagicMock()
    result.scalars.return_value.first.return_value = value
    return result


class QuestionChoicesTests(unittest.TestCase):
    def test_associations_keep_their_position_and_whitespace_does_not_change_labels(self):
        question = Question(
            word="цезарь", options="салат,Рим", associo=" повар , император ",
        )

        self.assertEqual(question_choices(question), ["повар", "император"])
        self.assertEqual(question.options, "салат,Рим")

    def test_unfilled_and_ambiguous_mappings_are_unavailable(self):
        for options, associo in (
            ("салат,Рим", None),
            ("салат,Рим", ""),
            ("салат,Рим", "повар"),
            ("салат,Рим", "повар,император,полководец"),
            ("салат,Рим", "повар,"),
            ("салат,Рим", "повар,ПОВАР"),
            ("салат,Рим", "повар,импе\nратор"),
            ("салат,", "повар,император"),
        ):
            with self.subTest(options=options, associo=associo):
                with self.assertRaises(ValueError):
                    question_choices(Question(word="цезарь", options=options, associo=associo))

    def test_database_filter_excludes_unfilled_or_differently_sized_mappings(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        Question.__table__.create(engine)
        with Session(engine) as session:
            session.add_all([
                Question(word="пусто", options="a,b", associo=""),
                Question(word="пробелы", options="a,b", associo="  "),
                Question(word="мало", options="a,b", associo="x"),
                Question(word="много", options="a,b", associo="x,y,z"),
                Question(word="цезарь", options="салат,Рим", associo="повар,император"),
            ])
            session.commit()

            words = session.scalars(select(Question.word).where(available_questions())).all()

        self.assertEqual(words, ["цезарь"])


class NextAvailableQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_legacy_question_does_not_block_later_complete_question(self):
        malformed = Question(id=3, word="старый", options="a,b", associo="x,X")
        ready = Question(id=8, word="цезарь", options="салат,Рим", associo="повар,император")
        session = AsyncMock(spec=AsyncSession)
        session.execute.side_effect = [scalar_result(malformed), scalar_result(ready)]

        question = await next_available_question(session, ~Question.id.in_({1, 2}))

        self.assertIs(question, ready)
        self.assertEqual(session.execute.await_count, 2)
        query = session.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": True})
        self.assertIn("questions.id > 3", str(query))
        self.assertIn("1, 2", str(query))

    async def test_only_malformed_remaining_questions_returns_no_question(self):
        session = AsyncMock(spec=AsyncSession)
        session.execute.side_effect = [
            scalar_result(Question(id=3, word="старый", options="a,b", associo="x,")),
            scalar_result(None),
        ]

        self.assertIsNone(await next_available_question(session))


if __name__ == "__main__":
    unittest.main()
