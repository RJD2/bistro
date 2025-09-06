from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

def get_keyboard(options: list, qid: int):
    kb = InlineKeyboardMarkup(row_width=len(options))
    buttons = [
        InlineKeyboardButton(opt.title(), callback_data=f"answer:{qid}:{i}")
        for i, opt in enumerate(options)
    ]
    kb.add(*buttons)
    return kb


main_menu = ReplyKeyboardMarkup(resize_keyboard=True)
main_menu.add(KeyboardButton("🎮 Быстротест"))
