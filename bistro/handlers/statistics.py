from asyncio import to_thread

from aiogram import Dispatcher, F, types
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bistro.charts import messages as chart_messages
from bistro.charts.updates import recipient_guard, refresh_question_charts, statistics_key
from bistro.database.session import async_session
from bistro.handlers.game import (
    _is_group, _personal_message_options, get_question_statistics, result_text,
)
from bistro.database.models import Answer, Question, User
from bistro.charts.rendering import render_statistics_chart
from bistro.ui.telegram_ephemeral import EditEphemeralMessageText
from bistro.ui.sharing import get_result_keyboard
from bistro.ui.text_formatting import capitalize_first


STATISTICS_PAGE_SIZE = 10
_SUPPORTED_CHATS = {ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP}


async def get_answered_questions(tg_id: int, page: int, session: AsyncSession):
    # EXISTS returns each word once even if an old installation has duplicate answers.
    answered = select(Answer.id).join(User, User.id == Answer.user_id).where(
        User.tg_id == tg_id, Answer.question_id == Question.id,
    ).exists()
    result = await session.execute(select(func.count()).select_from(Question).where(answered))
    count = result.scalar_one()
    if not count:
        return [], 0, 0
    page = min(max(page, 0), (count - 1) // STATISTICS_PAGE_SIZE)
    result = await session.execute(
        select(Question).where(answered).order_by(Question.id)
        .offset(page * STATISTICS_PAGE_SIZE).limit(STATISTICS_PAGE_SIZE)
    )
    return list(result.scalars().all()), count, page


def _list_view(questions: list[Question], count: int, page: int, tg_id: int):
    if not count:
        return (
            "📊 Статистика\n\nВы еще не ответили ни на одно слово. Начните Быстротест: /play.",
            None,
        )
    text = (
        f"📊 Статистика\n\nСлов с ответом: {count}.\n"
        "Выберите слово, чтобы увидеть распределение ответов и свой выбор."
    )
    buttons = [[types.InlineKeyboardButton(
        text=capitalize_first(question.word),
        callback_data=f"statistics:word:{tg_id}:{question.id}",
    )] for question in questions]
    pages = (count + STATISTICS_PAGE_SIZE - 1) // STATISTICS_PAGE_SIZE
    if pages > 1:
        text += f"\n\nСтраница {page + 1} из {pages}."
        navigation = []
        if page > 0:
            navigation.append(types.InlineKeyboardButton(
                text="← Назад", callback_data=f"statistics:page:{tg_id}:{page - 1}",
            ))
        if page + 1 < pages:
            navigation.append(types.InlineKeyboardButton(
                text="Далее →", callback_data=f"statistics:page:{tg_id}:{page + 1}",
            ))
        buttons.append(navigation)
    return text, types.InlineKeyboardMarkup(inline_keyboard=buttons)


async def show_statistics(message: types.Message):
    if message.from_user is None or message.chat.type not in _SUPPORTED_CHATS:
        return
    if _is_group(message):
        # A callback is required to send a personal message inside a group.
        await message.answer(
            "Нажмите кнопку, чтобы открыть свою статистику. Список слов и результаты будут видны только вам.",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[
                types.InlineKeyboardButton(text="📊 Моя статистика", callback_data="statistics:open"),
            ]]),
        )
        return
    async with async_session() as session:
        questions, count, page = await get_answered_questions(message.from_user.id, 0, session)
    text, keyboard = _list_view(questions, count, page, message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


async def _show_list(callback: types.CallbackQuery, page: int):
    async with async_session() as session:
        questions, count, page = await get_answered_questions(callback.from_user.id, page, session)
    text, keyboard = _list_view(questions, count, page, callback.from_user.id)
    message = callback.message
    if _is_group(message):
        async with recipient_guard(callback.bot.id, message.chat.id, callback.from_user.id):
            ephemeral_id = getattr(message, "ephemeral_message_id", None)
            if isinstance(ephemeral_id, int):
                await callback.bot(EditEphemeralMessageText(
                    chat_id=message.chat.id,
                    receiver_user_id=callback.from_user.id,
                    ephemeral_message_id=ephemeral_id,
                    text=text,
                    reply_markup=keyboard,
                ))
                list_message = message
            else:
                list_message = await message.answer(
                    text, reply_markup=keyboard, **_personal_message_options(callback),
                )
            # Telegram may reuse a previous chart's personal message ID for this list.
            await chart_messages.forget_message(callback.bot.id, list_message, callback.from_user.id)
    else:
        await message.edit_text(text, reply_markup=keyboard)


async def _show_word(callback: types.CallbackQuery, question_id: int):
    async with async_session() as session:
        result = await session.execute(
            select(Question, Answer.choice)
            .join(Answer, Answer.question_id == Question.id)
            .join(User, User.id == Answer.user_id)
            .where(User.tg_id == callback.from_user.id, Question.id == question_id)
            .order_by(Answer.id).limit(1)
        )
        saved = result.first()
        if saved is None:
            await callback.answer("Ответ не найден. Откройте статистику заново.", show_alert=True)
            return
        question, choice = saved
        statistics = await get_question_statistics(question_id, session)
    await callback.answer()
    caption = result_text(question, choice)
    chart = await to_thread(render_statistics_chart, statistics, selected_choice=choice)
    photo = types.BufferedInputFile(chart, filename=f"statistics_{question_id}.png")
    result_keyboard = await get_result_keyboard(callback.bot, choice, question_id)
    message = callback.message
    personal = _is_group(message)
    receiver_id = callback.from_user.id if personal else 0
    options = _personal_message_options(callback) if personal else {}
    async with recipient_guard(callback.bot.id, message.chat.id, receiver_id):
        chart_caption, parse_mode = caption, "HTML"
        if len(caption.encode("utf-16-le")) // 2 > 1024:
            text_message = await message.answer(caption, parse_mode="HTML", **options)
            if personal:
                await chart_messages.forget_message(callback.bot.id, text_message, receiver_id)
            chart_caption, parse_mode = None, None
        chart_message = await message.answer_photo(
            photo=photo, caption=chart_caption, parse_mode=parse_mode, **options,
            reply_markup=result_keyboard,
        )
        await chart_messages.remember_chart(
            bot_id=callback.bot.id, question_id=question_id, message=chart_message,
            receiver_user_id=receiver_id, caption=chart_caption,
            parse_mode=parse_mode, statistics_key=statistics_key(statistics),
            reply_markup=result_keyboard, selected_choice=choice,
        )
    # Catch answers that arrived while this chart was being rendered or sent.
    await refresh_question_charts(
        callback.bot, question_id, get_question_statistics, statistics=statistics, chart=chart,
        selected_choice=choice,
    )


async def process_statistics(callback: types.CallbackQuery):
    if (
        not isinstance(callback.message, types.Message)
        or callback.message.chat.type not in _SUPPORTED_CHATS
    ):
        await callback.answer("Статистика недоступна в этом чате.")
        return
    if callback.data == "statistics:open":
        action, value = "page", 0
    else:
        try:
            prefix, action, raw_owner, raw_value = (callback.data or "").split(":")
            owner, value = int(raw_owner), int(raw_value)
            if (
                prefix != "statistics" or action not in {"page", "word"}
                or not 0 < owner <= 2**63 - 1
                or not 0 <= value <= 2**31 - 1
                or (action == "word" and value == 0)
            ):
                raise ValueError
        except ValueError:
            await callback.answer("Некорректный запрос статистики.", show_alert=True)
            return
        if owner != callback.from_user.id:
            await callback.answer("Откройте свою статистику через /statistics.", show_alert=True)
            return
    if action == "word":
        await _show_word(callback, value)
        return
    await callback.answer()
    try:
        await _show_list(callback, value)
    except TelegramBadRequest as error:
        if "message is not modified" not in error.message.casefold():
            raise


def register_handlers_statistics(dp: Dispatcher):
    dp.message.register(show_statistics, Command("statistics"))
    dp.message.register(show_statistics, F.text.in_({"📊 Статистика", "Статистика"}))
    dp.callback_query.register(process_statistics, F.data.startswith("statistics:"))
