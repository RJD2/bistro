import logging
from html import escape

from aiogram import Dispatcher, F, types
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from bistro import config
from bistro.database.session import async_session
from bistro.services.statistics import get_bot_statistics
from bistro.ui.keyboards import BOT_STATISTICS_BUTTON
from bistro.ui.menus import get_main_menu_for_chat
from bistro.database.models import Question
from bistro.handlers.states import AssocioCreationStates, BulkQuestionCreationStates, QuestionCreationStates
from bistro.ui.text_formatting import capitalize_first


logger = logging.getLogger(__name__)
ADD_QUESTION_BUTTON = "➕ Добавить вопрос"
ADD_QUESTIONS_BUTTON = "📚 Добавить вопросы"
ADD_ASSOCIO_BUTTON = "🧩 Добавить ассоциации"
CONFIRM_CALLBACK = "admin_question:confirm"
CANCEL_CALLBACK = "admin_question:cancel"
BULK_CONFIRM_CALLBACK = "admin_questions:confirm"
BULK_CANCEL_CALLBACK = "admin_questions:cancel"
ASSOCIO_CONFIRM_CALLBACK = "admin_associo:confirm"
ASSOCIO_CANCEL_CALLBACK = "admin_associo:cancel"
MAX_BULK_QUESTIONS = 50
CREATION_STATES = {
    QuestionCreationStates.waiting_for_word.state,
    QuestionCreationStates.waiting_for_options.state,
    QuestionCreationStates.waiting_for_associo.state,
    QuestionCreationStates.waiting_for_confirmation.state,
}
BULK_CREATION_STATES = {
    BulkQuestionCreationStates.waiting_for_words.state,
    BulkQuestionCreationStates.waiting_for_options.state,
    BulkQuestionCreationStates.waiting_for_associo.state,
    BulkQuestionCreationStates.waiting_for_confirmation.state,
}
ASSOCIO_CREATION_STATES = {
    AssocioCreationStates.waiting_for_associo.state,
    AssocioCreationStates.waiting_for_confirmation.state,
}


def _is_owner(user: types.User | None) -> bool:
    return config.ADMIN_ID > 0 and user is not None and user.id == config.ADMIN_ID


async def show_bot_statistics(message: types.Message):
    if not _is_owner(message.from_user) or message.chat.type != ChatType.PRIVATE:
        await message.answer("Статистика бота доступна только владельцу в личном чате.")
        return
    try:
        async with async_session() as session:
            statistics = await get_bot_statistics(session)
    except SQLAlchemyError:
        logger.exception("Не удалось загрузить статистику бота")
        await message.answer("Не удалось загрузить статистику бота. Попробуйте еще раз позже.")
        return

    average = f"{statistics.average_answers_per_participant:.2f}".replace(".", ",")
    text = (
        f"{BOT_STATISTICS_BUTTON}\n\n"
        f"Всего участников: {statistics.total_participants}\n"
        f"Всего вопросов: {statistics.total_questions}\n"
        f"Всего ответов на вопросы: {statistics.total_answers}\n"
        f"В среднем вопросов на участника: {average}\n\n"
    )
    popular = statistics.most_popular_question
    if popular is None:
        text += "Самый популярный вопрос: нет данных."
    else:
        text += (
            f"Самый популярный вопрос: «{capitalize_first(popular.word)}»\n"
            f"Ответивших участников: {popular.answer_count}"
        )
    text += (
        "\n\nУчастники — игроки хотя бы с одним сохранённым ответом. "
        "Сброшенные ответы не учитываются."
    )
    await message.answer(text, parse_mode=None)


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


def _validate_associo(associo: list[str], options: list[str]) -> list[str]:
    try:
        normalized = _validate_options(associo)
    except ValueError as error:
        raise ValueError(f"Ассоциации: {error}") from error
    if len(normalized) != len(options):
        raise ValueError(
            f"Нужно ровно {len(options)} ассоциаций через запятую, в том же порядке, что и результаты."
        )
    return normalized


