"""Keep previously sent inline share buttons working with link previews."""

from html import escape
import re

from aiogram import Dispatcher, types
from sqlalchemy import select

from bistro.database.models import Answer, Question, User
from bistro.database.session import async_session


async def share_result(inline: types.InlineQuery):
    results = []
    match = re.fullmatch(r"share:([0-9]{1,10})", inline.query)
    question_id = int(match[1]) if match is not None else 0
    if 0 < question_id <= 2**31 - 1:
        async with async_session() as session:
            result = await session.execute(
                select(Question, Answer.choice)
                .join(Answer, Answer.question_id == Question.id)
                .join(User, User.id == Answer.user_id)
                .where(User.tg_id == inline.from_user.id, Question.id == question_id)
                .order_by(Answer.id).limit(1)
            )
            saved = result.first()
        if saved is not None:
            question, choice = saved
            identity = await inline.bot.me()
            bot_link = f"https://t.me/{identity.username}?start=question_{question.id}"
            text = (
                f"Оказывается я {escape(choice)}, узнай кто ты в "
                f'<a href="{escape(bot_link, quote=True)}">Быстротесте</a>'
            )
            results.append(types.InlineQueryResultArticle(
                id=f"share:{inline.from_user.id}:{question.id}",
                title="Поделиться результатом",
                description=f"Оказывается я {choice}, узнай кто ты в Быстротесте",
                input_message_content=types.InputTextMessageContent(
                    message_text=text,
                    parse_mode="HTML",
                    link_preview_options=types.LinkPreviewOptions(url=bot_link, is_disabled=False),
                ),
            ))
    # Inline queries are editable and results are specific to their sender.
    # Never trust a choice/user ID from the query or reuse another user's cache.
    await inline.answer(results=results, cache_time=0, is_personal=True)


def register_handlers_sharing(dp: Dispatcher):
    dp.inline_query.register(share_result)
