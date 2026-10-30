from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

from text_formatting import capitalize_first


def get_keyboard(options: list, qid: int, *, callback_prefix: str = "answer"):
    buttons = [
        InlineKeyboardButton(text=capitalize_first(opt), callback_data=f"{callback_prefix}:{qid}:{i}")
        for i, opt in enumerate(options)
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[[button] for button in buttons])
    return kb


def get_main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="🎮 Быстротест")],
        [KeyboardButton(text="Автотест")],
        [KeyboardButton(text="🔄 Сбросить прогресс")]
    ]
    if is_admin:
        buttons.append([KeyboardButton(text="➕ Добавить вопрос")])
        buttons.append([KeyboardButton(text="📚 Добавить вопросы")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


main_menu = get_main_menu()
