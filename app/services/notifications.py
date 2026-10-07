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


async def notify_referral_reward(bot: Bot, reward) -> bool:  # noqa: ANN001 - referral.Reward
    """Сказать пригласившему, что друг оплатил и бонус начислен.

    Без этого сообщения люди не понимают, что рефералка работает:
    «я привёл друга, а где мои дни?» — самый частый вопрос в поддержку.
    """
    referrer = reward.referrer
    if referrer.is_blocked:
        return False

    if reward.referrer_accrued:
        tail = (
            f"Дни уже в запасе: <b>{referrer.bonus_days_balance} дн.</b>\n\n"
            "Как только подключишься (пробный доступ или оплата) — они добавятся к сроку "
            "автоматически."
        )
    else:
        expires = ""
        if reward.referrer_sub is not None and reward.referrer_sub.expires_at:
            expires = f"\nПодписка теперь активна до <b>{reward.referrer_sub.expires_at:%d.%m.%Y}</b>."
        tail = f"Дни уже начислены.{expires}"

    text = (
        "🎉 <b>Твой друг оплатил подписку!</b>\n\n"
        f"Тебе начислено <b>+{reward.referrer_days} дней</b> бесплатно.\n\n"
        f"{tail}\n\n"
        "Приглашай ещё — дни копятся: раздел «Пригласить друга»."
    )
    return await _send(bot, referrer.tg_id, text)


async def notify_referral_limit(bot: Bot, reward) -> bool:  # noqa: ANN001 - referral.Reward
    """Честно предупредить, что месячный лимит наград исчерпан."""
    referrer = reward.referrer
    if referrer.is_blocked:
        return False
    text = (
        "Друг оплатил подписку, но месячный лимит наград исчерпан — "
        f"бонус за эту оплату не начислен.\n\n"
        f"Лимит: {settings.referral_max_rewards_per_month} наград в месяц. "
        "Напиши в поддержку — разберёмся."
    )
    return await _send(bot, referrer.tg_id, text)
