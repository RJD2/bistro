from aiogram import types, Dispatcher
from aiogram.filters import Command
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
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_help, Command("help"))
