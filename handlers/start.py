from aiogram import types, Dispatcher
from aiogram.filters import Command
from messages import MESSAGES
import config
import keyboards as kb


def is_owner_chat(message: types.Message) -> bool:
    return (
        config.ADMIN_ID > 0
        and message.from_user is not None
        and message.from_user.id == config.ADMIN_ID
        and message.chat.type == "private"
    )


async def cmd_start(message: types.Message):
    await message.answer(
        MESSAGES["start"],
        reply_markup=kb.get_main_menu(is_admin=is_owner_chat(message)),
    )

async def cmd_help(message: types.Message):
    text = MESSAGES["help"]
    if is_owner_chat(message):
        text += (
            "\n\n/add_question — добавить вопрос"
            "\n/add_questions — добавить вопросы списком"
            "\n/cancel — отменить добавление"
        )
    await message.answer(text)

def register_handlers_start(dp: Dispatcher):
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_help, Command("help"))
