"""Keep the visible associations aligned with the stored result options."""

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Question


def question_choices(question: Question) -> list[str]:
    """Return associations only when every position has a matching result."""
    if not isinstance(question.options, str) or not isinstance(question.associo, str):
        raise ValueError("У вопроса не заполнены ассоциации.")
    options = question.options.split(",")
    choices = [choice.strip() for choice in question.associo.split(",")]
    if (
        not 2 <= len(choices) <= 8
        or len(choices) != len(options)
        or any(not option.strip() for option in options)
        or any(not choice or not choice.isprintable() for choice in choices)
        or len({choice.casefold() for choice in choices}) != len(choices)
    ):
        raise ValueError("Ассоциации должны соответствовать результатам по количеству и порядку.")
    return choices


def available_questions():
    """Exclude unfilled and differently sized mappings before fetching rows."""
    choice_separators = func.length(Question.associo) - func.length(func.replace(Question.associo, ",", ""))
    option_separators = func.length(Question.options) - func.length(func.replace(Question.options, ",", ""))
    return and_(
        Question.associo.is_not(None),
        func.trim(Question.associo) != "",
        choice_separators.between(1, 7),
        choice_separators == option_separators,
    )


async def next_available_question(session: AsyncSession, *conditions) -> Question | None:
    """Skip malformed legacy rows without hiding later, complete questions."""
    statement = select(Question).where(available_questions(), *conditions).order_by(Question.id).limit(1)
    while True:
        result = await session.execute(statement)
        question = result.scalars().first()
        if question is None:
            return None
        try:
            question_choices(question)
        except ValueError:
            statement = statement.where(Question.id > question.id)
        else:
            return question
