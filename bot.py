from aiogram import Bot, Dispatcher, executor
from aiogram.contrib.fsm_storage.memory import MemoryStorage
import config
from handlers.start import register_handlers_start
from handlers.game import register_handlers_game

bot = Bot(token=config.TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())

register_handlers_start(dp)
register_handlers_game(dp)

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
