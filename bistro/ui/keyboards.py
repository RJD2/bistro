from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

from bistro.ui.text_formatting import capitalize_first


AUTOTEST_OFF_BUTTON = "⏰ Автотест"
AUTOTEST_ON_BUTTON = "🚫 Автотест"
BOT_STATISTICS_BUTTON = "📈 Статистика бота"


def get_keyboard(options: list, qid: int, *, callback_prefix: str = "answer"):
    buttons = [
        InlineKeyboardButton(text=capitalize_first(opt), callback_data=f"{callback_prefix}:{qid}:{i}")
        for i, opt in enumerate(options)
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[[button] for button in buttons])
    return kb


def get_main_menu(is_admin: bool = False, *, autotest_enabled: bool = False) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="🎮 Быстротест")],
        [KeyboardButton(text=AUTOTEST_ON_BUTTON if autotest_enabled else AUTOTEST_OFF_BUTTON)],
        [KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="🔄 Сбросить прогресс")]
    ]
    if is_admin:
        buttons.append([KeyboardButton(text=BOT_STATISTICS_BUTTON)])
        buttons.append([KeyboardButton(text="➕ Добавить вопрос")])
        buttons.append([KeyboardButton(text="📚 Добавить вопросы")])
        buttons.append([KeyboardButton(text="🧩 Добавить ассоциации")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


main_menu = get_main_menu()
