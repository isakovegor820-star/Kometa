"""Статистика для админки."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

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

    # --- добавлено для страницы «Финансы»; поля с дефолтами, чтобы старый код
    # --- (и тесты), создающий Stats позиционно, продолжал работать.
    #: Сколько возвратов оформлено за последние 30 дней.
    refunds_month: int = 0
    #: Выручка за предыдущий календарный день (UTC).
    revenue_yesterday: int = 0
    #: Сколько заказов оплачено за последние 30 дней.
    paid_orders_month: int = 0
    #: Средний чек за 30 дней: revenue_month // paid_orders_month.
    avg_check_month: int = 0

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
            f"📈 Конверсия в оплату: <b>{self.conversion:.1f}%</b>\n\n"
            f"💵 Выручка за вчера: <b>{self.revenue_yesterday} ₽</b>\n"
            f"🧾 Оплачено за 30 дней: <b>{self.paid_orders_month}</b>\n"
            f"🧮 Средний чек за 30 дней: <b>{self.avg_check_month} ₽</b>\n"
            f"↩️ Возвратов за 30 дней: <b>{self.refunds_month}</b>"
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
    "manual": "Перевод по СБП/карте (напрямую)",
    "sbp": "СБП НСПК (QR/ссылка)",
    "platega_sbp": "СБП НСПК (QR/ссылка)",
    "platega_card": "Карта МИР (Platega)",
    "platega_intl": "Зарубежная карта (Platega)",
    "crypto": "Крипта (Crypto Pay)",
    "stars": "Telegram Stars",
}

#: Способы оплаты через банка-партнёра (НСПК): с оборота удерживается
#: ставка FEE_PERCENT_SBP. Прямой перевод на карту сюда не входит —
#: у него комиссии нет.
SBP_PROVIDERS = frozenset({"sbp", "nspk", "platega_sbp"})


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
    if order.provider == "crypto":
        return int(round(order.amount_rub * (1 - settings.fee_percent_crypto / 100)))
    if order.provider in SBP_PROVIDERS:
        return int(round(order.amount_rub * (1 - settings.fee_percent_sbp / 100)))
    return int(round(order.amount_rub * (1 - settings.fee_percent_manual / 100)))


#: Подпись строки для заказов без тарифа (тариф удалили или не проставили).
NO_PLAN_TITLE = "Без тарифа"


@dataclass(slots=True)
class DayPoint:
    """Один день на графике выручки."""

    day: date
    rub: int
    orders: int


def _as_utc(moment: datetime) -> datetime:
    """SQLite отдаёт время без зоны — достраиваем UTC, а не гадаем."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


async def revenue_by_day(session: AsyncSession, days: int = 30) -> list[DayPoint]:
    """Выручка по календарным дням (UTC) за последние ``days`` дней.

    Последняя точка — сегодня, порядок — от старых к новым. Дни без оплат
    возвращаются нулями: точек ровно ``days``, поэтому график не «съезжает».
    Считаются только заказы ``status == "paid"`` с заполненным ``paid_at``
    (возвраты в выручку не попадают). ``days <= 0`` — пустой список.
    """
    if days <= 0:
        return []

    today = datetime.now(timezone.utc).date()
    first_day = today - timedelta(days=days - 1)
    since = datetime.combine(first_day, time.min, tzinfo=timezone.utc)

    points: dict[date, DayPoint] = {}
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        points[day] = DayPoint(day=day, rub=0, orders=0)

    paid_orders = await session.scalars(
        select(Order).where(Order.status == "paid", Order.paid_at.is_not(None), Order.paid_at >= since)
    )
    for order in paid_orders:
        point = points.get(_as_utc(order.paid_at).date())
        if point is None:
            continue
        point.rub += order.amount_rub
        point.orders += 1

    return list(points.values())


@dataclass(slots=True)
class PlanRevenue:
    """Тариф в разрезе выручки: сколько заказов и сколько остаётся на руки."""

    plan_id: int | None
    title: str
    orders: int
    gross_rub: int
    net_rub: int


