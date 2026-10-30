import logging
from html import escape

from aiogram import Dispatcher, F, types
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

import config
from db import async_session
from keyboards import get_main_menu
from models import Question
from states import BulkQuestionCreationStates, QuestionCreationStates
from text_formatting import capitalize_first


logger = logging.getLogger(__name__)
ADD_QUESTION_BUTTON = "➕ Добавить вопрос"
ADD_QUESTIONS_BUTTON = "📚 Добавить вопросы"
CONFIRM_CALLBACK = "admin_question:confirm"
CANCEL_CALLBACK = "admin_question:cancel"
BULK_CONFIRM_CALLBACK = "admin_questions:confirm"
BULK_CANCEL_CALLBACK = "admin_questions:cancel"
MAX_BULK_QUESTIONS = 50
CREATION_STATES = {
    QuestionCreationStates.waiting_for_word.state,
    QuestionCreationStates.waiting_for_options.state,
    QuestionCreationStates.waiting_for_confirmation.state,
}
BULK_CREATION_STATES = {
    BulkQuestionCreationStates.waiting_for_words.state,
    BulkQuestionCreationStates.waiting_for_options.state,
    BulkQuestionCreationStates.waiting_for_confirmation.state,
}


def _is_owner(user: types.User | None) -> bool:
    return config.ADMIN_ID > 0 and user is not None and user.id == config.ADMIN_ID


async def _allow_message(message: types.Message) -> bool:
    if _is_owner(message.from_user) and message.chat.type == ChatType.PRIVATE:
        return True
    await message.answer("Добавлять вопросы может только владелец бота в личном чате.")
    return False


async def _allow_callback(callback: types.CallbackQuery) -> bool:
    if (
        _is_owner(callback.from_user)
        and isinstance(callback.message, types.Message)
        and callback.message.chat.type == ChatType.PRIVATE
    ):
        return True
    await callback.answer("Добавлять вопросы может только владелец бота в личном чате.", show_alert=True)
    return False


def _validate_word(text: str | None) -> str:
    if not isinstance(text, str):
        raise ValueError("Отправьте название вопроса текстом.")
    word = text.strip()
    if not word or len(word) > 100 or not word.isprintable() or word.startswith("/"):
        raise ValueError("Название должно содержать от 1 до 100 символов в одной строке. Для отмены — /cancel.")
    return word


def _validate_options(options: list[str]) -> list[str]:
    if not isinstance(options, list) or not 2 <= len(options) <= 8:
        raise ValueError("Нужно от 2 до 8 вариантов ответа через запятую.")
    normalized = []
    for option in options:
        if not isinstance(option, str):
            raise ValueError("Отправьте варианты ответа текстом через запятую.")
        option = option.strip()
        if (
            not option
            or len(option) > 64
            or not option.isprintable()
            or "," in option
            or option.startswith("/")
        ):
            raise ValueError("Каждый вариант должен содержать от 1 до 64 символов в одной строке. Пустые варианты недопустимы.")
        normalized.append(option)
    if len({option.casefold() for option in normalized}) != len(normalized):
        raise ValueError("Варианты ответа не должны повторяться.")
    return normalized


def _validate_words(text: str | None) -> list[str]:
    if not isinstance(text, str):
        raise ValueError("Отправьте названия вопросов текстом через запятую.")
    words = text.split(",")
    if not 1 <= len(words) <= MAX_BULK_QUESTIONS:
        raise ValueError(f"За один раз можно добавить от 1 до {MAX_BULK_QUESTIONS} вопросов.")
    words = [_validate_word(word) for word in words]
    if len({word.casefold() for word in words}) != len(words):
        raise ValueError("Названия вопросов в списке не должны повторяться.")
    return words


def _validate_bulk_draft(data: dict, *, complete: bool) -> tuple[list[str], list[dict], int]:
    words = data.get("bulk_words")
    questions = data.get("bulk_questions")
    index = data.get("bulk_index")
    if (
        not isinstance(words, list)
        or not 1 <= len(words) <= MAX_BULK_QUESTIONS
        or not isinstance(questions, list)
        or type(index) is not int
        or not 0 <= index <= len(words)
        or len(questions) != index
        or (complete and index != len(words))
        or (not complete and index == len(words))
    ):
        raise ValueError("Черновик списка неполный. Начните заново: /add_questions.")
    words = [_validate_word(word) for word in words]
    if any("," in word for word in words) or len({word.casefold() for word in words}) != len(words):
        raise ValueError("Названия вопросов в черновике некорректны. Начните заново: /add_questions.")
    validated = []
    for position, question in enumerate(questions):
        if not isinstance(question, dict) or question.get("word") != words[position]:
            raise ValueError("Черновик списка неполный. Начните заново: /add_questions.")
        validated.append({"word": words[position], "options": _validate_options(question.get("options"))})
    return words, validated, index


