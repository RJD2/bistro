from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton


def get_keyboard(options: list, qid: int):
    buttons = [
        InlineKeyboardButton(text=opt.title(), callback_data=f"answer:{qid}:{i}")
        for i, opt in enumerate(options)
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[buttons])
    return kb


main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎮 Быстротест")],
        [KeyboardButton(text="🔄 Сбросить прогресс")]
    ],
    resize_keyboard=True
)
