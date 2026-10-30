"""Persisted daily questions, independent of the interactive game's FSM."""

import asyncio
from datetime import datetime, timedelta, timezone
import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter,
)
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from db import async_session
from handlers.game import get_or_create_user, question_text
from keyboards import get_keyboard
from models import (
    Answer, AutotestDelivery, AutotestSubscription, GroupAutotestDelivery,
    GroupAutotestSubscription, Question, User,
)
from questions import next_available_question, question_choices


logger = logging.getLogger(__name__)
DAY = timedelta(days=1)
POLL_INTERVAL = 60
RETRY_DELAY = timedelta(minutes=5)
_sender_task: asyncio.Task | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def toggle_subscription(tg_id: int) -> bool:
    async with async_session() as session:
        user = await get_or_create_user(tg_id, session)
        await session.execute(
            insert(AutotestSubscription).values(user_id=user.id)
            .on_conflict_do_nothing(index_elements=[AutotestSubscription.user_id])
        )
        result = await session.execute(
            select(AutotestSubscription)
            .where(AutotestSubscription.user_id == user.id)
            .with_for_update()
        )
        subscription = result.scalar_one()
        enabled = subscription.next_send_at is None
        subscription.next_send_at = utc_now() + DAY if enabled else None
        await session.commit()
        return enabled


async def toggle_group_subscription(chat_id: int) -> bool:
    async with async_session() as session:
        await session.execute(
            insert(GroupAutotestSubscription).values(chat_id=chat_id)
            .on_conflict_do_nothing(index_elements=[GroupAutotestSubscription.chat_id])
        )
        result = await session.execute(
            select(GroupAutotestSubscription)
            .where(GroupAutotestSubscription.chat_id == chat_id)
            .with_for_update()
        )
        subscription = result.scalar_one()
        enabled = subscription.next_send_at is None
        subscription.next_send_at = utc_now() + DAY if enabled else None
        await session.commit()
        return enabled


async def _send_question(bot: Bot, chat_id: int, question: Question) -> tuple[bool, datetime | None]:
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=question_text(question),
            parse_mode="HTML",
            reply_markup=get_keyboard(
                question_choices(question), question.id,
                callback_prefix="autotest_answer",
            ),
            request_timeout=20,
        )
    except TelegramForbiddenError:
        return False, None
    except TelegramRetryAfter as error:
        return False, utc_now() + timedelta(seconds=max(60, error.retry_after))
    except TelegramBadRequest as error:
        if "chat not found" in error.message.casefold():
            return False, None
        logger.warning("Не удалось отправить Автотест в чат %s: TelegramBadRequest", chat_id)
        return False, utc_now() + RETRY_DELAY
    except TelegramAPIError as error:
        logger.warning("Не удалось отправить Автотест в чат %s: %s", chat_id, type(error).__name__)
        return False, utc_now() + RETRY_DELAY
    return True, utc_now() + DAY


async def send_one_due_autotest(bot: Bot, *, now: datetime | None = None) -> bool:
    now = now if now is not None else utc_now()
    async with async_session() as session:
        result = await session.execute(
            select(AutotestSubscription, User.tg_id)
            .join(User, User.id == AutotestSubscription.user_id)
            .where(AutotestSubscription.next_send_at <= now)
            .order_by(AutotestSubscription.next_send_at, AutotestSubscription.user_id)
            .limit(1)
            .with_for_update(of=AutotestSubscription, skip_locked=True)
        )
        row = result.first()
        if row is None:
            return False
        subscription, tg_id = row

        # Keep the row locked through delivery and commit: a second worker
        # cannot send the same word, and switching off waits for an in-flight send.
        answered = select(Answer.question_id).where(Answer.user_id == subscription.user_id)
        delivered = select(AutotestDelivery.question_id).where(
            AutotestDelivery.user_id == subscription.user_id
        )
        question = await next_available_question(
            session, ~Question.id.in_(answered), ~Question.id.in_(delivered),
        )
        if question is None:
            # Leave the subscription active for questions added in the future.
            subscription.next_send_at = now + DAY
        else:
            sent, subscription.next_send_at = await _send_question(bot, tg_id, question)
            if sent:
                session.add(AutotestDelivery(user_id=subscription.user_id, question_id=question.id))
        await session.commit()
        return True


async def send_one_due_group_autotest(bot: Bot, *, now: datetime | None = None) -> bool:
    now = now if now is not None else utc_now()
    async with async_session() as session:
        result = await session.execute(
            select(GroupAutotestSubscription)
            .where(GroupAutotestSubscription.next_send_at <= now)
            .order_by(GroupAutotestSubscription.next_send_at, GroupAutotestSubscription.chat_id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        subscription = result.scalars().first()
        if subscription is None:
            return False

        # One schedule and delivery history per group, independent of who
        # enabled it and of individual members' answers/private subscriptions.
        delivered = select(GroupAutotestDelivery.question_id).where(
            GroupAutotestDelivery.chat_id == subscription.chat_id
        )
        question = await next_available_question(session, ~Question.id.in_(delivered))
        if question is None:
            subscription.next_send_at = now + DAY
        else:
            sent, subscription.next_send_at = await _send_question(bot, subscription.chat_id, question)
            if sent:
                session.add(GroupAutotestDelivery(chat_id=subscription.chat_id, question_id=question.id))
        await session.commit()
        return True


async def send_due_autotests(bot: Bot) -> None:
    # Bound each pass and avoid a burst above Telegram's usual sending rate.
    for _ in range(100):
        private_processed = await send_one_due_autotest(bot)
        group_processed = await send_one_due_group_autotest(bot)
        if not (private_processed or group_processed):
            return
        await asyncio.sleep(0.1)


async def _run_autotests(bot: Bot) -> None:
    while True:
        try:
            await send_due_autotests(bot)
        except Exception:
            logger.exception("Ошибка ежедневной отправки Автотестов")
        await asyncio.sleep(POLL_INTERVAL)


async def start_autotests(bot: Bot) -> None:
    global _sender_task
    if _sender_task is None or _sender_task.done():
        _sender_task = asyncio.create_task(_run_autotests(bot))


async def stop_autotests() -> None:
    global _sender_task
    if _sender_task is not None:
        _sender_task.cancel()
        await asyncio.gather(_sender_task, return_exceptions=True)
        _sender_task = None
