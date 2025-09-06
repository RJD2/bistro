from aiogram import types, Dispatcher
from aiogram.dispatcher import FSMContext
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from db import async_session
from models import User, Answer
from states import GameStates
from questions import questions
from keyboards import get_keyboard
from messages import MESSAGES

async def get_or_create_user(tg_id: int, session: AsyncSession):
    result = await session.execute(select(User).where(User.tg_id == tg_id))
    user = result.scalars().first()
    if not user:
        user = User(tg_id=tg_id)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user

# Запуск игры
async def start_game(message: types.Message, state: FSMContext):
    async with async_session() as session:
        user = await get_or_create_user(message.from_user.id, session)

        # находим какие вопросы уже были
        result = await session.execute(select(Answer.question_id).where(Answer.user_id == user.id))
        answered_ids = {row[0] for row in result.all()}

        # первый неотвеченный вопрос
        next_qid = next((i for i in range(len(questions)) if i not in answered_ids), None)

        if next_qid is None:
            await message.answer(MESSAGES["game_over"])
            return

        await state.update_data(qid=next_qid)
        q = questions[next_qid]
        await message.answer(q["word"], reply_markup=get_keyboard(q["options"], next_qid))
        await GameStates.waiting_for_answer.set()

# Обработка ответа
async def process_answer(callback: types.CallbackQuery, state: FSMContext):
    _, qid, opt = callback.data.split(":")
    qid, opt = int(qid), int(opt)

    async with async_session() as session:
        user = await get_or_create_user(callback.from_user.id, session)

        # сохраняем ответ
        answer = Answer(user_id=user.id, question_id=qid, choice=questions[qid]["options"][opt])
        session.add(answer)
        await session.commit()

    # убираем кнопки
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"Вы {questions[qid]['options'][opt]}")

    # ищем следующий вопрос
    async with async_session() as session:
        result = await session.execute(select(Answer.question_id).where(Answer.user_id == user.id))
        answered_ids = {row[0] for row in result.all()}

    next_qid = next((i for i in range(len(questions)) if i not in answered_ids), None)

    if next_qid is not None:
        q = questions[next_qid]
        await state.update_data(qid=next_qid)
        await callback.message.answer(q["word"], reply_markup=get_keyboard(q["options"], next_qid))
    else:
        await callback.message.answer(MESSAGES["game_over"])
        await state.finish()

def register_handlers_game(dp: Dispatcher):
    dp.register_message_handler(start_game, lambda m: m.text == "🎮 Быстротест", state="*")
    dp.register_callback_query_handler(
        process_answer,
        lambda c: c.data.startswith("answer"),
        state=GameStates.waiting_for_answer
    )
