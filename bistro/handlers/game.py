from asyncio import to_thread
from html import escape

from aiogram import F, types, Dispatcher
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command, StateFilter
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from bistro.charts import messages as chart_messages
from bistro.charts.updates import recipient_guard, refresh_question_charts, statistics_key
from bistro.database.session import async_session
from bistro.database.models import User, Answer, Question
from bistro.services.questions import next_available_question, question_choices
from bistro.handlers.states import GameStates
from bistro.ui.keyboards import get_keyboard
from bistro.ui.messages import MESSAGES
from bistro.ui.sharing import get_result_keyboard
from bistro.charts.rendering import render_statistics_chart
from bistro.ui.telegram_ephemeral import EditEphemeralMessageMedia, EditEphemeralMessageText
from bistro.ui.text_formatting import capitalize_first


async def get_or_create_user(tg_id: int, session: AsyncSession):
    # The same user may interact from multiple chats at the same time.
    await session.execute(
        insert(User).values(tg_id=tg_id).on_conflict_do_nothing(index_elements=[User.tg_id])
    )
    await session.commit()
    result = await session.execute(select(User).where(User.tg_id == tg_id))
    return result.scalars().one()


def _is_group(message: types.Message) -> bool:
    return message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}


def question_text(question: Question) -> str:
    return (
        f"<b>{escape(capitalize_first(question.word))}</b>\n\n"
        "Какая ассоциация первой пришла вам в голову?"
    )


def result_text(question: Question, selected_choice: str) -> str:
    options = question.options.split(",")
    if len(options) == 2:
        prompt = f"{options[0]} вы или {options[1]}"
    else:
        prompt = f"{options[0]} вы, {', '.join(options[1:-1])} или {options[-1]}"
    return (
        f"<b>{escape(capitalize_first(question.word))}</b>\n\n"
        f"Это был быстротест {escape(prompt)}\n\n"
        f"И вы {escape(selected_choice)}"
    )


def _personal_message_options(callback: types.CallbackQuery, *, replace: bool = False) -> dict:
    parameters = {
        "receiver_user_id": callback.from_user.id,
        "callback_query_id": callback.id,
    }
    if replace:
        parameters["replace_callback_query_message"] = True
    return {"ephemeral_message_parameters": parameters}


async def _show_group_result(
    callback: types.CallbackQuery,
    caption: str,
    photo: types.BufferedInputFile,
    *,
    question_id: int,
    chart_key: str,
    reply_markup: types.InlineKeyboardMarkup,
):
    message = callback.message
    ephemeral_id = getattr(message, "ephemeral_message_id", None)
    edit_options = {
        "chat_id": message.chat.id,
        "receiver_user_id": callback.from_user.id,
        "ephemeral_message_id": ephemeral_id,
    }
    async with recipient_guard(callback.bot.id, message.chat.id, callback.from_user.id):
        if len(caption) <= 1024:
            if isinstance(ephemeral_id, int):
                await callback.bot(EditEphemeralMessageMedia(
                    **edit_options,
                    media=types.InputMediaPhoto(media=photo, caption=caption, parse_mode="HTML"),
                    reply_markup=reply_markup,
                ))
                chart_message = message
            else:
                # Replace the shared question only for the participant who answered.
                chart_message = await message.answer_photo(
                    photo=photo,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                    **_personal_message_options(callback, replace=True),
                )
            chart_caption, parse_mode = caption, "HTML"
        else:
            if isinstance(ephemeral_id, int):
                await callback.bot(EditEphemeralMessageText(**edit_options, text=caption, parse_mode="HTML"))
                text_message = message
            else:
                text_message = await message.answer(
                    caption, parse_mode="HTML", **_personal_message_options(callback, replace=True),
                )
            await chart_messages.forget_message(callback.bot.id, text_message, callback.from_user.id)
            chart_message = await message.answer_photo(
                photo=photo, reply_markup=reply_markup, **_personal_message_options(callback),
            )
            chart_caption, parse_mode = None, None
        await chart_messages.remember_chart(
            bot_id=callback.bot.id, question_id=question_id, message=chart_message,
            receiver_user_id=callback.from_user.id, caption=chart_caption,
            parse_mode=parse_mode, statistics_key=chart_key,
            reply_markup=reply_markup,
        )