def _associo_prompt(word: str, options: list[str]) -> str:
    return (
        f"«{capitalize_first(word)}»\n\nРезультаты (options):\n"
        + "\n".join(f"{index}. {option}" for index, option in enumerate(options, 1))
        + f"\n\nОтправьте ровно {len(options)} ассоциаций (associo) через запятую, "
        "каждая до 64 символов. Пользователь выбирает ассоциацию и получает результат на той же позиции.\n"
        "Например: для «повар,император» — «салат,Рим».\n\nДля отмены — /cancel."
    )


def _mapping_preview(options: list[str], associo: list[str], *, html: bool = False) -> str:
    render = escape if html else str
    return "\n".join(
        f"{index}. {render(association)} → {render(option)}"
        for index, (association, option) in enumerate(zip(associo, options), 1)
    )


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
        options = _validate_options(question.get("options"))
        validated.append({
            "word": words[position],
            "options": options,
            "associo": _validate_associo(question.get("associo"), options),
        })
    return words, validated, index


def _bulk_options_prompt(words: list[str], index: int) -> str:
    return (
        f"Вопрос {index + 1} из {len(words)}: «{capitalize_first(words[index])}»\n\n"
        "Отправьте от 2 до 8 результатов (options) через запятую, каждый до 64 символов.\n"
        "Для отмены всего списка — /cancel."
    )


def _bulk_preview_chunks(questions: list[dict]) -> list[str]:
    chunks = []
    current = "Проверьте вопросы и соответствие «ассоциация → результат»:\n\n"
    for index, question in enumerate(questions, 1):
        section = (
            f"{index}. {capitalize_first(question['word'])}\n"
            + _mapping_preview(question["options"], question["associo"])
            + "\n\n"
        )
        # Telegram measures message length in UTF-16 code units.
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
        # The message may have already been edited or deleted.
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
        "Теперь отправьте от 2 до 8 результатов (options) через запятую, каждый до 64 символов.\n"
        "Например: повар, император\n\nДля отмены — /cancel."
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

    await state.update_data(options=options)
    await state.set_state(QuestionCreationStates.waiting_for_associo)
    await message.answer(_associo_prompt(word, options), parse_mode=None)


