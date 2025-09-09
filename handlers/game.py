from aiogram import types, Dispatcher
from aiogram.dispatcher import FSMContext
from sqlalchemy import delete, func
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


async def get_question_statistics(question_id: int, session: AsyncSession):
    # Подсчет общего количества ответов
    total_answers = await session.execute(
        select(func.count()).select_from(Answer).where(Answer.question_id == question_id)
    )
    total_count = total_answers.scalar() or 0

    if total_count == 0:
        return {}

    # Подсчет количества ответов для каждого варианта
    result = await session.execute(
        select(Answer.choice, func.count(Answer.choice))
        .where(Answer.question_id == question_id)
        .group_by(Answer.choice)
    )
    choices_count = {row[0]: row[1] for row in result.all()}

    # Вычисление процентов
    percentages = {
        choice: (count / total_count) * 100
        for choice, count in choices_count.items()
    }

    return percentages


async def reset_progress(message: types.Message, state: FSMContext):
    async with async_session() as session:
        user = await get_or_create_user(message.from_user.id, session)

        # Удаляем все ответы пользователя
        stmt = delete(Answer).where(Answer.user_id == user.id)
        await session.execute(stmt)
        await session.commit()

    await state.finish()
    await message.answer("Ваш прогресс был сброшен. Вы можете начать игру заново.", reply_markup=None)


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

        # Получаем статистику по текущему вопросу
        stats = await get_question_statistics(qid, session)

    # Формируем сообщение со статистикой
    stats_message = "📊 Статистика ответов:\n"
    for choice, percentage in stats.items():
        stats_message += f"{choice}: {percentage:.0f}%\n"

    # Убираем кнопки и отправляем статистику
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"Вы {question.options.split(',')[opt]}\n\n{stats_message}")

    # Ищем следующий вопрос
    async with async_session() as session:
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


def register_handlers_game(dp: Dispatcher):
    dp.register_message_handler(start_game, lambda m: m.text == "🎮 Быстротест", state="*")
    dp.register_message_handler(reset_progress, lambda m: m.text == "🔄 Сбросить прогресс", state="*")
    dp.register_callback_query_handler(
        process_answer,
        lambda c: c.data.startswith("answer"),
        state=GameStates.waiting_for_answer
    )