async def start_game(message: types.Message, state: FSMContext):
    if not message.from_user:
        return
    async with async_session() as session:
        user = await get_or_create_user(message.from_user.id, session)

        # Find questions the user has already answered.
        result = await session.execute(select(Answer.question_id).where(Answer.user_id == user.id))
        answered_ids = {row[0] for row in result.all()}

        # Select the first unanswered question.
        question = await next_available_question(session, ~Question.id.in_(answered_ids))

        if not question:
            await message.answer(MESSAGES["game_over"])
            return

        choices = question_choices(question)
        await _send_question(message, state, question, choices)


async def _send_question(
    message: types.Message, state: FSMContext, question: Question, choices: list[str],
):
    qid = int(question.id)
    await message.answer(
        question_text(question),
        parse_mode="HTML",
        reply_markup=get_keyboard(choices, qid),
    )
    if not _is_group(message):
        await state.update_data(qid=qid)
        await state.set_state(GameStates.waiting_for_answer)


async def start_shared_game(message: types.Message, state: FSMContext, question_id: int):
    if message.from_user is None or message.chat.type != ChatType.PRIVATE:
        return
    async with async_session() as session:
        question = await session.get(Question, question_id)
    try:
        choices = question_choices(question) if question is not None else None
    except ValueError:
        choices = None
    if choices is None:
        await message.answer("Этот вопрос больше недоступен. Предложим другой Быстротест.")
        await start_game(message, state)
        return
    # A shared link explicitly chooses this question, even if it was answered
    # before. process_answer will preserve the recipient's saved choice.
    await _send_question(message, state, question, choices)


async def get_question_statistics(question_id: int, session: AsyncSession) -> dict[str, float]:
    # Count answers for each option.
    result = await session.execute(
        select(Answer.choice, func.count(Answer.choice))
        .where(Answer.question_id == question_id)
        .group_by(Answer.choice)
    )
    choices_count = {row[0]: row[1] for row in result.all()}
    total_count = sum(choices_count.values())

    if total_count == 0:
        return {}

    # Preserve option order, including options that nobody has selected yet.
    question = await session.get(Question, question_id)
    if question is not None:
        choices_count = {**dict.fromkeys(question.options.split(","), 0), **choices_count}

    # Calculate percentages.
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
        await session.get(User, user.id, with_for_update=True)

        result = await session.execute(
            select(Answer.question_id).where(Answer.user_id == user.id).distinct()
        )
        affected_questions = [row[0] for row in result.all()]

        # Delete all of the user's answers.
        stmt = delete(Answer).where(Answer.user_id == user.id)
        await session.execute(stmt)
        await session.commit()

    try:
        await state.clear()
        await message.answer("Ваш прогресс сброшен. Вы можете начать игру заново.", reply_markup=None)
    finally:
        for question_id in affected_questions:
            await refresh_question_charts(message.bot, question_id, get_question_statistics)


