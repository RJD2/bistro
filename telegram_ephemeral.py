"""Bot API 10.3 methods not yet included in the pinned aiogram version.

SendMessage/SendPhoto already forward ephemeral_message_parameters via **kwargs.
https://core.telegram.org/bots/api#ephemeralmessageparameters
"""

from aiogram.methods import TelegramMethod
from aiogram.types import InlineKeyboardMarkup, InputMediaPhoto


class EditEphemeralMessageMedia(TelegramMethod[bool]):
    __returning__ = bool
    __api_method__ = "editEphemeralMessageMedia"

    chat_id: int | str
    receiver_user_id: int
    ephemeral_message_id: int
    media: InputMediaPhoto
    reply_markup: InlineKeyboardMarkup | None = None


class EditEphemeralMessageText(TelegramMethod[bool]):
    __returning__ = bool
    __api_method__ = "editEphemeralMessageText"

    chat_id: int | str
    receiver_user_id: int
    ephemeral_message_id: int
    text: str
    parse_mode: str | None = None
    reply_markup: InlineKeyboardMarkup | None = None
