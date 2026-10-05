"""Заказы: от выбора тарифа до выдачи доступа.

Ключевое требование — идемпотентность: повторное подтверждение одного и того же
заказа не должно продлевать подписку дважды.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Order, Plan, Subscription, User
from app.panels.base import PanelClient
from app.services import events, referral, subscriptions

settings = get_settings()

KIND_PURCHASE = "purchase"
KIND_RENEW = "renew"


async def create_order(
    session: AsyncSession,
    user: User,
    plan: Plan,
    *,
    provider: str,
    kind: str = KIND_PURCHASE,
) -> Order:
    sub = await subscriptions.get_subscription(session, user.id)
    if kind == KIND_PURCHASE and sub is not None and sub.is_active:
        kind = KIND_RENEW

    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        kind=kind,
        amount_rub=plan.price_rub,
        provider=provider,
        status="pending",
        external_id=f"ord-{uuid4().hex[:16]}",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.order_ttl_minutes),
    )
    session.add(order)
    await session.flush()
    await events.log_event(
        session,
        events.ORDER_CREATED,
        user_id=user.id,
        payload={"order_id": order.id, "plan": plan.code, "provider": provider, "amount": plan.price_rub},
    )
    return order


async def get_order(session: AsyncSession, order_id: int) -> Order | None:
    return await session.get(Order, order_id)


async def get_plan(session: AsyncSession, plan_id: int) -> Plan | None:
    return await session.get(Plan, plan_id)


async def list_plans(session: AsyncSession) -> list[Plan]:
    stmt = select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order)
    return list((await session.scalars(stmt)).all())


async def mark_paid(
    session: AsyncSession,
    order: Order,
    panel: PanelClient,
    *,
    confirmed_by: int | None = None,
    provider_payment_id: str | None = None,
) -> tuple[Subscription | None, bool]:
    """Отметить заказ оплаченным и выдать/продлить доступ.

    Возвращает (подписка, уже_был_оплачен). Второй элемент True означает, что
    заказ уже был оплачен ранее — повторная выдача не производится.
    """
    if order.status == "paid":
        return await subscriptions.get_subscription(session, order.user_id), True
    if order.status in {"canceled", "expired"}:
        return None, False

    plan = await get_plan(session, order.plan_id) if order.plan_id else None
    if plan is None:
        raise ValueError(f"у заказа #{order.id} нет тарифа")

    user = await session.get(User, order.user_id)
    if user is None:
        raise ValueError(f"у заказа #{order.id} нет пользователя")

    order.status = "paid"
    order.paid_at = datetime.now(timezone.utc)
    order.confirmed_by = confirmed_by
    if provider_payment_id:
        order.comment = f"payment_id={provider_payment_id}"

    sub = await subscriptions.activate_plan(session, user, plan, panel)
    await session.flush()
    await events.log_event(
        session,
        events.ORDER_PAID,
        user_id=user.id,
        payload={"order_id": order.id, "amount": order.amount_rub, "provider": order.provider},
    )
    await referral.reward_on_payment(session, order, panel)
    return sub, False


async def cancel_order(session: AsyncSession, order: Order, *, reason: str = "") -> None:
    if order.status != "pending":
        return
    order.status = "canceled"
    if reason:
        order.comment = reason
    await session.flush()
    await events.log_event(session, events.ORDER_CANCELED, user_id=order.user_id, payload={"order_id": order.id})


async def pending_orders(session: AsyncSession, limit: int = 50) -> list[Order]:
    stmt = (
        select(Order)
        .where(Order.status == "pending")
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def expire_stale_orders(session: AsyncSession) -> list[Order]:
    """Закрыть неоплаченные заказы, у которых истёк срок."""
    now = datetime.now(timezone.utc)
    stmt = select(Order).where(Order.status == "pending", Order.expires_at.is_not(None), Order.expires_at <= now)
    orders = list((await session.scalars(stmt)).all())
    for order in orders:
        order.status = "expired"
    if orders:
        await session.flush()
    return orders