async def revenue_by_plan(session: AsyncSession, days: int = 30) -> list[PlanRevenue]:
    """Выручка по тарифам за последние ``days`` дней (окно — как у каналов).

    Заказы без тарифа (или с уже удалённым тарифом) собираются в одну строку
    «Без тарифа». ``net_rub`` считается той же :func:`_net_for_order`, что и в
    остальных отчётах, поэтому суммы сходятся с ``channel_economics``.
    Сортировка — по ``net_rub`` убыв.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    paid_orders = list(
        (
            await session.scalars(
                select(Order).where(Order.status == "paid", Order.paid_at.is_not(None), Order.paid_at >= since)
            )
        ).all()
    )

    plans: dict[int, Plan] = {}
    buckets: dict[int | None, PlanRevenue] = {}

    for order in paid_orders:
        plan = None
        if order.plan_id:
            if order.plan_id not in plans:
                loaded = await session.get(Plan, order.plan_id)
                if loaded is not None:
                    plans[order.plan_id] = loaded
            plan = plans.get(order.plan_id)

        # Ключ — id тарифа, а не его название: у двух тарифов может совпасть title.
        key = plan.id if plan is not None else None
        bucket = buckets.get(key)
        if bucket is None:
            bucket = PlanRevenue(
                plan_id=key,
                title=plan.title if plan is not None else NO_PLAN_TITLE,
                orders=0,
                gross_rub=0,
                net_rub=0,
            )
            buckets[key] = bucket

        bucket.orders += 1
        bucket.gross_rub += order.amount_rub
        bucket.net_rub += _net_for_order(order, plan)

    return sorted(buckets.values(), key=lambda item: item.net_rub, reverse=True)


async def refund_summary(session: AsyncSession, days: int = 30) -> dict[str, int]:
    """Возвраты за последние ``days`` дней: ``{"count": N, "rub": S}``.

    Считаем по ``refunded_at``, а не по дате заказа: возврат по старому заказу
    должен попадать в отчёт того месяца, когда деньги вернули.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    row = (
        await session.execute(
            select(func.count(Order.id), func.coalesce(func.sum(Order.amount_rub), 0)).where(
                Order.status == "refunded",
                Order.refunded_at.is_not(None),
                Order.refunded_at >= since,
            )
        )
    ).one()
    return {"count": int(row[0] or 0), "rub": int(row[1] or 0)}


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
    """Собрать сводку для дашборда.

    «Вчера» — предыдущий календарный день UTC (от полуночи до полуночи), как и
    точки :func:`revenue_by_day`. Так цифра на дашборде совпадает с последним
    столбиком графика; «сегодня» и остальные окна по-прежнему скользящие.
    """
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

    async def revenue_between(start: datetime, end: datetime) -> int:
        """Выручка за интервал [start, end): нужна для «вчера»."""
        value = await session.scalar(
            select(func.coalesce(func.sum(Order.amount_rub), 0)).where(
                Order.status == "paid",
                Order.paid_at.is_not(None),
                Order.paid_at >= start,
                Order.paid_at < end,
            )
        )
        return int(value or 0)

    async def paid_orders_since(moment: datetime) -> int:
        value = await session.scalar(
            select(func.count(Order.id)).where(
                Order.status == "paid", Order.paid_at.is_not(None), Order.paid_at >= moment
            )
        )
        return int(value or 0)

    pending_orders = await session.scalar(select(func.count(Order.id)).where(Order.status == "pending")) or 0
    conversion = (paying_total / users_total * 100) if users_total else 0.0

    revenue_month = await revenue_since(now - timedelta(days=30))
    paid_orders_month = await paid_orders_since(now - timedelta(days=30))
    today_start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)

    return Stats(
        users_total=users_total,
        trials_active=trials_active,
        active_paid=active_paid,
        expired=expired,
        paying_total=paying_total,
        revenue_today=await revenue_since(now - timedelta(days=1)),
        revenue_week=await revenue_since(now - timedelta(days=7)),
        revenue_month=revenue_month,
        pending_orders=pending_orders,
        conversion=conversion,
        refunds_month=(await refund_summary(session, 30))["count"],
        revenue_yesterday=await revenue_between(today_start - timedelta(days=1), today_start),
        paid_orders_month=paid_orders_month,
        avg_check_month=revenue_month // paid_orders_month if paid_orders_month else 0,
    )
