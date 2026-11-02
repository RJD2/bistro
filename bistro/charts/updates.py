"""Refresh group game results only within the chat where an answer arrived"""

from asyncio import Lock, Task, create_task, gather, sleep, to_thread
from contextlib import asynccontextmanager
from hashlib import sha256
import json
import logging
from weakref import WeakValueDictionary

from aiogram import Bot, types
from aiogram.exceptions import (
    TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter,
)

from bistro.charts import messages as chart_messages
from bistro.database.session import async_session
from bistro.charts.rendering import render_statistics_chart
from bistro.ui.telegram_ephemeral import EditEphemeralMessageMedia


logger = logging.getLogger(__name__)
_locks: WeakValueDictionary[tuple, Lock] = WeakValueDictionary()
_scheduled_retries: dict[tuple[int, int, int], Task] = {}
_retry_tasks: set[Task] = set()


def _schedule_retry(bot: Bot, question_id: int, get_statistics, delay: float, *, chat_id: int) -> None:
    key = (bot.id, chat_id, question_id)
    if key in _scheduled_retries:
        return

    async def retry():
        try:
            await sleep(max(1, delay))
            _scheduled_retries.pop(key, None)
            await refresh_question_charts(bot, question_id, get_statistics, chat_id=chat_id)
        except Exception as error:
            # Keep references in the database for a later answer or retry.
            logger.warning("Не удалось повторить обновление диаграмм: %s", type(error).__name__)

    task = create_task(retry())
    _scheduled_retries[key] = task
    _retry_tasks.add(task)
    task.add_done_callback(_retry_tasks.discard)


async def stop_chart_updates() -> None:
    tasks = list(_retry_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await gather(*tasks, return_exceptions=True)
    _scheduled_retries.clear()
    _retry_tasks.clear()


@asynccontextmanager
async def _guard(key: tuple):
    lock = _locks.setdefault(key, Lock())
    async with lock:
        yield


def recipient_guard(bot_id: int, chat_id: int, receiver_user_id: int):
    # Sending a new ephemeral and forgetting a reused ID must be atomic with
    # respect to edits of that recipient's previously registered charts.
    return _guard(("recipient", bot_id, chat_id, receiver_user_id))


def statistics_key(statistics: dict[str, float]) -> str:
    payload = json.dumps(list(statistics.items()), ensure_ascii=False, separators=(",", ":"))
    return sha256(payload.encode()).hexdigest()


def _unavailable(error: TelegramBadRequest) -> bool:
    message = error.message.casefold()
    return any(reason in message for reason in (
        "message to edit not found", "message not found", "message can't be edited",
        "message can not be edited", "message_id_invalid", "message identifier is not specified",
        "ephemeral_message_not_found", "ephemeral message not found", "ephemeral message expired",
        "ephemeral_message_id_invalid",
        "chat not found", "user not found",
    ))


def _is_live_target(target, bot_id: int, question_id: int, chat_id: int) -> bool:
    return (
        target is not None and target.bot_id == bot_id and target.question_id == question_id
        and target.chat_id == chat_id and chat_id < 0 and getattr(target, "live_updates", False)
        and target.receiver_user_id > 0 and target.ephemeral_message_id > 0
        and target.message_id == 0
    )


async def refresh_question_charts(
    bot: Bot,
    question_id: int,
    get_statistics,
    *,
    chat_id: int,
    statistics: dict[str, float] | None = None,
    chart: bytes | None = None,
    selected_choice: str | None = None,
) -> None:
    if type(chat_id) is not int or chat_id >= 0:
        return
    async with _guard(("question", bot.id, chat_id, question_id)):
        targets = [
            target for target in await chart_messages.list_charts(bot.id, question_id, chat_id=chat_id)
            if _is_live_target(target, bot.id, question_id, chat_id)
        ]
        if not targets:
            return
        # Read after acquiring the lock, so a delayed callback cannot roll the
        # graph back to percentages captured before a more recent answer.
        async with async_session() as session:
            current_statistics = await get_statistics(question_id, session)
        current_key = statistics_key(current_statistics)
        if all(target.statistics_key == current_key for target in targets):
            return
        # Only reuse a supplied image for the recipient choice it was drawn for.
        # Other players may have the same percentages but a different highlight.
        charts: dict[str | None, bytes] = {}
        if chart is not None and statistics is not None and statistics_key(statistics) == current_key:
            charts[selected_choice] = chart

        for snapshot in targets:
            async with recipient_guard(bot.id, snapshot.chat_id, snapshot.receiver_user_id):
                # The ID may have been reused for a new question while waiting.
                target = await chart_messages.get_chart(snapshot.id)
                if (
                    not _is_live_target(target, bot.id, question_id, chat_id)
                    or target.statistics_key == current_key
                ):
                    continue
                choice = getattr(target, "selected_choice", None)
                if choice not in charts:
                    charts[choice] = await to_thread(
                        render_statistics_chart, current_statistics, selected_choice=choice,
                    )
                media = types.InputMediaPhoto(
                    media=types.BufferedInputFile(charts[choice], filename=f"statistics_{question_id}.png"),
                    caption=target.caption,
                    parse_mode=target.parse_mode,
                )
                reply_markup = (
                    types.InlineKeyboardMarkup.model_validate_json(target.reply_markup)
                    if target.reply_markup else None
                )
                try:
                    await bot(EditEphemeralMessageMedia(
                        chat_id=target.chat_id,
                        receiver_user_id=target.receiver_user_id,
                        ephemeral_message_id=target.ephemeral_message_id,
                        media=media,
                        reply_markup=reply_markup,
                    ))
                except TelegramBadRequest as error:
                    if "message is not modified" in error.message.casefold():
                        await chart_messages.mark_chart_updated(target, current_key)
                    elif _unavailable(error):
                        await chart_messages.remove_chart(target)
                    else:
                        logger.warning("Не удалось обновить диаграмму %s: TelegramBadRequest", target.id)
                except TelegramForbiddenError:
                    await chart_messages.remove_chart(target)
                except TelegramRetryAfter as error:
                    # Coalesce retries and read fresh statistics when the delay
                    # ends, including when this was the final vote in the game.
                    _schedule_retry(bot, question_id, get_statistics, error.retry_after, chat_id=chat_id)
                    logger.warning("Telegram ограничил частоту обновления диаграмм")
                    break
                except (TelegramAPIError, OSError, TimeoutError) as error:
                    _schedule_retry(bot, question_id, get_statistics, 5, chat_id=chat_id)
                    logger.warning("Не удалось обновить диаграмму %s: %s", target.id, type(error).__name__)
                else:
                    await chart_messages.mark_chart_updated(target, current_key)