def _bulk_options_prompt(words: list[str], index: int) -> str:
    return (
        f"Вопрос {index + 1} из {len(words)}: «{capitalize_first(words[index])}»\n\n"
        "Отправьте от 2 до 8 вариантов ответа через запятую, каждый до 64 символов.\n"
        "Для отмены всего списка — /cancel."
    )


def _bulk_preview_chunks(questions: list[dict]) -> list[str]:
    chunks = []
    current = "Проверьте вопросы и варианты ответа:\n\n"
    for index, question in enumerate(questions, 1):
        section = (
            f"{index}. {capitalize_first(question['word'])}\n"
            + "\n".join(f"   • {capitalize_first(option)}" for option in question["options"])
            + "\n\n"
        )
        # Telegram считает длину сообщения в кодовых единицах UTF-16.
        if len((current + section).encode("utf-16-le")) // 2 > 3500:
            chunks.append(current.rstrip())
            current = ""
        current += section
    if current:
        chunks.append(current.rstrip())
    return chunks


async def _remove_keyboard(message: types.Message) -> None:
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        # Сообщение уже могло быть изменено или удалено.
        pass


async def _remove_draft_keyboard(message: types.Message, data: dict) -> None:
    preview_id = data.get("confirmation_message_id")
    if isinstance(preview_id, int):
        try:
            await message.bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=preview_id,
                reply_markup=None,
            )
        except TelegramBadRequest:
            pass


async def start_add_question(message: types.Message, state: FSMContext):
    if not await _allow_message(message):
        return
    await _remove_draft_keyboard(message, await state.get_data())
    await state.clear()
    await state.set_state(QuestionCreationStates.waiting_for_word)
    await message.answer(
        "Отправьте слово или название нового вопроса (до 100 символов).\n\nДля отмены — /cancel.",
        reply_markup=types.ReplyKeyboardRemove(),
    )


async def receive_question_word(message: types.Message, state: FSMContext):
    if not await _allow_message(message):
        return
    if await state.get_state() != QuestionCreationStates.waiting_for_word.state:
        return
    try:
        word = _validate_word(message.text)
    except ValueError as error:
        await message.answer(str(error))
        return
    await state.update_data(word=word)
    await state.set_state(QuestionCreationStates.waiting_for_options)
    await message.answer(
        "Теперь отправьте от 2 до 8 вариантов ответа через запятую, каждый до 64 символов.\n"
        "Например: салат, император\n\nДля отмены — /cancel."
    )