async def receive_question_associo(message: types.Message, state: FSMContext):
    if not await _allow_message(message):
        return
    if await state.get_state() != QuestionCreationStates.waiting_for_associo.state:
        return
    try:
        if not isinstance(message.text, str):
            raise ValueError("Отправьте ассоциации текстом через запятую.")
        data = await state.get_data()
        word = _validate_word(data.get("word"))
        options = _validate_options(data.get("options"))
        associo = _validate_associo(message.text.split(","), options)
    except ValueError as error:
        await message.answer(str(error))
        return

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="✅ Добавить", callback_data=CONFIRM_CALLBACK),
        types.InlineKeyboardButton(text="❌ Отмена", callback_data=CANCEL_CALLBACK),
    ]])
    preview = await message.answer(
        f"<b>{escape(capitalize_first(word))}</b>\n\nАссоциация → результат:\n"
        + _mapping_preview(options, associo, html=True)
        + "\n\nДобавить этот вопрос?",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    await state.update_data(associo=associo, confirmation_message_id=preview.message_id)
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
        associo = _validate_associo(data.get("associo"), options)
    except ValueError:
        await state.clear()
        await _remove_keyboard(callback.message)
        await callback.message.answer(
            "Черновик неполный. Начните заново: /add_question.",
            reply_markup=await get_main_menu_for_chat(callback.message.chat.id, is_admin=True),
        )
        return

    try:
        async with async_session() as session:
            result = await session.execute(
                insert(Question)
                .values(word=word, options=",".join(options), associo=",".join(associo))
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
        reply_markup=await get_main_menu_for_chat(callback.message.chat.id, is_admin=True),
    )


async def cancel_add_question(message: types.Message, state: FSMContext):
    if not await _allow_message(message):
        return
    if await state.get_state() not in CREATION_STATES:
        return
    await _remove_draft_keyboard(message, await state.get_data())
    await state.clear()
    await message.answer(
        "Добавление вопроса отменено.",
        reply_markup=await get_main_menu_for_chat(message.chat.id, is_admin=True),
    )


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
    await callback.message.answer(
        "Добавление вопроса отменено.",
        reply_markup=await get_main_menu_for_chat(callback.message.chat.id, is_admin=True),
    )


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
        "Затем я по очереди спрошу результаты (options) и ассоциации (associo).\n\nДля отмены — /cancel.",
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
    await state.update_data(bulk_current_options=options)
    await state.set_state(BulkQuestionCreationStates.waiting_for_associo)
    await message.answer(_associo_prompt(words[index], options), parse_mode=None)


async def receive_bulk_question_associo(message: types.Message, state: FSMContext):
    if not await _allow_message(message):
        return
    if await state.get_state() != BulkQuestionCreationStates.waiting_for_associo.state:
        return
    try:
        if not isinstance(message.text, str):
            raise ValueError("Отправьте ассоциации текстом через запятую.")
        data = await state.get_data()
        words, questions, index = _validate_bulk_draft(data, complete=False)
        options = _validate_options(data.get("bulk_current_options"))
        associo = _validate_associo(message.text.split(","), options)
    except ValueError as error:
        await message.answer(str(error))
        return
    questions.append({"word": words[index], "options": options, "associo": associo})
    next_index = index + 1
    if next_index < len(words):
        await message.answer(_bulk_options_prompt(words, next_index), parse_mode=None)
        await state.update_data(bulk_questions=questions, bulk_index=next_index, bulk_current_options=None)
        await state.set_state(BulkQuestionCreationStates.waiting_for_options)
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
        bulk_current_options=None,
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
            reply_markup=await get_main_menu_for_chat(callback.message.chat.id, is_admin=True),
        )
        return

    try:
        async with async_session() as session:
            result = await session.execute(
                insert(Question)
                .values([
                    {
                        "word": question["word"], "options": ",".join(question["options"]),
                        "associo": ",".join(question["associo"]),
                    }
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
        reply_markup=await get_main_menu_for_chat(callback.message.chat.id, is_admin=True),
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
        reply_markup=await get_main_menu_for_chat(message.chat.id, is_admin=True),
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
        reply_markup=await get_main_menu_for_chat(callback.message.chat.id, is_admin=True),
    )


async def remind_bulk_confirmation(message: types.Message, state: FSMContext):
    if not await _allow_message(message):
        return
    await message.answer("Подтвердите список кнопкой «Добавить все» под черновиком или отмените: /cancel.")


async def _offer_associo_question(
    message: types.Message, state: FSMContext, *, word: str | None = None,
):
    statement = select(Question).order_by(Question.id).limit(1)
    if word is None:
        statement = statement.where(or_(Question.associo.is_(None), func.trim(Question.associo) == ""))
    else:
        statement = statement.where(Question.word == word)
    try:
        async with async_session() as session:
            question = (await session.execute(statement)).scalar_one_or_none()
    except SQLAlchemyError:
        logger.exception("Не удалось загрузить вопрос для ассоциаций")
        await state.clear()
        await message.answer(
            "Не удалось загрузить вопрос. Попробуйте еще раз: /add_associo.",
            reply_markup=await get_main_menu_for_chat(message.chat.id, is_admin=True),
        )
        return
    if question is None:
        await state.clear()
        await message.answer(
            "Вопрос с таким названием не найден. Укажите точное название: /add_associo слово."
            if word is not None else "✅ У всех вопросов уже заполнены ассоциации.",
            reply_markup=await get_main_menu_for_chat(message.chat.id, is_admin=True),
        )
        return
    try:
        options = _validate_options(question.options.split(","))
    except ValueError:
        await state.clear()
        await message.answer(
            f"У вопроса «{capitalize_first(question.word)}» некорректные результаты (options). "
            "Сначала исправьте их в базе, затем повторите /add_associo.",
            parse_mode=None,
            reply_markup=await get_main_menu_for_chat(message.chat.id, is_admin=True),
        )
        return
    await state.set_data({
        "question_id": question.id,
        "word": question.word,
        "options": options,
        "original_options": question.options,
        "original_associo": question.associo,
        "assoc_auto_next": word is None,
    })
    await state.set_state(AssocioCreationStates.waiting_for_associo)
    await message.answer(
        _associo_prompt(question.word, options),
        parse_mode=None,
        reply_markup=types.ReplyKeyboardRemove(),
    )


async def start_add_associo(message: types.Message, state: FSMContext):
    if not await _allow_message(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    word = parts[1] if len(parts) == 2 and parts[0].startswith("/") else None
    if word is not None:
        try:
            word = _validate_word(word)
        except ValueError as error:
            await message.answer(str(error))
            return
    await _remove_draft_keyboard(message, await state.get_data())
    await state.clear()
    await _offer_associo_question(message, state, word=word)


async def receive_existing_question_associo(message: types.Message, state: FSMContext):
    if not await _allow_message(message):
        return
    if await state.get_state() != AssocioCreationStates.waiting_for_associo.state:
        return
    try:
        if not isinstance(message.text, str):
            raise ValueError("Отправьте ассоциации текстом через запятую.")
        data = await state.get_data()
        word = _validate_word(data.get("word"))
        options = _validate_options(data.get("options"))
        associo = _validate_associo(message.text.split(","), options)
    except ValueError as error:
        await message.answer(str(error))
        return
    preview = await message.answer(
        f"<b>{escape(capitalize_first(word))}</b>\n\nАссоциация → результат:\n"
        + _mapping_preview(options, associo, html=True)
        + "\n\nСохранить ассоциации?",
        parse_mode="HTML",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(text="✅ Сохранить", callback_data=ASSOCIO_CONFIRM_CALLBACK),
            types.InlineKeyboardButton(text="❌ Отмена", callback_data=ASSOCIO_CANCEL_CALLBACK),
        ]]),
    )
    await state.update_data(associo=associo, confirmation_message_id=preview.message_id)
    await state.set_state(AssocioCreationStates.waiting_for_confirmation)


