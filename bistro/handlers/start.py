import re

from aiogram import types, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from bistro.handlers.game import start_shared_game
from bistro.ui.messages import MESSAGES
from bistro import config
from bistro.ui.menus import get_main_menu_for_chat


def is_owner_chat(message: types.Message) -> bool:
    return (
        config.ADMIN_ID > 0
        and message.from_user is not None
        and message.from_user.id == config.ADMIN_ID
        and message.chat.type == "private"
    )


def _shared_question_id(payload: str | None) -> int | None:
    match = re.fullmatch(r"question_([0-9]{1,10})", payload or "")
    if match is not None:
        question_id = int(match[1])
        if 0 < question_id <= 2**31 - 1:
            return question_id
    return None


async def cmd_start(
    message: types.Message, state: FSMContext | None = None, command: CommandObject | None = None,
):
    question_id = _shared_question_id(command.args if command is not None else None)
    if message.chat.type == "private" and question_id is not None and state is not None:
        await start_shared_game(message, state, question_id)
        return
    await message.answer(
        MESSAGES["start"],
        reply_markup=await get_main_menu_for_chat(message.chat.id, is_admin=is_owner_chat(message)),
    )


async def cmd_stop(message: types.Message):
    if message.chat.type not in {"private", "group", "supergroup"}:
        return
    await message.answer(
        "Клавиатура скрыта. Чтобы вернуть меню, отправьте /start.",
        reply_markup=types.ReplyKeyboardRemove(),
    )


async def cmd_help(message: types.Message):
    text = MESSAGES["help"] + "\n\n/play — начать или продолжить тест\n/reset — сбросить свой прогресс"
    text += "\n/start — показать меню\n/stop — скрыть нижнюю клавиатуру"
    text += "\n/autotest или «Автотест» — включить или отключить ежедневный Быстротест"
    text += "\n/statistics или «📊 Статистика» — отвеченные слова, графики и ваш выбор"
    if message.chat.type in {"group", "supergroup"}:
        text += (
            "\n\nНа вопросы может отвечать каждый. Результат виден только вам."
            " Чтобы получить следующий общий вопрос, снова нажмите «🎮 Быстротест» или отправьте /play."
            " Автотест присылает один общий вопрос в день для всей группы; ответы и результаты персональные."
        )
    if is_owner_chat(message):
        text += (
            "\n\n/add_question — добавить вопрос"
            "\n/add_questions — добавить вопросы списком"
            "\n/add_associo [слово] — заполнить ассоциации к существующим вопросам"
            "\n/cancel — отменить добавление"
        )
    await message.answer(text)

def register_handlers_start(dp: Dispatcher):
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_stop, Command("stop"))
    dp.message.register(cmd_help, Command("help"))
