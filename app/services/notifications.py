"""Уведомления пользователям: напоминания о продлении, истечение, ответы поддержки."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Subscription, User
from app.services import subscriptions

logger = logging.getLogger(__name__)
settings = get_settings()


async def _send(bot: Bot, tg_id: int, text: str) -> bool:
    try:
        await bot.send_message(tg_id, text, disable_web_page_preview=True)
        return True
    except TelegramAPIError as exc:  # пользователь мог заблокировать бота
        logger.warning("Не удалось отправить сообщение %s: %s", tg_id, exc)
        return False


async def notify_expiring(bot: Bot, session: AsyncSession, days_before: int) -> int:
    """Напомнить тем, у кого подписка кончается через N дней."""
    subs = await subscriptions.due_for_reminder(session, days_before)
    sent = 0
    for sub in subs:
        user = await session.get(User, sub.user_id)
        if user is None or user.is_blocked:
            continue
        when = "завтра" if days_before == 1 else f"через {days_before} дня"
        text = (
            f"⏳ Подписка заканчивается {when}.\n\n"
            f"Продлить в один клик — раздел «Моя подписка» → «Продлить».\n"
            "Если ничего не делать, доступ отключится автоматически, данные сохраним 30 дней."
        )
        if await _send(bot, user.tg_id, text):
            sent += 1
    return sent


async def notify_expired(bot: Bot, session: AsyncSession, changed: list[Subscription]) -> int:
    sent = 0
    for sub in changed:
        user = await session.get(User, sub.user_id)
        if user is None or user.is_blocked:
            continue
        text = (
            "⌛️ Подписка закончилась, доступ отключён.\n\n"
            "Продлить можно в любой момент — доступ вернётся сразу после оплаты, "
            "настройки в приложении менять не нужно."
        )
        if await _send(bot, user.tg_id, text):
            sent += 1
    return sent


async def notify_admins(bot: Bot, text: str) -> None:
    for admin_id in settings.admin_id_list:
        await _send(bot, admin_id, text)