async def receive_question_options(message: types.Message, state: FSMContext):
    if not await _allow_message(message):
        return
    if await state.get_state() != QuestionCreationStates.waiting_for_options.state:
        return
    try:
        if not isinstance(message.text, str):
            raise ValueError("Отправьте варианты ответа текстом через запятую.")
        options = _validate_options(message.text.split(","))
        data = await state.get_data()
        word = _validate_word(data.get("word"))
    except ValueError as error:
        await message.answer(str(error))
        return

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="✅ Добавить", callback_data=CONFIRM_CALLBACK),
        types.InlineKeyboardButton(text="❌ Отмена", callback_data=CANCEL_CALLBACK),
    ]])
    preview = await message.answer(
        f"<b>{escape(capitalize_first(word))}</b>\n\nВарианты ответа:\n"
        + "\n".join(f"{index}. {escape(capitalize_first(option))}" for index, option in enumerate(options, 1))
        + "\n\nДобавить этот вопрос?",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    await state.update_data(options=options, confirmation_message_id=preview.message_id)
    await state.set_state(QuestionCreationStates.waiting_for_confirmation)


async def _active_callback_draft(
    callback: types.CallbackQuery,
    state: FSMContext,
    expected_state: str = QuestionCreationStates.waiting_for_confirmation.state,
    restart_command: str = "/add_question",
) -> dict | None:
    data = await state.get_data()
    if (
        await state.get_state() != expected_state
        or data.get("confirmation_message_id") != callback.message.message_id
    ):
        await callback.answer(f"Этот черновик уже закрыт. Начните заново: {restart_command}.", show_alert=True)
        await _remove_keyboard(callback.message)
        return None
    return data


async def confirm_add_question(callback: types.CallbackQuery, state: FSMContext):
    if not await _allow_callback(callback):
        return
    if callback.data != CONFIRM_CALLBACK:
        await callback.answer("Неизвестное действие.")
        return
    data = await _active_callback_draft(callback, state)
    if data is None:
        return
    await callback.answer()
    try:
        word = _validate_word(data.get("word"))
        options = _validate_options(data.get("options"))
    except ValueError:
        await state.clear()
        await _remove_keyboard(callback.message)
        await callback.message.answer(
            "Черновик неполный. Начните заново: /add_question.",
            reply_markup=get_main_menu(is_admin=True),
        )
        return

    try:
        async with async_session() as session:
            result = await session.execute(
                insert(Question)
                .values(word=word, options=",".join(options))
                .on_conflict_do_nothing(index_elements=["word"])
                .returning(Question.id)
            )
            question_id = result.scalar_one_or_none()
            await session.commit()
    except SQLAlchemyError:
        logger.exception("Не удалось сохранить новый вопрос")
        await callback.message.answer("Не удалось сохранить вопрос. Попробуйте нажать «Добавить» еще раз или отмените: /cancel.")
        return

    await state.clear()
    await _remove_keyboard(callback.message)
    if question_id is None:
        await state.set_state(QuestionCreationStates.waiting_for_word)
        await callback.message.answer("Вопрос с таким названием уже есть. Отправьте другое название или /cancel.")
        return
    await callback.message.answer(
        f"✅ Вопрос «{capitalize_first(word)}» добавлен. Он уже доступен в игре.",
        parse_mode=None,
        reply_markup=get_main_menu(is_admin=True),
    )


async def cancel_add_question(message: types.Message, state: FSMContext):
    if not await _allow_message(message):
        return
    if await state.get_state() not in CREATION_STATES:
        return
    await _remove_draft_keyboard(message, await state.get_data())
    await state.clear()
    await message.answer("Добавление вопроса отменено.", reply_markup=get_main_menu(is_admin=True))


async def cancel_question_callback(callback: types.CallbackQuery, state: FSMContext):
    if not await _allow_callback(callback):
        return
    if callback.data != CANCEL_CALLBACK:
        await callback.answer("Неизвестное действие.")
        return
    if await _active_callback_draft(callback, state) is None:
        return
    await callback.answer()
    await state.clear()
    await _remove_keyboard(callback.message)
    await callback.message.answer("Добавление вопроса отменено.", reply_markup=get_main_menu(is_admin=True))


async def remind_question_confirmation(message: types.Message, state: FSMContext):
    if not await _allow_message(message):
        return
    await message.answer("Подтвердите вопрос кнопкой «Добавить» под черновиком или отмените: /cancel.")


async def start_add_questions(message: types.Message, state: FSMContext):
    if not await _allow_message(message):
        return
    await _remove_draft_keyboard(message, await state.get_data())
    await state.clear()
    await state.set_state(BulkQuestionCreationStates.waiting_for_words)
    await message.answer(
        "Отправьте названия вопросов через запятую, например: Цезарь, Замок, Камера.\n"
        f"До {MAX_BULK_QUESTIONS} вопросов, каждый до 100 символов. "
        "Затем я по очереди спрошу варианты ответа.\n\nДля отмены — /cancel.",
        reply_markup=types.ReplyKeyboardRemove(),
    )


async def receive_question_words(message: types.Message, state: FSMContext):
    if not await _allow_message(message):
        return
    if await state.get_state() != BulkQuestionCreationStates.waiting_for_words.state:
        return
    try:
        words = _validate_words(message.text)
    except ValueError as error:
        await message.answer(str(error))
        return
    await state.update_data(bulk_words=words, bulk_questions=[], bulk_index=0)
    await state.set_state(BulkQuestionCreationStates.waiting_for_options)
    await message.answer(_bulk_options_prompt(words, 0), parse_mode=None)


async def receive_bulk_question_options(message: types.Message, state: FSMContext):
    if not await _allow_message(message):
        return
    if await state.get_state() != BulkQuestionCreationStates.waiting_for_options.state:
        return
    try:
        if not isinstance(message.text, str):
            raise ValueError("Отправьте варианты ответа текстом через запятую.")
        options = _validate_options(message.text.split(","))
        words, questions, index = _validate_bulk_draft(await state.get_data(), complete=False)
    except ValueError as error:
        await message.answer(str(error))
        return
    questions.append({"word": words[index], "options": options})
    next_index = index + 1
    if next_index < len(words):
        await message.answer(_bulk_options_prompt(words, next_index), parse_mode=None)
        await state.update_data(bulk_questions=questions, bulk_index=next_index)
        return

    for chunk in _bulk_preview_chunks(questions):
        await message.answer(chunk, parse_mode=None)
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="✅ Добавить все", callback_data=BULK_CONFIRM_CALLBACK),
        types.InlineKeyboardButton(text="❌ Отмена", callback_data=BULK_CANCEL_CALLBACK),
    ]])
    preview = await message.answer(
        f"Список готов: {len(questions)} вопросов. Добавить все?\n"
        "Вопросы с уже существующими названиями будут пропущены.",
        reply_markup=keyboard,
    )
    await state.update_data(
        bulk_questions=questions,
        bulk_index=next_index,
        confirmation_message_id=preview.message_id,
    )
    await state.set_state(BulkQuestionCreationStates.waiting_for_confirmation)


