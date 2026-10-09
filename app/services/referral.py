"""Реферальная программа.

Правила:
  * у каждого пользователя есть код и ссылка вида t.me/<bot>?start=ref_<код>;
  * друг по ссылке получает скидку 50% на первую оплату (см. app.services.promo);
  * после ПЕРВОЙ реальной оплаты приглашённого пригласивший получает +30 дней,
    а приглашённый — ещё +3 дня сверху;
  * повторные награды за одного и того же человека невозможны (уникальный invited_id);
  * не более N награждений одному человеку в календарный месяц (защита от накрутки);
  * если у пригласившего ещё нет подписки, дни не теряются, а копятся
    в ``User.bonus_days_balance`` и применяются при первой же подписке.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Order, Referral, Subscription, User, utcnow
from app.panels.base import PanelClient
from app.services import events, promo as promo_service, subscriptions

settings = get_settings()


@dataclass(slots=True)
class Friend:
    """Строка списка «мои друзья»."""

    user: User
    paid: bool
    bonus_days: int
    joined_at: datetime


@dataclass(slots=True)
class Reward:
    """Что получилось после оплаты приглашённого."""

    referrer: User
    invited: User
    referrer_days: int
    invited_days: int
    referrer_sub: Subscription | None
    referrer_accrued: int
    limit_reached: bool
    #: Дни за продление (вторая и следующие оплаты друга). 0 — это первая оплата.
    renewal_days: int = 0
    #: Номер оплаты друга: 1 — первая, 2 — первое продление и так далее.
    renewal_number: int = 0


def month_start(now: datetime | None = None) -> datetime:
    """Начало текущего календарного месяца (UTC) — окно лимита наград."""
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


# ------------------------------------------------------------------ привязка
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
    # Промокод создаём сразу: другу он понадобится в том же сообщении.
    await promo_service.ensure_referral_code(session, referrer)
    await events.log_event(
        session,
        events.REFERRAL_JOINED,
        user_id=referrer.id,
        payload={"invited_id": user.id, "invited_tg_id": user.tg_id},
    )
    return referrer


# ------------------------------------------------------------------ награда
async def rewards_this_month(session: AsyncSession, referrer_id: int) -> int:
    """Сколько наград пригласивший уже получил в этом календарном месяце."""
    count = await session.scalar(
        select(func.count(Referral.id)).where(
            Referral.referrer_id == referrer_id,
            Referral.rewarded_at.is_not(None),
            Referral.rewarded_at >= month_start(),
            Referral.bonus_days_referrer > 0,
        )
    )
    return int(count or 0)


async def reward_on_payment(session: AsyncSession, order: Order, panel: PanelClient) -> Reward | None:
    """Начислить бонусные дни за оплату приглашённого.

    Первая оплата друга даёт полную награду (``referral_bonus_days_referrer``),
    каждое продление — ``referral_bonus_days_renewal``. Так программа работает
    не только на привлечение, но и на удержание: пока друг платит, пригласивший
    продолжает получать дни (см. docs/МАРКЕТИНГ-ЭКОНОМИКА.md).
    """
    ref = await session.scalar(select(Referral).where(Referral.invited_id == order.user_id))
    if ref is None:
        return None

    referrer = await session.get(User, ref.referrer_id)
    invited = await session.get(User, ref.invited_id)
    if referrer is None or invited is None:
        return None

    is_renewal = ref.paid_order_id is not None
    limit_reached = await rewards_this_month(session, ref.referrer_id) >= settings.referral_max_rewards_per_month

    if is_renewal:
        # Продление: награда поменьше, но повторяемая. Начисляем и засчитываем
        # в тот же месячный лимит, чтобы накрутка через самопродление не прошла.
        ref.renewals_count = (ref.renewals_count or 0) + 1
        renewal_days = 0 if limit_reached else settings.referral_bonus_days_renewal
        ref.renewal_bonus_days = (ref.renewal_bonus_days or 0) + renewal_days
        if renewal_days > 0:
            ref.renewal_rewarded_at = utcnow()
            ref.rewarded_at = utcnow()
        await session.flush()

        referrer_sub: Subscription | None = None
        accrued = 0
        if renewal_days > 0:
            referrer_sub, accrued = await subscriptions.add_bonus_days(
                session, referrer, renewal_days, panel, reason="referral_renewal"
            )
        await events.log_event(
            session,
            events.REFERRAL_REWARDED,
            user_id=referrer.id,
            payload={
                "invited_id": invited.id,
                "order_id": order.id,
                "renewal": True,
                "renewal_number": ref.renewals_count,
                "referrer_days": renewal_days,
                "accrued_days": accrued,
                "limit_reached": limit_reached,
            },
        )
        return Reward(
            referrer=referrer,
            invited=invited,
            referrer_days=renewal_days,
            invited_days=0,
            referrer_sub=referrer_sub,
            referrer_accrued=accrued,
            limit_reached=limit_reached,
            renewal_days=renewal_days,
            renewal_number=ref.renewals_count or 0,
        )

    referrer_days = 0 if limit_reached else settings.referral_bonus_days_referrer
    invited_days = settings.referral_bonus_days_invited

    # Фиксируем факт обработки в любом случае: одну и ту же оплату
    # не разбираем дважды, даже если сработал месячный лимит.
    ref.paid_order_id = order.id
    ref.bonus_days_referrer = referrer_days
    ref.bonus_days_invited = invited_days
    ref.rewarded_at = utcnow()
    await session.flush()

    referrer_sub: Subscription | None = None
    accrued = 0
    if referrer_days > 0:
        referrer_sub, accrued = await subscriptions.add_bonus_days(
            session, referrer, referrer_days, panel, reason="referral_referrer"
        )
    await subscriptions.add_bonus_days(session, invited, invited_days, panel, reason="referral_invited")

    await events.log_event(
        session,
        events.REFERRAL_REWARDED,
        user_id=referrer.id,
        payload={
            "invited_id": invited.id,
            "order_id": order.id,
            "referrer_days": referrer_days,
            "invited_days": invited_days,
            "accrued_days": accrued,
            "limit_reached": limit_reached,
        },
    )
    return Reward(
        referrer=referrer,
        invited=invited,
        referrer_days=referrer_days,
        invited_days=invited_days,
        referrer_sub=referrer_sub,
        referrer_accrued=accrued,
        limit_reached=limit_reached,
    )


# ------------------------------------------------------------------ статистика
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
    return int(invited), int(paid)


async def earned_days(session: AsyncSession, user: User) -> int:
    """Сколько дней пользователь заработал на приглашениях за всё время."""
    total = await session.scalar(
        select(func.coalesce(func.sum(Referral.bonus_days_referrer), 0)).where(Referral.referrer_id == user.id)
    )
    return int(total or 0)


async def list_invited(session: AsyncSession, user: User, limit: int = 10) -> list[Friend]:
    """Кого пригласил пользователь и чем всё закончилось."""
    rows = (
        await session.execute(
            select(Referral, User)
            .join(User, User.id == Referral.invited_id)
            .where(Referral.referrer_id == user.id)
            .order_by(Referral.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        Friend(
            user=invited,
            paid=ref.paid_order_id is not None,
            bonus_days=ref.bonus_days_referrer or 0,
            joined_at=ref.created_at,
        )
        for ref, invited in rows
    ]


async def overview(session: AsyncSession, user: User) -> dict:
    """Всё, что нужно экрану «Пригласить друга», одним запросом-набором."""
    invited, paid = await referral_stats(session, user)
    return {
        "invited": invited,
        "paid": paid,
        "earned_days": await earned_days(session, user),
        "balance": int(user.bonus_days_balance or 0),
        "rewards_left": max(0, settings.referral_max_rewards_per_month - await rewards_this_month(session, user.id)),
    }


# ------------------------------------------------------------------ админка
async def top_referrers(session: AsyncSession, limit: int = 10) -> list[tuple[User, int, int]]:
    """(пользователь, приглашено, оплатили) — для админки и отчётов."""
    stmt = (
        select(
            User,
            func.count(Referral.id).label("invited"),
            func.count(Referral.paid_order_id).label("paid"),
        )
        .join(Referral, Referral.referrer_id == User.id)
        .group_by(User.id)
        .order_by(func.count(Referral.paid_order_id).desc(), func.count(Referral.id).desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [(user, int(invited), int(paid)) for user, invited, paid in rows]


async def program_stats(session: AsyncSession) -> dict:
    """Сводка по реферальной программе: сколько привели, сколько заплатили,
    сколько дней и рублей на это ушло."""
    invited = await session.scalar(select(func.count(Referral.id))) or 0
    paid = await session.scalar(select(func.count(Referral.paid_order_id))) or 0
    days = await session.scalar(select(func.coalesce(func.sum(Referral.bonus_days_referrer), 0))) or 0
    accrued = await session.scalar(
        select(func.coalesce(func.sum(User.bonus_days_balance), 0))
    ) or 0
    return {
        "invited": int(invited),
        "paid": int(paid),
        "conversion": round(int(paid) / int(invited) * 100) if invited else 0,
        "days_rewarded": int(days),
        "days_in_balance": int(accrued),
    }
