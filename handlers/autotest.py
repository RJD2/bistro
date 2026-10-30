import logging

from aiogram import Dispatcher, F, types
from aiogram.enums import ChatType
from aiogram.filters import Command
from sqlalchemy.exc import SQLAlchemyError

from autotest import toggle_group_subscription, toggle_subscription
from handlers.start import is_owner_chat
from keyboards import AUTOTEST_OFF_BUTTON, AUTOTEST_ON_BUTTON, get_main_menu


logger = logging.getLogger(__name__)
AUTOTEST_ENABLED = "Вы будете получать новый Быстротест каждый день"
AUTOTEST_DISABLED = "Автотест отключён. Вы больше не будете получать ежедневные Быстротесты."
GROUP_AUTOTEST_ENABLED = "Автотест включён для группы. Новый Быстротест будет приходить сюда каждый день."
GROUP_AUTOTEST_DISABLED = "Автотест отключён для группы. Ежедневная отправка остановлена."


async def toggle_autotest(message: types.Message):
    if message.from_user is None:
        return
    is_group = message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}
    if message.chat.type != ChatType.PRIVATE and not is_group:
        return

    try:
        if is_group:
            enabled = await toggle_group_subscription(message.chat.id)
        else:
            enabled = await toggle_subscription(message.from_user.id)
    except SQLAlchemyError:
        logger.exception("Не удалось изменить подписку на Автотест")
        await message.answer("Не удалось изменить настройку Автотеста. Попробуйте ещё раз позже.")
        return

    if is_group:
        text = GROUP_AUTOTEST_ENABLED if enabled else GROUP_AUTOTEST_DISABLED
    else:
        text = AUTOTEST_ENABLED if enabled else AUTOTEST_DISABLED
    await message.answer(
        text,
        reply_markup=get_main_menu(is_admin=is_owner_chat(message), autotest_enabled=enabled),
    )


def register_handlers_autotest(dp: Dispatcher):
    dp.message.register(toggle_autotest, Command("autotest"))
    dp.message.register(toggle_autotest, F.text.in_({"Автотест", AUTOTEST_OFF_BUTTON, AUTOTEST_ON_BUTTON}))
