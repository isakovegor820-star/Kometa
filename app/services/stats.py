"""Статистика для админки."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Order, Plan, Subscription, User


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


@dataclass(slots=True)
class ChannelStats:
    """Доходность одного канала приёма платежей."""

    provider: str
    title: str
    orders: int
    gross_rub: int
    net_rub: int

    @property
    def fee_percent(self) -> float:
        """Эффективная комиссия канала, % от оборота."""
        if not self.gross_rub:
            return 0.0
        return (1 - self.net_rub / self.gross_rub) * 100


PROVIDER_TITLES = {
    "manual": "Перевод по СБП/карте",
    "crypto": "Крипта (Crypto Pay)",
    "stars": "Telegram Stars",
    "wata": "Карта/СБП (WATA)",
}


async def channel_economics(session: AsyncSession, days: int = 30) -> list[ChannelStats]:
    """Сколько денег реально доходит до нас по каждому каналу.

    У звёзд «комиссия» не процент, а сама природа выплаты: Telegram платит
    фиксированные $0.013 за звезду, поэтому считаем от цены тарифа в звёздах.
    """
    settings = get_settings()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    paid_orders = list(
        (
            await session.scalars(
                select(Order).where(Order.status == "paid", Order.paid_at.is_not(None), Order.paid_at >= since)
            )
        ).all()
    )

    plans: dict[int, Plan] = {}
    buckets: dict[str, ChannelStats] = {}

    for order in paid_orders:
        plan = None
        if order.plan_id:
            if order.plan_id not in plans:
                plan = await session.get(Plan, order.plan_id)
                if plan is not None:
                    plans[order.plan_id] = plan
            plan = plans.get(order.plan_id)

        bucket = buckets.get(order.provider)
        if bucket is None:
            bucket = ChannelStats(
                provider=order.provider,
                title=PROVIDER_TITLES.get(order.provider, order.provider),
                orders=0,
                gross_rub=0,
                net_rub=0,
            )
            buckets[order.provider] = bucket

        bucket.orders += 1
        bucket.gross_rub += order.amount_rub
        bucket.net_rub += _net_for_order(order, plan)

    return sorted(buckets.values(), key=lambda item: item.net_rub, reverse=True)


def _net_for_order(order, plan) -> int:  # noqa: ANN001 - Order, Plan | None
    """Сколько остаётся с заказа после комиссий канала."""
    settings = get_settings()
    if order.provider == "stars":
        stars = plan.price_stars if plan and plan.price_stars else 0
        if not stars:
            return 0
        gross_usd = stars * settings.stars_payout_usd
        net_rub = gross_usd * settings.usd_rub_rate
        return int(round(net_rub * (1 - settings.fragment_withdrawal_percent / 100)))
    if order.provider == "wata":
        return int(round(order.amount_rub * (1 - settings.fee_percent_wata / 100)))
    if order.provider == "crypto":
        return int(round(order.amount_rub * (1 - settings.fee_percent_crypto / 100)))
    return int(round(order.amount_rub * (1 - settings.fee_percent_manual / 100)))


async def profit_summary(session: AsyncSession, days: int = 30) -> dict[str, float]:
    """Оборот, «на руки» и прибыль с учётом постоянных расходов."""
    settings = get_settings()
    channels = await channel_economics(session, days)
    gross = sum(channel.gross_rub for channel in channels)
    net = sum(channel.net_rub for channel in channels)
    costs = settings.monthly_costs_rub
    return {
        "gross": gross,
        "net": net,
        "costs": costs,
        "profit": net - costs,
        "margin_percent": (net - costs) / gross * 100 if gross else 0.0,
    }


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
