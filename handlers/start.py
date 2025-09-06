from aiogram import types, Dispatcher
from messages import MESSAGES
import keyboards as kb

async def cmd_start(message: types.Message):
    await message.answer(
        MESSAGES["start"],
        reply_markup=kb.main_menu
    )

async def cmd_help(message: types.Message):
    await message.answer(MESSAGES["help"])

def register_handlers_start(dp: Dispatcher):
    dp.register_message_handler(cmd_start, commands="start", state="*")
    dp.register_message_handler(cmd_help, commands="help", state="*")
