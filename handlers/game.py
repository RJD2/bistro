from asyncio import to_thread
from html import escape

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
from statistics_chart import render_statistics_chart
from text_formatting import capitalize_first


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
        display_options = [capitalize_first(option) for option in options]
        if len(options) == 2:
            msg = f"{display_options[0]} вы или {display_options[1]}"
        else:
            msg = f"{display_options[0]} вы, {", ".join(display_options[1:-1])} или {display_options[-1]}"
        await message.answer(
            f"<b>{escape(capitalize_first(question.word))}</b>\n\nЭто был быстротест {escape(msg)}",
            parse_mode="HTML",
            reply_markup=get_keyboard(options, qid),
        )
        await state.set_state(GameStates.waiting_for_answer)


async def get_question_statistics(question_id: int, session: AsyncSession) -> dict[str, float]:
    # Подсчет количества ответов для каждого варианта
    result = await session.execute(
        select(Answer.choice, func.count(Answer.choice))
        .where(Answer.question_id == question_id)
        .group_by(Answer.choice)
    )
    choices_count = {row[0]: row[1] for row in result.all()}
    total_count = sum(choices_count.values())

    if total_count == 0:
        return {}

    # Сохраняем порядок вариантов, включая те, которые еще никто не выбрал.
    question = await session.get(Question, question_id)
    if question is not None:
        choices_count = {**dict.fromkeys(question.options.split(","), 0), **choices_count}

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
    await message.answer("Ваш прогресс сброшен. Вы можете начать игру заново.", reply_markup=None)


async def process_answer(callback: types.CallbackQuery, state: FSMContext):
    if not callback.from_user or not callback.data or not isinstance(callback.message, types.Message):
        return
    await callback.answer()
    message = callback.message
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
    stats_message = "📊 Статистика ответов:\n" + "\n".join(
        f"{escape(capitalize_first(choice))}: {percentage:.0f}%" for choice, percentage in stats.items()
    )
    caption = (
        f"<b>{escape(capitalize_first(question.word))}</b>\n\n"
        f"Это был быстротест, вы {escape(capitalize_first(question.options.split(',')[opt]))}\n\n"
        f"{stats_message}"
    )
    chart = await to_thread(render_statistics_chart, stats)
    photo = types.BufferedInputFile(chart, filename=f"statistics_{qid}.png")

    # Заменяем вопрос диаграммой с текстовой статистикой и убираем кнопки.
    if len(caption) <= 1024:
        await message.edit_media(
            media=types.InputMediaPhoto(media=photo, caption=caption, parse_mode="HTML"),
            reply_markup=None,
        )
    else:
        # Длинная статистика не поместится в подпись к фото Telegram.
        await message.edit_text(text=caption, parse_mode="HTML", reply_markup=None)
        await message.answer_photo(
            photo=photo,
            caption=f"📊 {capitalize_first(question.word)}",
            parse_mode=None,
        )

    # Ищем следующий вопрос
    async with async_session() as session:
        result = await session.execute(select(Answer.question_id).where(Answer.user_id == user.id))
        answered_ids = {row[0] for row in result.all()}

        result = await session.execute(select(Question).where(~Question.id.in_(answered_ids)))
        next_question = result.scalars().first()

    if next_question:
        options = next_question.options.split(",")
        display_options = [capitalize_first(option) for option in options]
        if len(options) == 2:
            msg = f"{display_options[0]} вы или {display_options[1]}"
        else:
            msg = f"{display_options[0]} вы, {", ".join(display_options[1:-1])} или {display_options[-1]}"
        next_qid = next_question.id
        await state.update_data(qid=next_qid)
        await message.answer(
            f"<b>{escape(capitalize_first(next_question.word))}</b>\n\nЭто был быстротест {escape(msg)}",
            parse_mode="HTML",
            reply_markup=get_keyboard(options, next_qid) # type: ignore
        )
    else:
        await message.answer(MESSAGES["game_over"])
        await state.clear()


def register_handlers_game(dp: Dispatcher):
    dp.message.register(start_game, lambda m: m.text == "🎮 Быстротест")
    dp.message.register(reset_progress, lambda m: m.text == "🔄 Сбросить прогресс")
    dp.callback_query.register(
        process_answer,
        lambda c: c.data.startswith("answer"),
        StateFilter(GameStates.waiting_for_answer)
    )
