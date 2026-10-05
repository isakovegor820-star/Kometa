"""Реферальная программа.

Правила (из ТЗ):
  * у каждого пользователя есть код и ссылка вида t.me/<bot>?start=ref_<код>;
  * бонус начисляется только после ПЕРВОЙ реальной оплаты приглашённого;
  * пригласивший получает +7 дней, приглашённый +3 дня;
  * повторные награды за одного и того же человека невозможны (уникальный invited_id).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Order, Referral, User
from app.panels.base import PanelClient
from app.services import events, subscriptions

settings = get_settings()

#: защита от накрутки: не более N награждений одному пользователю в месяц
MAX_REWARDS_PER_MONTH = 10


async def attach_referrer(session: AsyncSession, user: User, code: str) -> User | None:
    """Привязать пригласившего по коду. Работает один раз и не для себя самого."""
    if not code or user.referred_by is not None:
        return None
    referrer = await session.scalar(select(User).where(User.referral_code == code))
    if referrer is None or referrer.id == user.id:
        return None
    if referrer.is_blocked:
        return None

    user.referred_by = referrer.id
    session.add(Referral(referrer_id=referrer.id, invited_id=user.id))
    await session.flush()
    return referrer


async def reward_on_payment(session: AsyncSession, order: Order, panel: PanelClient) -> dict | None:
    """Начислить бонусные дни, если это первая оплата приглашённого."""
    ref = await session.scalar(select(Referral).where(Referral.invited_id == order.user_id))
    if ref is None or ref.paid_order_id is not None:
        return None

    rewards_this_month = await session.scalar(
        select(func.count(Referral.id)).where(
            Referral.referrer_id == ref.referrer_id,
            Referral.paid_order_id.is_not(None),
        )
    )
    if (rewards_this_month or 0) >= MAX_REWARDS_PER_MONTH:
        return None

    referrer_days = settings.referral_bonus_days_referrer
    invited_days = settings.referral_bonus_days_invited

    referrer = await session.get(User, ref.referrer_id)
    invited = await session.get(User, ref.invited_id)
    if referrer is None or invited is None:
        return None

    ref.paid_order_id = order.id
    ref.bonus_days_referrer = referrer_days
    ref.bonus_days_invited = invited_days
    await session.flush()

    await subscriptions.extend_days(session, referrer, referrer_days, panel, reason="referral_referrer")
    await subscriptions.extend_days(session, invited, invited_days, panel, reason="referral_invited")

    await events.log_event(
        session,
        events.REFERRAL_REWARDED,
        user_id=referrer.id,
        payload={"invited_id": invited.id, "referrer_days": referrer_days, "invited_days": invited_days},
    )
    return {
        "referrer": referrer,
        "invited": invited,
        "referrer_days": referrer_days,
        "invited_days": invited_days,
    }


async def referral_stats(session: AsyncSession, user: User) -> tuple[int, int]:
    """(сколько приглашено, сколько из них оплатили)."""
    invited = await session.scalar(select(func.count(Referral.id)).where(Referral.referrer_id == user.id)) or 0
    paid = (
        await session.scalar(
            select(func.count(Referral.id)).where(
                Referral.referrer_id == user.id, Referral.paid_order_id.is_not(None)
            )
        )
        or 0
    )
    return invited, paid
