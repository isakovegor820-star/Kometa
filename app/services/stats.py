"""Статистика для админки."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, Subscription, User


@dataclass(slots=True)
class Stats:
    users_total: int
    trials_active: int
    active_paid: int
    expired: int
    paying_total: int
    revenue_today: int
    revenue_week: int
    revenue_month: int
    pending_orders: int
    conversion: float

    def as_text(self) -> str:
        return (
            "📊 <b>Статистика Kometa</b>\n\n"
            f"👥 Пользователей всего: <b>{self.users_total}</b>\n"
            f"🎁 На пробном: <b>{self.trials_active}</b>\n"
            f"✅ Активных платных: <b>{self.active_paid}</b>\n"
            f"⌛️ Истекших: <b>{self.expired}</b>\n"
            f"💰 Платили хотя бы раз: <b>{self.paying_total}</b>\n\n"
            f"💵 Выручка сегодня: <b>{self.revenue_today} ₽</b>\n"
            f"💵 За 7 дней: <b>{self.revenue_week} ₽</b>\n"
            f"💵 За 30 дней: <b>{self.revenue_month} ₽</b>\n\n"
            f"🧾 Ожидают подтверждения: <b>{self.pending_orders}</b>\n"
            f"📈 Конверсия в оплату: <b>{self.conversion:.1f}%</b>"
        )


async def collect(session: AsyncSession) -> Stats:
    now = datetime.now(timezone.utc)

    users_total = await session.scalar(select(func.count(User.id))) or 0
    trials_active = (
        await session.scalar(
            select(func.count(Subscription.id)).where(
                Subscription.status == "trial", Subscription.expires_at > now
            )
        )
        or 0
    )
    active_paid = (
        await session.scalar(
            select(func.count(Subscription.id)).where(
                Subscription.status == "active", Subscription.expires_at > now
            )
        )
        or 0
    )
    expired = await session.scalar(select(func.count(Subscription.id)).where(Subscription.status == "expired")) or 0
    paying_total = (
        await session.scalar(select(func.count(func.distinct(Order.user_id))).where(Order.status == "paid")) or 0
    )

    async def revenue_since(moment: datetime) -> int:
        value = await session.scalar(
            select(func.coalesce(func.sum(Order.amount_rub), 0)).where(
                Order.status == "paid", Order.paid_at.is_not(None), Order.paid_at >= moment
            )
        )
        return int(value or 0)

    pending_orders = await session.scalar(select(func.count(Order.id)).where(Order.status == "pending")) or 0
    conversion = (paying_total / users_total * 100) if users_total else 0.0

    return Stats(
        users_total=users_total,
        trials_active=trials_active,
        active_paid=active_paid,
        expired=expired,
        paying_total=paying_total,
        revenue_today=await revenue_since(now - timedelta(days=1)),
        revenue_week=await revenue_since(now - timedelta(days=7)),
        revenue_month=await revenue_since(now - timedelta(days=30)),
        pending_orders=pending_orders,
        conversion=conversion,
    )
