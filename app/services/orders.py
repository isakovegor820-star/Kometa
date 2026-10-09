"""Заказы: от выбора тарифа до выдачи доступа.

Ключевое требование — идемпотентность: повторное подтверждение одного и того же
заказа не должно продлевать подписку дважды.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Order, Plan, Subscription, User
from app.panels.base import PanelClient
from app.payments.matching import allocate_signature
from app.services import events, notifications, partners, promo as promo_service, referral, subscriptions

settings = get_settings()

KIND_PURCHASE = "purchase"
KIND_RENEW = "renew"
#: Покупка подписки в подарок: доступ выдаётся не покупателю, а получателю.
KIND_GIFT = "gift"


async def _allocate_pay_kopecks(session: AsyncSession, base_rub: int) -> int:
    """Подобрать уникальные копейки, чтобы платёж однозначно матчился с заказом.

    Смотрим только на активные (pending) заказы с такой же базовой суммой:
    двух заказов на 199.13 ₽ одновременно быть не должно.
    """
    taken_rows = await session.scalars(
        select(Order.pay_kopecks).where(Order.status == "pending", Order.amount_rub == base_rub)
    )
    taken = {int(value or 0) for value in taken_rows}
    return allocate_signature(taken)


async def create_order(
    session: AsyncSession,
    user: User,
    plan: Plan,
    *,
    provider: str,
    kind: str = KIND_PURCHASE,
    with_discount: bool = True,
) -> Order:
    sub = await subscriptions.get_subscription(session, user.id)
    if kind == KIND_PURCHASE and sub is not None and sub.is_active:
        kind = KIND_RENEW

    # Скидка на первую оплату: своя (введённый код) или за приглашение.
    discount = None
    if with_discount:
        discount = await promo_service.resolve(session, user, base_rub=plan.price_rub)

    base_rub = plan.price_rub
    discount_rub = discount.discount_rub if discount else 0
    amount_rub = base_rub - discount_rub
    stars_amount = 0
    if provider == "stars":
        stars_amount = discount.stars_for(plan.price_stars) if discount else plan.price_stars

    if discount_rub:
        # Один заказ со скидкой за раз: иначе можно оформить два и оплатить
        # оба по половинной цене.
        await _cancel_other_discounted(session, user)

    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        kind=kind,
        amount_rub=amount_rub,
        base_amount_rub=base_rub,
        discount_rub=discount_rub,
        promo_code=discount.code if discount else None,
        stars_amount=stars_amount,
        # Уникальные копейки нужны только для ручных переводов: по ним система
        # сама узнаёт, какой заказ оплатили.
        pay_kopecks=await _allocate_pay_kopecks(session, amount_rub) if provider == "manual" else 0,
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
        payload={
            "order_id": order.id,
            "plan": plan.code,
            "provider": provider,
            "amount": amount_rub,
            "base_amount": base_rub,
            "discount": discount_rub,
            "promo": order.promo_code,
            "stars": stars_amount,
        },
    )
    if discount is not None and discount_rub:
        await events.log_event(
            session,
            events.PROMO_APPLIED,
            user_id=user.id,
            payload={"order_id": order.id, "code": discount.code, "discount_rub": discount_rub},
        )
    return order


async def _cancel_other_discounted(session: AsyncSession, user: User) -> list[Order]:
    """Отменить другие неоплаченные заказы со скидкой у этого пользователя."""
    stmt = select(Order).where(
        Order.user_id == user.id,
        Order.status == "pending",
        Order.discount_rub > 0,
    )
    others = list((await session.scalars(stmt)).all())
    for order in others:
        order.status = "canceled"
        order.comment = "заменён новым заказом со скидкой"
        await events.log_event(
            session,
            events.ORDER_CANCELED,
            user_id=user.id,
            payload={"order_id": order.id, "reason": "replaced_by_discounted_order"},
        )
    if others:
        await session.flush()
    return others


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
    bot: Bot | None = None,
) -> tuple[Subscription | None, bool]:
    """Отметить заказ оплаченным и выдать/продлить доступ.

    Возвращает (подписка, уже_был_оплачен). Второй элемент True означает, что
    заказ уже был оплачен ранее — повторная выдача не производится.

    Если передан ``bot``, пригласивший получает сообщение о начисленных днях.
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

    # Партнёр, который привёл этого человека: фиксируем снимок в заказе и
    # считаем выплату. Партнёр получает с КАЖДОГО платежа, а не только с
    # первого — так ему выгодно приводить тех, кто остаётся (app/services/partners.py).
    await partners.accrue_reward(session, order, user)

    # Подарочный сертификат: деньги получены, но доступ покупателю не выдаём.
    # Дни уйдут получателю, когда он активирует ссылку (app/services/gift.py).
    if order.gift_token:
        sub = None
        await session.flush()
        await events.log_event(
            session,
            events.ORDER_PAID,
            user_id=user.id,
            payload={
                "order_id": order.id,
                "amount": order.amount_rub,
                "provider": order.provider,
                "gift": True,
                "gift_token": order.gift_token,
            },
        )
        return sub, False

    sub = await subscriptions.activate_plan(session, user, plan, panel)
    await session.flush()
    await events.log_event(
        session,
        events.ORDER_PAID,
        user_id=user.id,
        payload={
            "order_id": order.id,
            "amount": order.amount_rub,
            "discount": order.discount_rub,
            "promo": order.promo_code,
            "provider": order.provider,
        },
    )

    if order.discount_rub and order.promo_code:
        promo_row = await promo_service.get_by_code(session, order.promo_code)
        if promo_row is not None:
            redemption = await promo_service.redeem(
                session, promo_row, user, order=order, discount_rub=order.discount_rub
            )
            if redemption is not None:
                await events.log_event(
                    session,
                    events.PROMO_REDEEMED,
                    user_id=user.id,
                    payload={"order_id": order.id, "code": promo_row.code, "discount_rub": order.discount_rub},
                )
            # Промокод введён руками, а не получен по ссылке: привязываем
            # покупателя к владельцу кода, чтобы тот получил награду.
            if promo_row.kind == "referral" and promo_row.owner_user_id:
                owner = await session.get(User, promo_row.owner_user_id)
                if owner is not None and owner.id != user.id:
                    await referral.attach_referrer(session, user, owner.referral_code)

    reward = await referral.reward_on_payment(session, order, panel)
    # Временный атрибут (в БД не пишется): по нему вызывающий код добавляет
    # в сообщение клиенту строку про бонус за приглашение.
    order.referral_reward = reward  # type: ignore[attr-defined]

    if bot is not None and reward is not None:
        if reward.referrer_days > 0:
            await notifications.notify_referral_reward(bot, reward)
        elif reward.limit_reached:
            await notifications.notify_referral_limit(bot, reward)

    return sub, False


