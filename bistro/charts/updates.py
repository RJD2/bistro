"""Refresh displayed statistics, preserving personal captions and keyboards"""

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
_scheduled_retries: dict[tuple[int, int], Task] = {}
_retry_tasks: set[Task] = set()


def _schedule_retry(bot: Bot, question_id: int, get_statistics, delay: float) -> None:
    key = (bot.id, question_id)
    if key in _scheduled_retries:
        return

    async def retry():
        try:
            await sleep(max(1, delay))
            _scheduled_retries.pop(key, None)
            await refresh_question_charts(bot, question_id, get_statistics)
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


async def refresh_question_charts(
    bot: Bot,
    question_id: int,
    get_statistics,
    *,
    statistics: dict[str, float] | None = None,
    chart: bytes | None = None,
) -> None:
    async with _guard(("question", bot.id, question_id)):
        targets = await chart_messages.list_charts(bot.id, question_id)
        if not targets:
            return
        # Read after acquiring the lock, so a delayed callback cannot roll the
        # graph back to percentages captured before a more recent answer.
        async with async_session() as session:
            current_statistics = await get_statistics(question_id, session)
        current_key = statistics_key(current_statistics)
        if all(target.statistics_key == current_key for target in targets):
            return
        if chart is None or statistics is None or statistics_key(statistics) != current_key:
            chart = await to_thread(render_statistics_chart, current_statistics)

        for snapshot in targets:
            async with recipient_guard(bot.id, snapshot.chat_id, snapshot.receiver_user_id):
                # The ID may have been reused for a new question while waiting.
                target = await chart_messages.get_chart(snapshot.id)
                if (
                    target is None or target.bot_id != bot.id
                    or target.question_id != question_id or target.statistics_key == current_key
                ):
                    continue
                media = types.InputMediaPhoto(
                    media=types.BufferedInputFile(chart, filename=f"statistics_{question_id}.png"),
                    caption=target.caption,
                    parse_mode=target.parse_mode,
                )
                reply_markup = (
                    types.InlineKeyboardMarkup.model_validate_json(target.reply_markup)
                    if target.reply_markup else None
                )
                try:
                    if target.ephemeral_message_id:
                        await bot(EditEphemeralMessageMedia(
                            chat_id=target.chat_id,
                            receiver_user_id=target.receiver_user_id,
                            ephemeral_message_id=target.ephemeral_message_id,
                            media=media,
                            reply_markup=reply_markup,
                        ))
                    else:
                        await bot.edit_message_media(
                            chat_id=target.chat_id, message_id=target.message_id, media=media,
                            reply_markup=reply_markup,
                        )
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
                    _schedule_retry(bot, question_id, get_statistics, error.retry_after)
                    logger.warning("Telegram ограничил частоту обновления диаграмм")
                    break
                except (TelegramAPIError, OSError, TimeoutError) as error:
                    _schedule_retry(bot, question_id, get_statistics, 5)
                    logger.warning("Не удалось обновить диаграмму %s: %s", target.id, type(error).__name__)
                else:
                    await chart_messages.mark_chart_updated(target, current_key)