async def process_answer(callback: types.CallbackQuery, state: FSMContext):
    if not callback.from_user or not callback.data or not isinstance(callback.message, types.Message):
        await callback.answer("Этот вопрос недоступен.")
        return
    message = callback.message
    try:
        action, raw_qid, raw_opt = callback.data.split(":")
        qid, opt = int(raw_qid), int(raw_opt)
        if action not in {"answer", "autotest_answer"} or not 0 < qid <= 2**31 - 1 or opt < 0:
            raise ValueError
    except ValueError:
        await callback.answer("Некорректный вариант ответа.", show_alert=True)
        return
    is_group = _is_group(message)

    async with async_session() as session:
        result = await session.execute(select(Question).where(Question.id == qid))
        question = result.scalars().first()
        if not question:
            await callback.answer("Этот вопрос больше недоступен.", show_alert=True)
            return
        try:
            choices = question_choices(question)
        except ValueError:
            await callback.answer("У этого вопроса пока не заполнены ассоциации. Выберите другой вопрос: /play.", show_alert=True)
            return
        if opt >= len(choices):
            await callback.answer("Некорректный вариант ответа.", show_alert=True)
            return
        options = question.options.split(",")

        user = await get_or_create_user(callback.from_user.id, session)
        # Lock the user row to prevent duplicate votes, even when callbacks
        # arrive concurrently from different chats or bot processes.
        await session.get(User, user.id, with_for_update=True)
        result = await session.execute(
            select(Answer).where(Answer.user_id == user.id, Answer.question_id == qid).order_by(Answer.id)
        )
        previous_answer = result.scalars().first()
        selected_choice = previous_answer.choice if previous_answer else options[opt]
        if previous_answer is None:
            session.add(Answer(user_id=user.id, question_id=qid, choice=selected_choice))
        await session.commit()

    stats = None
    chart = None
    try:
        if is_group:
            # Show a short personal result immediately, before rendering the chart.
            await callback.answer(f"Это был быстротест, вы {selected_choice}"[:200], cache_time=0)
        else:
            await callback.answer()
        async with async_session() as session:
            stats = await get_question_statistics(qid, session)

        # Build the caption with the selected answer.
        caption = result_text(question, selected_choice)
        chart = await to_thread(render_statistics_chart, stats)
        photo = types.BufferedInputFile(chart, filename=f"statistics_{qid}.png")
        chart_key = statistics_key(stats)
        result_keyboard = await get_result_keyboard(callback.bot, selected_choice, qid)

        if is_group:
            await _show_group_result(
                callback, caption, photo, question_id=qid, chart_key=chart_key,
                reply_markup=result_keyboard,
            )
            # The next shared question requires the game button or /play.
            # The finally block still refreshes charts for other participants.
            return
        else:
            async with recipient_guard(callback.bot.id, message.chat.id, 0):
                if len(caption) <= 1024:
                    await message.edit_media(
                        media=types.InputMediaPhoto(media=photo, caption=caption, parse_mode="HTML"),
                        reply_markup=result_keyboard,
                    )
                    chart_message = message
                    chart_caption, parse_mode = caption, "HTML"
                else:
                    # A long result exceeds Telegram's photo caption limit.
                    await message.edit_text(text=caption, parse_mode="HTML", reply_markup=None)
                    chart_caption = f"📊 {capitalize_first(question.word)}"
                    parse_mode = None
                    chart_message = await message.answer_photo(
                        photo=photo, caption=chart_caption, parse_mode=parse_mode,
                        reply_markup=result_keyboard,
                    )
                await chart_messages.remember_chart(
                    bot_id=callback.bot.id, question_id=qid, message=chart_message,
                    receiver_user_id=0, caption=chart_caption,
                    parse_mode=parse_mode, statistics_key=chart_key,
                    reply_markup=result_keyboard,
                )

        if action == "autotest_answer":
            # Daily questions show their result without starting a regular game
            # or changing a draft/the current game stored in the FSM.
            return

        # Find the next question.
        async with async_session() as session:
            result = await session.execute(select(Answer.question_id).where(Answer.user_id == user.id))
            answered_ids = {row[0] for row in result.all()}

            next_question = await next_available_question(session, ~Question.id.in_(answered_ids))

        if next_question:
            choices = question_choices(next_question)
            next_qid = next_question.id
            await state.update_data(qid=next_qid)
            await message.answer(
                question_text(next_question),
                parse_mode="HTML",
                reply_markup=get_keyboard(choices, next_qid),
            )
        else:
            await message.answer(MESSAGES["game_over"])
            await state.clear()
    finally:
        # A committed vote must refresh other players even if this player's
        # result or next question could not be delivered.
        await refresh_question_charts(
            callback.bot, qid, get_question_statistics, statistics=stats, chart=chart,
        )


def register_handlers_game(dp: Dispatcher):
    dp.message.register(start_game, Command("play"))
    dp.message.register(start_game, F.text == "🎮 Быстротест")
    dp.message.register(reset_progress, Command("reset"))
    dp.message.register(reset_progress, F.text == "🔄 Сбросить прогресс")
    dp.callback_query.register(
        process_answer,
        F.data.startswith("answer:") | F.data.startswith("autotest_answer:"),
        F.message.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    )
    dp.callback_query.register(
        process_answer,
        F.data.startswith("autotest_answer:"),
        F.message.chat.type == ChatType.PRIVATE,
    )
    dp.callback_query.register(
        process_answer,
        F.data.startswith("answer:"),
        F.message.chat.type == ChatType.PRIVATE,
        StateFilter(GameStates.waiting_for_answer),
    )
