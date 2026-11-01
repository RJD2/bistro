import asyncio
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
import config
from autotest import start_autotests, stop_autotests
from chart_updates import stop_chart_updates
from handlers.admin import register_handlers_admin
from handlers.autotest import register_handlers_autotest
from handlers.start import register_handlers_start
from handlers.game import register_handlers_game
from handlers.statistics import register_handlers_statistics

bot = Bot(token=config.TOKEN)
dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
dp.startup.register(start_autotests)
dp.shutdown.register(stop_autotests)
dp.shutdown.register(stop_chart_updates)

register_handlers_autotest(dp)
register_handlers_statistics(dp)
# Menu controls must work before the question wizards' catch-all handlers.
register_handlers_start(dp)
register_handlers_admin(dp)
register_handlers_game(dp)

async def main():
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