async def confirm_add_associo(callback: types.CallbackQuery, state: FSMContext):
    if not await _allow_callback(callback):
        return
    if callback.data != ASSOCIO_CONFIRM_CALLBACK:
        await callback.answer("Неизвестное действие.")
        return
    data = await _active_callback_draft(
        callback, state, AssocioCreationStates.waiting_for_confirmation.state, "/add_associo",
    )
    if data is None:
        return
    await callback.answer()
    try:
        word = _validate_word(data.get("word"))
        options = _validate_options(data.get("options"))
        associo = _validate_associo(data.get("associo"), options)
        question_id = data.get("question_id")
        original_options = data.get("original_options")
        original_associo = data.get("original_associo")
        auto_next = data.get("assoc_auto_next")
        if (
            type(question_id) is not int or question_id <= 0
            or not isinstance(original_options, str)
            or _validate_options(original_options.split(",")) != options
            or not (original_associo is None or isinstance(original_associo, str))
            or type(auto_next) is not bool
        ):
            raise ValueError("Неполный черновик")
    except ValueError:
        await state.clear()
        await _remove_keyboard(callback.message)
        await callback.message.answer(
            "Черновик неполный. Начните заново: /add_associo.",
            reply_markup=await get_main_menu_for_chat(callback.message.chat.id, is_admin=True),
        )
        return
    try:
        async with async_session() as session:
            result = await session.execute(
                update(Question)
                .where(
                    Question.id == question_id,
                    Question.word == word,
                    Question.options == original_options,
                    Question.associo == original_associo,
                )
                .values(associo=",".join(associo))
                .returning(Question.id)
            )
            saved_id = result.scalar_one_or_none()
            await session.commit()
    except SQLAlchemyError:
        logger.exception("Не удалось сохранить ассоциации")
        await callback.message.answer(
            "Не удалось сохранить ассоциации. Нажмите «Сохранить» еще раз или отмените: /cancel."
        )
        return
    await state.clear()
    await _remove_keyboard(callback.message)
    if saved_id is None:
        await callback.message.answer(
            "Вопрос уже изменен или удален. Загрузите его заново: /add_associo.",
            reply_markup=await get_main_menu_for_chat(callback.message.chat.id, is_admin=True),
        )
        return
    await callback.message.answer(
        f"✅ Ассоциации для «{capitalize_first(word)}» сохранены.",
        parse_mode=None,
        **({} if auto_next else {
            "reply_markup": await get_main_menu_for_chat(callback.message.chat.id, is_admin=True),
        }),
    )
    if auto_next:
        await _offer_associo_question(callback.message, state)


