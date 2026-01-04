from aiogram import types, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from sqlalchemy import delete, func, select
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
    if not message.from_user:
        return
    async with async_session() as session:
        user = await get_or_create_user(message.from_user.id, session)

        # Находим уже отвеченные вопросы
        result = await session.execute(select(Answer.question_id).where(Answer.user_id == user.id))
        answered_ids = {row[0] for row in result.all()}

        # Первый неотвеченный вопрос
        result = await session.execute(select(Question).where(~Question.id.in_(answered_ids)))
        question = result.scalars().first()

        if not question:
            await message.answer(MESSAGES["game_over"])
            return

        await session.refresh(question)
        qid = int(question.id) # type: ignore
        await state.update_data(qid=qid)
        options = question.options.split(",")
        await message.answer(str(question.word), reply_markup=get_keyboard(options, qid))
        await state.set_state(GameStates.waiting_for_answer)


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
    if not message.from_user:
        return
    async with async_session() as session:
        user = await get_or_create_user(message.from_user.id, session)

        # Удаляем все ответы пользователя
        stmt = delete(Answer).where(Answer.user_id == user.id)
        await session.execute(stmt)
        await session.commit()

    await state.clear()
    await message.answer("Ваш прогресс был сброшен. Вы можете начать игру заново.", reply_markup=None)


async def process_answer(callback: types.CallbackQuery, state: FSMContext):
    if not callback.from_user or not callback.data:
        return
    _, qid, opt = callback.data.split(":")
    qid, opt = int(qid), int(opt)

    async with async_session() as session:
        user = await get_or_create_user(callback.from_user.id, session)

        # Сохраняем ответ
        result = await session.execute(select(Question).where(Question.id == qid))
        question = result.scalars().first()
        if not question:
            return
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
    if callback.message is not None:
        await callback.message.edit_text( # type: ignore
            text=f"Вы ответили на '{question.options.split(',')[opt]}'\n\n{stats_message}",
            reply_markup=None
        )

    # Ищем следующий вопрос
    async with async_session() as session:
        result = await session.execute(select(Answer.question_id).where(Answer.user_id == user.id))
        answered_ids = {row[0] for row in result.all()}

        result = await session.execute(select(Question).where(~Question.id.in_(answered_ids)))
        next_question = result.scalars().first()

    if next_question:
        options = next_question.options.split(",")
        next_qid = next_question.id
        await state.update_data(qid=next_qid)
        if callback.message is not None:
            await callback.message.answer(str(next_question.word), reply_markup=get_keyboard(options, next_qid)) # type: ignore
    else:
        if callback.message is not None:
            await callback.message.answer(MESSAGES["game_over"])
        await state.clear()


def register_handlers_game(dp: Dispatcher):
    dp.message.register(start_game, lambda m: m.text == "🎮 Быстротест")
    dp.message.register(reset_progress, lambda m: m.text == "🔄 Сбросить прогресс")
    dp.callback_query.register(
        process_answer,
        lambda c: c.data.startswith("answer"),
        StateFilter(GameStates.waiting_for_answer)
    )
