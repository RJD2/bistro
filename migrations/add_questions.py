import asyncio
from sqlalchemy.dialects.postgresql import insert

from db import async_session
from models import Question


QUESTIONS = [
    {"word": "цезарь", "options": "салат,император"},
    {"word": "замок", "options": "слесарь,рыцарь"},
    {"word": "камера", "options": "фотограф,велосипедист,преступник"},
    {"word": "ультрамарин", "options": "космодесантник,художник"},
]


async def seed_questions():
    async with async_session() as session:
        stmt = insert(Question).values(QUESTIONS)
        stmt = stmt.on_conflict_do_nothing(index_elements=["word"])
        await session.execute(stmt)
        await session.commit()


if __name__ == "__main__":
    asyncio.run(seed_questions())