async def confirm_add_questions(callback: types.CallbackQuery, state: FSMContext):
    if not await _allow_callback(callback):
        return
    if callback.data != BULK_CONFIRM_CALLBACK:
        await callback.answer("Неизвестное действие.")
        return
    data = await _active_callback_draft(
        callback,
        state,
        BulkQuestionCreationStates.waiting_for_confirmation.state,
        "/add_questions",
    )
    if data is None:
        return
    await callback.answer()
    try:
        _, questions, _ = _validate_bulk_draft(data, complete=True)
    except ValueError:
        await state.clear()
        await _remove_keyboard(callback.message)
        await callback.message.answer(
            "Черновик списка неполный. Начните заново: /add_questions.",
            reply_markup=get_main_menu(is_admin=True),
        )
        return

    try:
        async with async_session() as session:
            result = await session.execute(
                insert(Question)
                .values([
                    {"word": question["word"], "options": ",".join(question["options"])}
                    for question in questions
                ])
                .on_conflict_do_nothing(index_elements=["word"])
                .returning(Question.word)
            )
            added_count = len(result.scalars().all())
            await session.commit()
    except SQLAlchemyError:
        logger.exception("Не удалось сохранить список вопросов")
        await callback.message.answer(
            "Не удалось сохранить список. Попробуйте нажать «Добавить все» еще раз "
            "или отмените: /cancel."
        )
        return

    await state.clear()
    await _remove_keyboard(callback.message)
    await callback.message.answer(
        f"✅ Добавлено вопросов: {added_count}.\n"
        f"Пропущено существующих: {len(questions) - added_count}.",
        reply_markup=get_main_menu(is_admin=True),
    )


async def cancel_add_questions(message: types.Message, state: FSMContext):
    if not await _allow_message(message):
        return
    if await state.get_state() not in BULK_CREATION_STATES:
        return
    await _remove_draft_keyboard(message, await state.get_data())
    await state.clear()
    await message.answer(
        "Массовое добавление отменено. Вопросы не сохранены.",
        reply_markup=get_main_menu(is_admin=True),
    )


async def cancel_bulk_callback(callback: types.CallbackQuery, state: FSMContext):
    if not await _allow_callback(callback):
        return
    if callback.data != BULK_CANCEL_CALLBACK:
        await callback.answer("Неизвестное действие.")
        return
    if await _active_callback_draft(
        callback,
        state,
        BulkQuestionCreationStates.waiting_for_confirmation.state,
        "/add_questions",
    ) is None:
        return
    await callback.answer()
    await state.clear()
    await _remove_keyboard(callback.message)
    await callback.message.answer(
        "Массовое добавление отменено. Вопросы не сохранены.",
        reply_markup=get_main_menu(is_admin=True),
    )


async def remind_bulk_confirmation(message: types.Message, state: FSMContext):
    if not await _allow_message(message):
        return
    await message.answer("Подтвердите список кнопкой «Добавить все» под черновиком или отмените: /cancel.")


def register_handlers_admin(dp: Dispatcher):
    dp.message.register(start_add_question, Command("add_question"))
    dp.message.register(start_add_question, F.text == ADD_QUESTION_BUTTON)
    dp.message.register(start_add_questions, Command("add_questions"))
    dp.message.register(start_add_questions, F.text == ADD_QUESTIONS_BUTTON)
    dp.message.register(cancel_add_question, Command("cancel"), StateFilter(QuestionCreationStates))
    dp.message.register(cancel_add_questions, Command("cancel"), StateFilter(BulkQuestionCreationStates))
    dp.message.register(receive_question_word, StateFilter(QuestionCreationStates.waiting_for_word))
    dp.message.register(receive_question_options, StateFilter(QuestionCreationStates.waiting_for_options))
    dp.message.register(remind_question_confirmation, StateFilter(QuestionCreationStates.waiting_for_confirmation))
    dp.message.register(receive_question_words, StateFilter(BulkQuestionCreationStates.waiting_for_words))
    dp.message.register(receive_bulk_question_options, StateFilter(BulkQuestionCreationStates.waiting_for_options))
    dp.message.register(remind_bulk_confirmation, StateFilter(BulkQuestionCreationStates.waiting_for_confirmation))
    dp.callback_query.register(confirm_add_question, F.data == CONFIRM_CALLBACK)
    dp.callback_query.register(cancel_question_callback, F.data == CANCEL_CALLBACK)
    dp.callback_query.register(confirm_add_questions, F.data == BULK_CONFIRM_CALLBACK)
    dp.callback_query.register(cancel_bulk_callback, F.data == BULK_CANCEL_CALLBACK)
