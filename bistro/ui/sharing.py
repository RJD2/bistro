"""Share an ordinary message with a result and a link to the same question."""

from urllib.parse import quote, urlencode

from aiogram import Bot, types


async def get_result_keyboard(
    bot: Bot, selected_choice: str, question_id: int,
) -> types.InlineKeyboardMarkup:
    identity = await bot.me()
    draft = (
        f"Оказывается я {selected_choice}, узнай кто ты в Быстротесте\n"
        f"https://t.me/{identity.username}?start=question_{question_id}"
    )
    # Clients insert `url` as the start of the draft. Keep the invitation in
    # that field so the actual bot link follows the result. iOS needs %20,
    # rather than '+', for spaces in this parameter.
    share_url = "https://t.me/share/url?" + urlencode({"url": draft}, quote_via=quote)
    return types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="📤 Поделиться", url=share_url),
    ]])
