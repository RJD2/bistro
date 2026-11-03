"""Aggregate bot activity from the saved answers."""

from dataclasses import dataclass

from sqlalchemy import func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from bistro.database.models import Answer, Question


@dataclass(frozen=True)
class PopularQuestion:
    id: int
    word: str
    answer_count: int


@dataclass(frozen=True)
class BotStatistics:
    total_participants: int
    total_questions: int
    total_answers: int
    most_popular_question: PopularQuestion | None

    @property
    def average_answers_per_participant(self) -> float:
        return self.total_answers / self.total_participants if self.total_participants else 0.0


async def get_bot_statistics(session: AsyncSession) -> BotStatistics:
    # Old installations may have multiple saved answers to the same question.
    # Each participant contributes at most one answer per question to all metrics.
    answers = select(Answer.user_id, Answer.question_id).distinct().cte("unique_answers")
    totals = select(
        func.count(func.distinct(answers.c.user_id)).label("total_participants"),
        func.count().label("total_answers"),
    ).select_from(answers).cte("answer_totals")
    answer_count = func.count().label("answer_count")
    popular = (
        select(Question.id, Question.word, answer_count)
        .join(answers, answers.c.question_id == Question.id)
        .group_by(Question.id, Question.word)
        .order_by(answer_count.desc(), Question.id)
        .limit(1)
        .cte("popular_question")
    )
    question_count = select(func.count()).select_from(Question).scalar_subquery()
    result = await session.execute(
        select(
            totals.c.total_participants,
            question_count.label("total_questions"),
            totals.c.total_answers,
            popular.c.id,
            popular.c.word,
            popular.c.answer_count,
        )
        .select_from(totals)
        .outerjoin(popular, true())
    )
    row = result.one()
    return BotStatistics(
        total_participants=row.total_participants,
        total_questions=row.total_questions,
        total_answers=row.total_answers,
        most_popular_question=(
            PopularQuestion(id=row.id, word=row.word, answer_count=row.answer_count)
            if row.id is not None else None
        ),
    )
