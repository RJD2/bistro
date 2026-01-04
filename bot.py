import asyncio
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
import config
from handlers.start import register_handlers_start
from handlers.game import register_handlers_game

bot = Bot(token=config.TOKEN)
dp = Dispatcher(storage=MemoryStorage())

register_handlers_start(dp)
register_handlers_game(dp)

async def main():
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
