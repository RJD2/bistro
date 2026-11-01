import asyncio
from sqlalchemy.dialects.postgresql import insert

from bistro.database.session import async_session
from bistro.database.models import Question


QUESTIONS = [
    {"word": "цезарь", "options": "повар,император", "associo": "салат,Рим"},
    {"word": "замок", "options": "слесарь,рыцарь", "associo": ""},
    {"word": "камера", "options": "фотограф,велосипедист,преступник", "associo": ""},
    {"word": "ультрамарин", "options": "космодесантник,художник", "associo": ""},
]


async def seed_questions():
    async with async_session() as session:
        stmt = insert(Question).values(QUESTIONS)
        stmt = stmt.on_conflict_do_nothing(index_elements=["word"])
        await session.execute(stmt)
        await session.commit()


if __name__ == "__main__":
    asyncio.run(seed_questions())
