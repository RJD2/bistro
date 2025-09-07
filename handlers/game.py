from aiogram import types, Dispatcher
from aiogram.dispatcher import FSMContext
from sqlalchemy import delete
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from db import async_session
from models import User, Answer, Question
from states import GameStates
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


async def start_game(message: types.Message, state: FSMContext):
    async with async_session() as session:
        user = await get_or_create_user(message.from_user.id, session)

        # Находим уже отвеченные вопросы
        result = await session.execute(select(Answer.question_id).where(Answer.user_id == user.id))
        answered_ids = {row[0] for row in result.all()}

        # Первый неотвеченный вопрос
        result = await session.execute(select(Question).where(Question.id.not_in(answered_ids)))
        question = result.scalars().first()

        if not question:
            await message.answer(MESSAGES["game_over"])
            return

        await state.update_data(qid=question.id)
        options = question.options.split(",")
        await message.answer(question.word, reply_markup=get_keyboard(options, question.id))
        await GameStates.waiting_for_answer.set()


async def process_answer(callback: types.CallbackQuery, state: FSMContext):
    _, qid, opt = callback.data.split(":")
    qid, opt = int(qid), int(opt)

    async with async_session() as session:
        user = await get_or_create_user(callback.from_user.id, session)

        # Сохраняем ответ
        result = await session.execute(select(Question).where(Question.id == qid))
        question = result.scalars().first()
        answer = Answer(user_id=user.id, question_id=qid, choice=question.options.split(",")[opt])
        session.add(answer)
        await session.commit()

    # Убираем кнопки
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"Вы {question.options.split(',')[opt]}")

    # Ищем следующий вопрос
    result = await session.execute(select(Answer.question_id).where(Answer.user_id == user.id))
    answered_ids = {row[0] for row in result.all()}

    result = await session.execute(select(Question).where(Question.id.not_in(answered_ids)))
    next_question = result.scalars().first()

    if next_question:
        options = next_question.options.split(",")
        await state.update_data(qid=next_question.id)
        await callback.message.answer(next_question.word, reply_markup=get_keyboard(options, next_question.id))
    else:
        await callback.message.answer(MESSAGES["game_over"])
        await state.finish()


async def reset_progress(message: types.Message, state: FSMContext):
    async with async_session() as session:
        user = await get_or_create_user(message.from_user.id, session)

        # Удаляем все ответы пользователя
        stmt = delete(Answer).where(Answer.user_id == user.id)
        await session.execute(stmt)
        await session.commit()

    await state.finish()
    await message.answer("Ваш прогресс был сброшен. Вы можете начать игру заново.", reply_markup=None)


def register_handlers_game(dp: Dispatcher):
    dp.register_message_handler(start_game, lambda m: m.text == "🎮 Быстротест", state="*")
    dp.register_message_handler(reset_progress, lambda m: m.text == "🔄 Сбросить прогресс", state="*")
    dp.register_callback_query_handler(
        process_answer,
        lambda c: c.data.startswith("answer"),
        state=GameStates.waiting_for_answer
    )