async def cancel_order(session: AsyncSession, order: Order, *, reason: str = "") -> None:
    if order.status != "pending":
        return
    order.status = "canceled"
    if reason:
        order.comment = reason
    await session.flush()
    await events.log_event(session, events.ORDER_CANCELED, user_id=order.user_id, payload={"order_id": order.id})


async def refund_order(
    session: AsyncSession,
    order: Order,
    panels: list[PanelClient],
    *,
    actor: str = "",
    note: str = "",
) -> tuple[bool, str, Subscription | None]:
    """Вернуть деньги по оплаченному заказу.

    Что важно для учёта: статус становится ``refunded``, и заказ **перестаёт
    попадать в выручку** (её считают только по ``paid``). Раньше возврат
    оставлял деньги в отчётах — сервис показывал прибыль, которой нет.

    Доступ забираем не всегда: если у клиента есть более поздняя оплата
    (например, вернули первый месяц, а второй оплачен) — доступ остаётся.

    :returns: (получилось, текст для админа, подписка)
    """
    if order.status == "refunded":
        return False, f"Заказ #{order.id} уже возвращён", await subscriptions.get_subscription(session, order.user_id)
    if order.status != "paid":
        return False, f"Вернуть можно только оплаченный заказ, а #{order.id} — {order.status}", None

    order.status = "refunded"
    order.refunded_at = datetime.now(timezone.utc)
    order.refunded_by = actor or "панель"
    order.refund_note = note or None

    sub = await subscriptions.get_subscription(session, order.user_id)
    other_paid = await session.scalar(
        select(Order.id).where(
            Order.user_id == order.user_id,
            Order.status == "paid",
            Order.id != order.id,
        ).limit(1)
    )

    await events.log_event(
        session,
        events.ORDER_REFUNDED,
        user_id=order.user_id,
        payload={
            "order_id": order.id,
            "amount": order.amount_rub,
            "provider": order.provider,
            "by": order.refunded_by,
            "note": note,
            "access_revoked": bool(sub is not None and other_paid is None),
        },
    )

    if sub is not None and other_paid is None:
        await subscriptions.revoke_access(session, sub, panels, reason=f"refund order #{order.id}")
        message = f"Заказ #{order.id} возвращён, доступ отключён"
    elif other_paid is not None:
        message = f"Заказ #{order.id} возвращён, доступ сохранён (есть более поздняя оплата)"
    else:
        message = f"Заказ #{order.id} возвращён (подписки у клиента уже нет)"

    await session.flush()
    return True, message, sub