async def cancel_add_associo(message: types.Message, state: FSMContext):
    if not await _allow_message(message):
        return
    if await state.get_state() not in ASSOCIO_CREATION_STATES:
        return
    await _remove_draft_keyboard(message, await state.get_data())
    await state.clear()
    await message.answer(
        "Добавление ассоциаций отменено. Ранее подтвержденные ассоциации сохранены.",
        reply_markup=await get_main_menu_for_chat(message.chat.id, is_admin=True),
    )


async def cancel_associo_callback(callback: types.CallbackQuery, state: FSMContext):
    if not await _allow_callback(callback):
        return
    if callback.data != ASSOCIO_CANCEL_CALLBACK:
        await callback.answer("Неизвестное действие.")
        return
    if await _active_callback_draft(
        callback, state, AssocioCreationStates.waiting_for_confirmation.state, "/add_associo",
    ) is None:
        return
    await callback.answer()
    await state.clear()
    await _remove_keyboard(callback.message)
    await callback.message.answer(
        "Добавление ассоциаций отменено. Ранее подтвержденные ассоциации сохранены.",
        reply_markup=await get_main_menu_for_chat(callback.message.chat.id, is_admin=True),
    )


async def remind_associo_confirmation(message: types.Message, state: FSMContext):
    if not await _allow_message(message):
        return
    await message.answer("Подтвердите ассоциации кнопкой «Сохранить» под черновиком или отмените: /cancel.")


def register_handlers_admin(dp: Dispatcher):
    dp.message.register(show_bot_statistics, Command("bot_statistics"))
    dp.message.register(show_bot_statistics, F.text.in_({BOT_STATISTICS_BUTTON, "Статистика бота"}))
    dp.message.register(start_add_question, Command("add_question"))
    dp.message.register(start_add_question, F.text == ADD_QUESTION_BUTTON)
    dp.message.register(start_add_questions, Command("add_questions"))
    dp.message.register(start_add_questions, F.text == ADD_QUESTIONS_BUTTON)
    dp.message.register(start_add_associo, Command("add_associo"))
    dp.message.register(start_add_associo, F.text == ADD_ASSOCIO_BUTTON)
    dp.message.register(cancel_add_question, Command("cancel"), StateFilter(QuestionCreationStates))
    dp.message.register(cancel_add_questions, Command("cancel"), StateFilter(BulkQuestionCreationStates))
    dp.message.register(cancel_add_associo, Command("cancel"), StateFilter(AssocioCreationStates))
    dp.message.register(receive_question_word, StateFilter(QuestionCreationStates.waiting_for_word))
    dp.message.register(receive_question_options, StateFilter(QuestionCreationStates.waiting_for_options))
    dp.message.register(receive_question_associo, StateFilter(QuestionCreationStates.waiting_for_associo))
    dp.message.register(remind_question_confirmation, StateFilter(QuestionCreationStates.waiting_for_confirmation))
    dp.message.register(receive_question_words, StateFilter(BulkQuestionCreationStates.waiting_for_words))
    dp.message.register(receive_bulk_question_options, StateFilter(BulkQuestionCreationStates.waiting_for_options))
    dp.message.register(receive_bulk_question_associo, StateFilter(BulkQuestionCreationStates.waiting_for_associo))
    dp.message.register(remind_bulk_confirmation, StateFilter(BulkQuestionCreationStates.waiting_for_confirmation))
    dp.message.register(receive_existing_question_associo, StateFilter(AssocioCreationStates.waiting_for_associo))
    dp.message.register(remind_associo_confirmation, StateFilter(AssocioCreationStates.waiting_for_confirmation))
    dp.callback_query.register(confirm_add_question, F.data == CONFIRM_CALLBACK)
    dp.callback_query.register(cancel_question_callback, F.data == CANCEL_CALLBACK)
    dp.callback_query.register(confirm_add_questions, F.data == BULK_CONFIRM_CALLBACK)
    dp.callback_query.register(cancel_bulk_callback, F.data == BULK_CANCEL_CALLBACK)
    dp.callback_query.register(confirm_add_associo, F.data == ASSOCIO_CONFIRM_CALLBACK)
    dp.callback_query.register(cancel_associo_callback, F.data == ASSOCIO_CANCEL_CALLBACK)
