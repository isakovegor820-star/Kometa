"""Заказы: от выбора тарифа до выдачи доступа.

Ключевое требование — идемпотентность: повторное подтверждение одного и того же
заказа не должно продлевать подписку дважды.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from aiogram import Bot
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.config import get_settings
from app.db.models import Order, Plan, Subscription, User
from app.panels.base import PanelClient, PanelError
from app.payments.matching import allocate_signature
from app.services import (
    alerts as alerts_service,
    events,
    notifications,
    partners,
    promo as promo_service,
    referral,
    subscriptions,
)

settings = get_settings()
logger = logging.getLogger(__name__)

KIND_PURCHASE = "purchase"
KIND_RENEW = "renew"
#: Покупка подписки в подарок: доступ выдаётся не покупателю, а получателю.
KIND_GIFT = "gift"

#: Сколько раз фоновая задача пытается выдать доступ по оплаченному заказу.
#: После лимита заказ оставляем человеку: алерт уже поднят, а бесконечно
#: дёргать мёртвую панель бессмысленно и вредно.
MAX_GRANT_ATTEMPTS = 10


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


# ------------------------------------------------------- выдача доступа (B2)
def _grant_fingerprint(order_id: int) -> str:
    """Отпечаток алерта «оплаченный заказ без доступа» — один на заказ."""
    return f"grant:order:{order_id}"


def _grant_target(sub: Subscription | None, plan: Plan, *, now: datetime | None = None) -> datetime:
    """До какого срока подписка должна продлиться после оплаты.

    Считаем от текущего срока (если подписка ещё жива) или от «сейчас»: это
    минимум, который обязана подтвердить панель. Бонусные дни и ручные правки в
    панели только увеличивают срок, поэтому проверка «не меньше цели» —
    корректный способ убедиться, что выдача состоялась.
    """
    moment = now or datetime.now(timezone.utc)
    base = sub.expires_at if sub is not None and sub.expires_at and sub.expires_at > moment else moment
    return base + timedelta(days=max(0, plan.days))


def _grant_problem(sub: Subscription | None, target: datetime | None) -> str:
    """Расхождение «что ждали ↔ что выдала панель» текстом. Пусто — всё сошлось."""
    if target is None:
        return ""
    if sub is None or sub.expires_at is None:
        return "панель не вернула срок подписки"
    if sub.expires_at < target:
        return (
            f"панель выдала доступ до {sub.expires_at:%d.%m.%Y %H:%M}, "
            f"а ожидали минимум до {target:%d.%m.%Y %H:%M}"
        )
    return ""


async def _record_grant_failure(
    session: AsyncSession,
    order: Order,
    target: datetime | None,
    exc: BaseException,
) -> None:
    """Запомнить, что оплаченный заказ ждёт выдачи, и поднять алерт.

    Коммитим сразу: факт «деньги приняты, доступ не выдан» должен пережить и
    падение процесса, и rollback вызывающего. Иначе заказ снова станет
    «оплачен и забыт» — ровно та дыра, из-за которой клиент платил и не получал
    ничего (B2).
    """
    order.grant_target_at = target or order.grant_target_at
    order.grant_attempts = int(order.grant_attempts or 0) + 1
    order.grant_last_error = str(exc)[:300]
    await alerts_service.raise_alert(
        session,
        "panel_error",
        title=f"Оплачен заказ #{order.id}, доступа нет",
        message=(
            f"Заказ #{order.id} на {order.amount_rub} ₽ оплачен, но доступ не выдан: "
            f"{order.grant_last_error}. Фоновая задача повторит выдачу автоматически."
        ),
        fingerprint=_grant_fingerprint(order.id),
        user_id=order.user_id,
    )
    await session.commit()


async def grant_ungranted_orders(
    session: AsyncSession,
    panels: list[PanelClient] | None = None,
    *,
    bot: Bot | None = None,
    limit: int = 50,
) -> list[int]:
    """Выдать доступ по оплаченным заказам, у которых выдача не подтверждена.

    Покрывает два случая: панель лежала в момент оплаты и процесс упал между
    оплатой и выдачей. Идемпотентность двойная:

    * заказ берём только с пустым ``granted_at``;
    * перед обращением к панели сверяем **факт**: если панель уже продлила
      клиента (ответ потерялся, а панель успела), выдачу не повторяем — просто
      отмечаем её выполненной.

    Старые заказы без ``grant_target_at`` не трогаем: цель выдачи неизвестна,
    автоматически продлевать их — значит дарить дни. Такие разбирает человек.
    """
    rows = list(
        (
            await session.scalars(
                select(Order)
                .where(
                    Order.status == "paid",
                    Order.granted_at.is_(None),
                    Order.grant_target_at.is_not(None),
                )
                .order_by(Order.created_at)
                .limit(limit)
            )
        ).all()
    )

    done: list[int] = []
    for order in rows:
        plan = await get_plan(session, order.plan_id) if order.plan_id else None
        user = await session.get(User, order.user_id)
        if plan is None or user is None:
            continue

        target = order.grant_target_at
        sub = await subscriptions.get_subscription(session, user.id)
        if not _grant_problem(sub, target):
            # Панель уже выдала доступ — фиксируем факт и снимаем алерт.
            order.granted_at = datetime.now(timezone.utc)
            order.grant_last_error = ""
            await alerts_service.resolve_by_fingerprint(session, _grant_fingerprint(order.id), by="auto")
            done.append(order.id)
            continue

        if int(order.grant_attempts or 0) >= MAX_GRANT_ATTEMPTS:
            logger.error(
                "Заказ #%s: доступ не выдан после %s попыток (%s) — нужен человек",
                order.id,
                order.grant_attempts,
                order.grant_last_error,
            )
            continue

        try:
            await subscriptions.activate_plan(
                session, user, plan, panels or await subscriptions.all_user_panels(session)
            )
        except Exception as exc:  # noqa: BLE001 - панель может не ответить
            logger.error("Повторная выдача по заказу #%s не удалась: %s", order.id, exc)
            await _record_grant_failure(session, order, target, exc)
            continue

        # Проверяем факт, а не «не было исключения»: панель могла ответить
        # успехом и не продлить срок (ручная правка, чужой клиент, старая нода).
        sub = await subscriptions.get_subscription(session, user.id)
        problem = _grant_problem(sub, target)
        if problem:
            logger.error("Заказ #%s: %s", order.id, problem)
            await _record_grant_failure(session, order, target, PanelError(problem))
            continue

        order.granted_at = datetime.now(timezone.utc)
        order.grant_last_error = ""
        await alerts_service.resolve_by_fingerprint(session, _grant_fingerprint(order.id), by="auto")
        done.append(order.id)
        if bot is not None:
            await notify_granted(bot, user, sub, order)

    await session.flush()
    return done


async def notify_granted(bot: Bot, user: User, sub: Subscription | None, order: Order) -> None:
    """Сообщить клиенту, что оплата наконец дошла до доступа (после повторной выдачи)."""
    if sub is None:
        return
    from app.bot import keyboards

    expires = sub.expires_at.strftime("%d.%m.%Y %H:%M") if sub.expires_at else "—"
    link = subscriptions.subscription_link(sub.subscription_token)
    try:
        await bot.send_message(
            user.tg_id,
            f"✅ Оплата по заказу #{order.id} дошла до доступа. Подписка активна до <b>{expires}</b>.",
        )
        await bot.send_message(
            user.tg_id,
            texts.SUBSCRIPTION_LINK_HINT.format(link=link),
            reply_markup=keyboards.connect_kb(link),
            disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001 - сообщение не важнее выдачи
        logger.warning("Не смог сообщить о выдаче доступа %s: %s", user.tg_id, exc)


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

    Идемпотентность держится на **атомарном захвате заказа в БД**, а не на
    статусе объекта в памяти. Причина: между чтением заказа и этой функцией
    проходит проверка платежа в платёжной системе (в бою ``GET /transaction``
    у Platega отвечает ~10 секунд). За это время тот же заказ успевает
    подтвердить параллельный путь — фоновый опрос, вебхук или кнопка
    «Проверить оплату». В памяти вызывающего статус остаётся ``pending``,
    поэтому проверка по объекту пропускала вторую выдачу: клиент заплатил за
    один месяц, а срок продлевался дважды (инцидент 09.10.2026, заказ #8 —
    «36 дн.» и «66 дн.» в одном чате).

    Если передан ``bot``, пригласивший получает сообщение о начисленных днях.
    """
    plan = await get_plan(session, order.plan_id) if order.plan_id else None
    if plan is None:
        raise ValueError(f"у заказа #{order.id} нет тарифа")

    user = await session.get(User, order.user_id)
    if user is None:
        raise ValueError(f"у заказа #{order.id} нет пользователя")

    # Захват заказа: право выдать доступ получает ровно один вызов. Условие
    # «status = pending» проверяется и меняется одной инструкцией, поэтому
    # второй и последующие подтверждения получают rowcount = 0, даже если
    # пришли с устаревшим объектом заказа.
    now = datetime.now(timezone.utc)
    sub_before = await subscriptions.get_subscription(session, user.id)
    target = _grant_target(sub_before, plan, now=now)
    values: dict[str, object] = {
        "status": "paid",
        "paid_at": now,
        "confirmed_by": confirmed_by,
        # Цель выдачи фиксируем ДО обращения к панели: по ней потом проверим
        # факт, а фоновая задача поймёт, чего добиваться при повторе.
        "grant_target_at": target,
        "grant_last_error": "",
    }
    if provider_payment_id:
        values["comment"] = f"payment_id={provider_payment_id}"
    claimed = await session.execute(
        update(Order).where(Order.id == order.id, Order.status == "pending").values(**values)
    )
    # Объект в памяти мог устареть (в том числе по статусу) — подтягиваем факт из БД.
    await session.refresh(order)

    if not claimed.rowcount:
        # Заказ уже обработан другим путём. Оплачен — доступ выдан, повторять
        # нечего; закрыт (отменён/истёк) — вызывающий сам решает, что делать.
        if order.status in {"paid", "refunded"}:
            return await subscriptions.get_subscription(session, order.user_id), True
        return None, False

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
            "grant_target": target.isoformat(),
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

    # Деньги уже приняты — фиксируем это ДО обращения к панели. Раньше статус
    # «paid» и выдача жили в одной транзакции: панель не ответила, вызывающий
    # погасил ошибку и закоммитил — заказ выпадал из всех очередей ретрая
    # («деньги приняты, доступа нет и не будет», B2).
    await session.commit()

    try:
        sub = await subscriptions.activate_plan(session, user, plan, panel)
    except Exception as exc:  # noqa: BLE001 - панель может не ответить
        logger.error("Панель не выдала доступ по заказу #%s: %s", order.id, exc)
        await _record_grant_failure(session, order, target, exc)
        return None, False

    # Выдача подтверждается фактом (срок в панели), а не отсутствием исключения.
    problem = _grant_problem(sub, target)
    if problem:
        logger.error("Заказ #%s: %s", order.id, problem)
        await _record_grant_failure(session, order, target, PanelError(problem))
        return sub, False

    order.granted_at = datetime.now(timezone.utc)
    order.grant_target_at = target
    order.grant_last_error = ""
    await session.flush()

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
