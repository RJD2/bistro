"""Restore the main menu using the chat's persisted Autotest setting"""

from aiogram.types import ReplyKeyboardMarkup
from sqlalchemy import select

from bistro.database.session import async_session
from bistro.ui.keyboards import get_main_menu
from bistro.database.models import AutotestSubscription, GroupAutotestSubscription, User


async def get_main_menu_for_chat(chat_id: int, *, is_admin: bool = False) -> ReplyKeyboardMarkup:
    if chat_id < 0:
        subscription = select(GroupAutotestSubscription.chat_id).where(
            GroupAutotestSubscription.chat_id == chat_id,
            GroupAutotestSubscription.next_send_at.is_not(None),
        )
    else:
        # In private chats chat_id is the user's Telegram ID, including on
        # callbacks where message.from_user identifies the bot instead.
        subscription = select(AutotestSubscription.user_id).join(User).where(
            User.tg_id == chat_id,
            AutotestSubscription.next_send_at.is_not(None),
        )
    async with async_session() as session:
        result = await session.execute(select(subscription.exists()))
        enabled = result.scalar_one()
    return get_main_menu(is_admin=is_admin, autotest_enabled=enabled)
