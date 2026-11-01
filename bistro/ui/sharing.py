"""Open Telegram's recipient chooser for a formatted inline result."""

from aiogram import types


def get_result_keyboard(question_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(
            text="📤 Поделиться",
            switch_inline_query_chosen_chat=types.SwitchInlineQueryChosenChat(
                query=f"share:{question_id}",
                allow_user_chats=True,
                allow_group_chats=True,
                allow_channel_chats=True,
            ),
        ),
    ]])