async def pending_orders(session: AsyncSession, limit: int = 50) -> list[Order]:
    stmt = (
        select(Order)
        .where(Order.status == "pending")
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def awaiting_payment(
    session: AsyncSession,
    *,
    provider_prefix: str = "",
    hours: int = 24,
    limit: int = 100,
    statuses: tuple[str, ...] = ("pending", "canceled", "expired"),
) -> list[Order]:
    """Заказы, по которым стоит спросить платёжную систему о статусе.

    Кроме ``pending`` сюда попадают недавние ``canceled`` и ``expired``. Причина:
    клиент мог оплатить счёт, пока бот был выключен (или вебхук не дошёл).
    Платёжная ссылка живёт 15 минут, а заказ закрывается через 30 — если машина
    спала, заказ успевает истечь, и платёж состоялся по уже закрытому заказу.
    Такую оплату выдаём автоматически (см. ``finalize_order``).

    ``statuses`` позволяет разделить два режима опроса: открытые счета (их
    клиент оплачивает прямо сейчас — спрашиваем часто) и закрытые (оплата
    могла прийти позже — спрашиваем редко, чтобы не долбить платёжную систему).

    :param provider_prefix: ограничить одним провайдером (например ``platega``).
    :param hours: насколько глубоко смотреть назад.
    :param statuses: какие статусы заказов интересны.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    stmt = (
        select(Order)
        .where(
            Order.status.in_(statuses),
            Order.created_at >= since,
            Order.external_id.is_not(None),
        )
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    if provider_prefix:
        stmt = stmt.where(Order.provider.startswith(provider_prefix))
    return list((await session.scalars(stmt)).all())


async def pending_invoices(session: AsyncSession) -> int:
    """Счета, которые ждут оплаты клиентом, а не решения админа.

    ``pending`` — это выставленный счёт (Stars, Platega, крипта). Пока клиент не
    заплатил, админу делать нечего: доступ выдастся сам. Раньше это число
    попадало в сводку как «ждут подтверждения», и владелец шёл подтверждать
    неоплаченные счета.
    """
    return int(
        await session.scalar(
            select(func.count(Order.id)).where(
                Order.status == "pending", Order.provider != "manual"
            )
        )
        or 0
    )


async def manual_requests(session: AsyncSession) -> int:
    """Заявки на ручную оплату: тут админ действительно нужен.

    Только перевод по реквизитам (``provider="manual"``): клиент нажал
    «Оплатил, доступа нет», и подтвердить поступление может человек.
    """
    return int(
        await session.scalar(
            select(func.count(Order.id)).where(
                Order.status == "pending", Order.provider == "manual"
            )
        )
        or 0
    )


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
