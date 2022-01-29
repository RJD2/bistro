from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, \
    InlineKeyboardMarkup

button_round = KeyboardButton('Быстротест')
round_kb = ReplyKeyboardMarkup(resize_keyboard=True).add(button_round)


def get_keyboard(question):
    option1 = InlineKeyboardButton(question['option1'].title(), callback_data='button1')
    option2 = InlineKeyboardButton(question['option2'].title(), callback_data='button2')
    return InlineKeyboardMarkup().row(option1, option2)
