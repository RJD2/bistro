"""Open Telegram's recipient chooser with a personal result and bot invitation."""

from urllib.parse import quote, urlencode

from aiogram import Bot, types


async def get_result_keyboard(
    bot: Bot, selected_choice: str, question_id: int,
) -> types.InlineKeyboardMarkup:
    identity = await bot.me()
    text = (
        f"Оказывается я {selected_choice}, узнай кто ты в Быстроботе\n"
        f"https://t.me/{identity.username}?start=question_{question_id}"
    )
    # Telegram puts `url` before `text` in the shared draft. Keep the complete
    # invitation together so the bot link stays after the personal result.
    # Encode spaces as %20: iOS does not convert '+' to spaces in this field.
    share_url = "https://t.me/share/url?" + urlencode({"url": text}, quote_via=quote)
    return types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="📤 Поделиться", url=share_url),
    ]])
