"""Persist displayed chart targets so later answers can refresh their media

Ephemeral identifiers can be reused by Telegram. Every newly sent ephemeral
message must replace or forget its old registration, including non-chart replies
"""

from aiogram import types
from aiogram.enums import ChatType
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from bistro.database.session import async_session
from bistro.database.models import ChartMessage


_TARGET_FIELDS = (
    "bot_id", "chat_id", "receiver_user_id", "message_id", "ephemeral_message_id",
)


def _message_target(
    bot_id: int, message: types.Message, receiver_user_id: int,
) -> dict[str, int] | None:
    if not isinstance(message, types.Message):
        return None
    chat_id = message.chat.id
    if type(bot_id) is not int or bot_id <= 0 or type(chat_id) is not int or chat_id == 0:
        return None

    ephemeral_id = getattr(message, "ephemeral_message_id", None)
    if isinstance(ephemeral_id, int):
        if type(ephemeral_id) is not int or ephemeral_id <= 0:
            return None
        if type(receiver_user_id) is not int or receiver_user_id <= 0:
            return None
        message_id = 0
    else:
        message_id = message.message_id
        if type(message_id) is not int or message_id <= 0:
            return None
        ephemeral_id = 0
        receiver_user_id = 0

    return {
        "bot_id": bot_id,
        "chat_id": chat_id,
        "receiver_user_id": receiver_user_id,
        "message_id": message_id,
        "ephemeral_message_id": ephemeral_id,
    }


async def remember_chart(
    bot_id: int,
    question_id: int,
    message: types.Message,
    receiver_user_id: int,
    caption: str | None,
    parse_mode: str | None,
    statistics_key: str,
    reply_markup: types.InlineKeyboardMarkup | None = None,
    selected_choice: str | None = None,
    live_updates: bool = False,
) -> None:
    target = _message_target(bot_id, message, receiver_user_id)
    if target is None:
        return
    live_updates = bool(
        live_updates and message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}
        and target["chat_id"] < 0 and target["receiver_user_id"] > 0
        and target["ephemeral_message_id"] > 0
    )
    statement = insert(ChartMessage).values(
        **target,
        question_id=question_id,
        caption=caption,
        parse_mode=parse_mode,
        reply_markup=reply_markup.model_dump_json(exclude_none=True) if reply_markup else None,
        statistics_key=statistics_key,
        selected_choice=selected_choice,
        live_updates=live_updates,
    )
    statement = statement.on_conflict_do_update(
        index_elements=list(_TARGET_FIELDS),
        set_={
            "question_id": statement.excluded.question_id,
            "caption": statement.excluded.caption,
            "parse_mode": statement.excluded.parse_mode,
            "reply_markup": statement.excluded.reply_markup,
            "statistics_key": statement.excluded.statistics_key,
            "selected_choice": statement.excluded.selected_choice,
            "live_updates": statement.excluded.live_updates,
        },
    )
    async with async_session() as session:
        await session.execute(statement)
        await session.commit()


async def forget_message(bot_id: int, message: types.Message, receiver_user_id: int) -> None:
    target = _message_target(bot_id, message, receiver_user_id)
    if target is None:
        return
    statement = delete(ChartMessage).where(
        *(getattr(ChartMessage, name) == value for name, value in target.items())
    )
    async with async_session() as session:
        await session.execute(statement)
        await session.commit()


async def list_charts(bot_id: int, question_id: int, *, chat_id: int) -> list[ChartMessage]:
    async with async_session() as session:
        result = await session.execute(
            select(ChartMessage)
            .where(
                ChartMessage.bot_id == bot_id, ChartMessage.question_id == question_id,
                ChartMessage.chat_id == chat_id, ChartMessage.chat_id < 0,
                ChartMessage.live_updates.is_(True), ChartMessage.receiver_user_id > 0,
                ChartMessage.ephemeral_message_id > 0, ChartMessage.message_id == 0,
            )
            .order_by(ChartMessage.id)
        )
        return list(result.scalars().all())


async def get_chart(chart_id: int) -> ChartMessage | None:
    async with async_session() as session:
        return await session.get(ChartMessage, chart_id)


def _snapshot_conditions(chart: ChartMessage) -> tuple:
    # Do not mutate a registration replaced since it was loaded for fanout.
    return (
        ChartMessage.id == chart.id,
        ChartMessage.question_id == chart.question_id,
        ChartMessage.statistics_key == chart.statistics_key,
        ChartMessage.caption == chart.caption,
        ChartMessage.parse_mode == chart.parse_mode,
        ChartMessage.reply_markup == chart.reply_markup,
        ChartMessage.selected_choice == chart.selected_choice,
        ChartMessage.live_updates == chart.live_updates,
    )


async def remove_chart(chart: ChartMessage) -> None:
    async with async_session() as session:
        await session.execute(delete(ChartMessage).where(*_snapshot_conditions(chart)))
        await session.commit()


async def mark_chart_updated(chart: ChartMessage, statistics_key: str) -> None:
    async with async_session() as session:
        await session.execute(
            update(ChartMessage)
            .where(*_snapshot_conditions(chart))
            .values(statistics_key=statistics_key)
        )
        await session.commit()
