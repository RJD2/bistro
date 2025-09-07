import asyncio

from db import async_session
from models import Question


async def seed_questions():
    async with async_session() as session:
        questions = [
            Question(word="Цезарь", options="салат,император"),
            Question(word="Замок", options="слесарь,рыцарь"),
            Question(word="Камера", options="фотограф,велосипедист,преступник"),
            Question(word="Ультрамарин", options="космодесантник,художник")
        ]
        session.add_all(questions)
        await session.commit()


if __name__ == "__main__":
    asyncio.run(seed_questions())
