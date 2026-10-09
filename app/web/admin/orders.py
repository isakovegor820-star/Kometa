"""Очередь заказов: подтверждение оплат, отклонение, возвраты, массовые действия.

Главный сценарий модератора — «утром пришло N переводов». Поэтому страница
построена вокруг очереди: сверху счётчики по статусам, в центре список с
поиском по сумме/копейкам/клиенту, снизу — панель массовых действий.
Каждое действие пишется в журнал: видно, кто подтвердил и кто вернул деньги.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, time, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import String, and_, cast, func, or_, select

from app.db.models import Order, Plan, User
from app.db.session import SessionMaker
from app.services import audit, orders as orders_service, stats as stats_service, subscriptions
from app.web.admin.common import filters, flash_redirect, make_page, page, parse_page, require

logger = logging.getLogger(__name__)
router = APIRouter()

#: Статусы, которые показываем вкладками. Порядок = порядок работы модератора.
TABS: tuple[tuple[str, str], ...] = (
    ("pending", "Ждут оплаты"),
    ("paid", "Оплаченные"),
    ("refunded", "Возвраты"),
    ("canceled", "Отменённые"),
    ("expired", "Просроченные"),
    ("all", "Все"),
)

FILTER_KEYS = ("q", "status", "provider", "kind", "date_from", "date_to", "sort")


def _parse_date(value: str, *, end: bool = False) -> datetime | None:
    """Дата из формы (YYYY-MM-DD) → момент времени UTC.

    Для «по» берём конец дня: иначе фильтр «с 1 по 1 октября» показывает пусто.
    """
    if not value:
        return None
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
    moment = datetime.combine(day, time.max if end else time.min)
    return moment.replace(tzinfo=timezone.utc)


def _search_condition(query: str):  # noqa: ANN202 - SQLAlchemy expression
    """Поиск по заказам: номер, сумма, копейки, внешний id, промокод и клиент.

    Модератор ищет «пришло 199.13» — значит сумма и копейки должны искаться
    так же легко, как номер заказа.
    """
    like = f"%{query.strip()}%"
    user_ids = select(User.id).where(
        or_(
            User.username.ilike(like),
            User.first_name.ilike(like),
            cast(User.tg_id, String).like(like),
        )
    )
    # «199.13» — это пара «сумма + копейки»: ищем точное совпадение, иначе
    # модератор, сверяя перевод, получал бы список всех заказов на 199 ₽.
    amount = re.fullmatch(r"(\d{1,7})[.,](\d{1,2})", query.strip())
    if amount:
        conditions_extra = [
            and_(Order.amount_rub == int(amount.group(1)), Order.pay_kopecks == int(amount.group(2)))
        ]
    else:
        conditions_extra = []

    digits = "".join(ch for ch in query if ch.isdigit())
    conditions = [
        Order.external_id.ilike(like),
        Order.comment.ilike(like),
        Order.promo_code.ilike(like),
        Order.user_id.in_(user_ids),
    ]
    if digits:
        conditions.append(cast(Order.id, String).like(f"%{digits}%"))
        conditions.append(cast(Order.amount_rub, String).like(f"%{digits}%"))
    return or_(*conditions, *conditions_extra)


async def _load_rows(db, orders_list: list[Order]) -> list[dict]:  # noqa: ANN001 - AsyncSession
    """Подтянуть клиентов и тарифы одним запросом на список, а не на строку."""
    if not orders_list:
        return []
    user_ids = {order.user_id for order in orders_list}
    plan_ids = {order.plan_id for order in orders_list if order.plan_id}
    users = {u.id: u for u in await db.scalars(select(User).where(User.id.in_(user_ids)))}
    plans = {p.id: p for p in await db.scalars(select(Plan).where(Plan.id.in_(plan_ids)))} if plan_ids else {}
    return [
        {"order": order, "user": users.get(order.user_id), "plan": plans.get(order.plan_id)}
        for order in orders_list
    ]


async def _status_counts(db) -> dict[str, int]:  # noqa: ANN001
    rows = (await db.execute(select(Order.status, func.count(Order.id)).group_by(Order.status))).all()
    counts = {status: int(total) for status, total in rows}
    counts["all"] = sum(counts.values())
    return counts


@router.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request):
    auth = await require(request, "orders.view")
    if isinstance(auth, Response):
        return auth

    current = filters(request, FILTER_KEYS)
    status = current["status"] or "pending"
    if status not in {code for code, _ in TABS}:
        status = "pending"
    page_no, per_page = parse_page(request)

    async with SessionMaker() as db:
        base = select(Order)
        if status != "all":
            base = base.where(Order.status == status)
        if current["q"]:
            base = base.where(_search_condition(current["q"]))
        if current["provider"]:
            base = base.where(Order.provider == current["provider"])
        if current["kind"]:
            base = base.where(Order.kind == current["kind"])
        date_from = _parse_date(current["date_from"])
        date_to = _parse_date(current["date_to"], end=True)
        if date_from:
            base = base.where(Order.created_at >= date_from)
        if date_to:
            base = base.where(Order.created_at <= date_to)

        subset = base.subquery()
        totals = (
            await db.execute(
                select(func.coalesce(func.sum(subset.c.amount_rub), 0), func.count(subset.c.id))
            )
        ).one()
        total = int(totals[1] or 0)
        sort = current["sort"] or "new"
        order_by = {
            "new": Order.created_at.desc(),
            "old": Order.created_at.asc(),
            "amount": Order.amount_rub.desc(),
            "status": Order.status.asc(),
        }.get(sort, Order.created_at.desc())
        rows_raw = list(
            (await db.scalars(base.order_by(order_by).limit(per_page).offset((page_no - 1) * per_page))).all()
        )
        rows = await _load_rows(db, rows_raw)
        counts = await _status_counts(db)
        snapshot = await stats_service.collect(db)
        providers = list(
            (await db.scalars(select(Order.provider).group_by(Order.provider).order_by(Order.provider))).all()
        )

    return await page(
        request,
        "orders.html",
        auth,
        title="Очередь заказов",
        page="orders",
        rows=rows,
        tabs=TABS,
        current=current,
        status=status,
        counts=counts,
        page_data=make_page(rows, total, page_no, per_page),
        filter_total_rub=int(totals[0] or 0),
        filter_total_orders=int(totals[1] or 0),
        stats=snapshot,
        providers=providers,
    )


@router.post("/orders/{order_id}/confirm")
async def order_confirm(order_id: int, request: Request):
    auth = await require(request, "orders.act")
    if isinstance(auth, Response):
        return auth

    from app.web.admin.common import notify

    async with SessionMaker() as db:
        order = await db.get(Order, order_id)
        if order is None or order.status != "pending":
            return flash_redirect("/admin/orders", error="Заказ не найден или уже обработан")
        user = await db.get(User, order.user_id)
        if user is not None and user.is_blocked:
            return flash_redirect(
                "/admin/orders",
                error=f"Клиент {user.display_name} заблокирован. Сначала разблокируй его в карточке клиента.",
            )

        bot = getattr(request.app.state, "bot", None)
        try:
            sub, already = await orders_service.mark_paid(
                db,
                order,
                await subscriptions.all_user_panels(db),
                confirmed_by=auth.tg_id or 0,
                bot=bot,
            )
        except Exception as exc:  # noqa: BLE001 - панель могла отвалиться
            logger.error("Подтверждение заказа %s не удалось: %s", order_id, exc)
            await db.rollback()
            return flash_redirect("/admin/orders", error=f"Панель не выдала доступ: {exc}")

        if sub is None and not already:
            # Оплата зафиксирована, выдача не подтвердилась: заказ подхватит
            # фоновая задача. Говорим это прямо, чтобы админ не искал «где доступ».
            await audit.log_action(
                db,
                "admin.order_confirm",
                actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
                user_id=order.user_id,
                payload={"order_id": order_id, "amount": order.amount_rub, "grant_pending": True},
            )
            await db.commit()
            return flash_redirect(
                "/admin/orders",
                message=(
                    f"Заказ #{order_id} подтверждён, оплата зафиксирована. "
                    f"Панель не подтвердила выдачу ({order.grant_last_error or 'нет ответа'}) — "
                    "доступ выдастся автоматически в течение нескольких минут."
                ),
            )

        await audit.log_action(
            db,
            "admin.order_confirm",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            user_id=order.user_id,
            payload={"order_id": order_id, "amount": order.amount_rub, "already": already},
        )
        await db.commit()
        user_name = user.display_name if user else f"id{order.user_id}"
        expires = sub.expires_at if sub else None

    if user is not None and sub is not None and not already:
        text = f"✅ Оплата получена! Подписка активна до {expires:%d.%m.%Y %H:%M}.\n\n"
        text += f"Ссылка-подписка: {subscriptions.subscription_link(sub.subscription_token)}"
        await notify(request, user.tg_id, text)

    return flash_redirect("/admin/orders", message=f"Заказ #{order_id} подтверждён — доступ у {user_name}")


@router.post("/orders/{order_id}/reject")
async def order_reject(order_id: int, request: Request, reason: str = Form("")):
    auth = await require(request, "orders.act")
    if isinstance(auth, Response):
        return auth

    from app.web.admin.common import notify

    async with SessionMaker() as db:
        order = await db.get(Order, order_id)
        if order is None:
            return flash_redirect("/admin/orders", error="Заказ не найден")
        user = await db.get(User, order.user_id)
        await orders_service.cancel_order(db, order, reason=reason or "отклонён в панели")
        await audit.log_action(
            db,
            "admin.order_reject",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            user_id=order.user_id,
            payload={"order_id": order_id, "reason": reason},
        )
        await db.commit()

    if user is not None:
        text = f"Заказ #{order_id} отклонён."
        if reason:
            text += f"\nПричина: {reason}"
        text += "\nЕсли оплата прошла — напиши в поддержку, разберёмся."
        await notify(request, user.tg_id, text)

    return flash_redirect("/admin/orders", message=f"Заказ #{order_id} отклонён")


@router.post("/orders/{order_id}/refund")
async def order_refund(order_id: int, request: Request, note: str = Form("")):
    auth = await require(request, "orders.refund")
    if isinstance(auth, Response):
        return auth

    from app.web.admin.common import notify

    async with SessionMaker() as db:
        order = await db.get(Order, order_id)
        if order is None:
            return flash_redirect("/admin/refunds", error="Заказ не найден")
        user = await db.get(User, order.user_id)
        try:
            ok, message, _sub = await orders_service.refund_order(
                db,
                order,
                await subscriptions.all_user_panels(db),
                actor=auth.name,
                note=note,
            )
        except Exception as exc:  # noqa: BLE001 - панель может не ответить
            logger.error("Возврат по заказу %s не удался: %s", order_id, exc)
            await db.rollback()
            return flash_redirect("/admin/refunds", error=f"Возврат не оформлен: {exc}")

        if not ok:
            await db.rollback()
            return flash_redirect("/admin/refunds", error=message)

        await audit.log_action(
            db,
            "admin.order_refund",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            user_id=order.user_id,
            payload={"order_id": order_id, "amount": order.amount_rub, "note": note},
        )
        await db.commit()
        tg_id = user.tg_id if user else None

    if tg_id:
        await notify(
            request,
            tg_id,
            f"По заказу #{order_id} оформлен возврат {order.amount_rub} ₽. "
            "Доступ по этому заказу отключён. Если это ошибка — напиши в поддержку.",
        )
    return flash_redirect("/admin/refunds", message=message)


@router.get("/refunds", response_class=HTMLResponse)
async def refunds_page(request: Request):
    auth = await require(request, "orders.refund")
    if isinstance(auth, Response):
        return auth

    page_no, per_page = parse_page(request)
    async with SessionMaker() as db:
        paid = (
            await db.scalars(
                select(Order)
                .where(Order.status == "paid", Order.paid_at.is_not(None))
                .order_by(Order.paid_at.desc())
                .limit(per_page)
                .offset((page_no - 1) * per_page)
            )
        ).all()
        total = int(await db.scalar(select(func.count(Order.id)).where(Order.status == "paid")) or 0)
        rows = await _load_rows(db, list(paid))

        refunded = (
            await db.scalars(
                select(Order)
                .where(Order.status == "refunded")
                .order_by(Order.refunded_at.desc().nullslast())
                .limit(50)
            )
        ).all()
        refund_rows = await _load_rows(db, list(refunded))
        fn = getattr(stats_service, "refund_summary", None)
        summary = await fn(db, days=30) if fn else {"count": 0, "rub": 0}

    return await page(
        request,
        "refunds.html",
        auth,
        title="Возвраты",
        page="refunds",
        rows=rows,
        refund_rows=refund_rows,
        summary=summary,
        page_data=make_page(rows, total, page_no, per_page),
    )


@router.post("/orders/bulk")
async def orders_bulk(request: Request, action: str = Form("confirm"), note: str = Form("")):
    """Массовое действие над выбранными заказами.

    Обрабатываем по одному и собираем итог: одна упавшая панель не должна
    отменять остальные подтверждения.
    """
    auth = await require(request, "orders.act")
    if isinstance(auth, Response):
        return auth

    form = await request.form()
    ids = [int(value) for value in form.getlist("ids") if str(value).isdigit()]
    if not ids:
        return flash_redirect("/admin/orders", error="Ничего не выбрано")
    if action == "refund":
        from app.web.ui import can

        if not can(auth.role, "orders.refund"):
            return flash_redirect("/admin/orders", error="Возврат доступен владельцу и модератору")

    from app.web.admin.common import notify

    allowed = {"confirm", "reject", "refund"}
    if action not in allowed:
        return flash_redirect("/admin/orders", error="Неизвестное действие")

    ok_count = 0
    problems: list[str] = []
    bot = getattr(request.app.state, "bot", None)

    async with SessionMaker() as db:
        panels = await subscriptions.all_user_panels(db)
        for order_id in ids:
            order = await db.get(Order, order_id)
            if order is None:
                problems.append(f"#{order_id}: не найден")
                continue
            user = await db.get(User, order.user_id)
            try:
                if action == "confirm":
                    if order.status != "pending":
                        problems.append(f"#{order_id}: статус {order.status}")
                        continue
                    if user is not None and user.is_blocked:
                        problems.append(f"#{order_id}: клиент заблокирован")
                        continue
                    sub, already = await orders_service.mark_paid(
                        db, order, panels, confirmed_by=auth.tg_id or 0, bot=bot
                    )
                    ok_count += 1
                    if user is not None and sub is not None and not already:
                        await notify(
                            request,
                            user.tg_id,
                            f"✅ Оплата получена! Подписка активна до {sub.expires_at:%d.%m.%Y %H:%M}.\n\n"
                            f"Ссылка-подписка: {subscriptions.subscription_link(sub.subscription_token)}",
                        )
                elif action == "reject":
                    if order.status != "pending":
                        problems.append(f"#{order_id}: статус {order.status}")
                        continue
                    await orders_service.cancel_order(db, order, reason=note or "массовое отклонение")
                    ok_count += 1
                    if user is not None:
                        await notify(request, user.tg_id, f"Заказ #{order_id} отклонён.")
                else:  # refund
                    ok, message, _sub = await orders_service.refund_order(
                        db, order, panels, actor=auth.name, note=note
                    )
                    if ok:
                        ok_count += 1
                        if user is not None:
                            await notify(
                                request,
                                user.tg_id,
                                f"По заказу #{order_id} оформлен возврат. Доступ по нему отключён.",
                            )
                    else:
                        problems.append(f"#{order_id}: {message}")
            except Exception as exc:  # noqa: BLE001 - продолжаем остальные
                logger.error("Массовое действие %s по заказу %s упало: %s", action, order_id, exc)
                problems.append(f"#{order_id}: {exc}")
                await db.rollback()

        await audit.log_action(
            db,
            "admin.order_bulk",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            payload={"action": action, "ids": ids, "ok": ok_count, "problems": problems[:5]},
        )
        await db.commit()

    if problems:
        return flash_redirect(
            "/admin/orders",
            error=f"Готово: {ok_count} из {len(ids)}. Проблемы: {'; '.join(problems[:3])}",
        )
    return flash_redirect("/admin/orders", message=f"Готово: {ok_count} заказ(ов) обработано")


@router.post("/orders/expire-stale")
async def expire_stale(request: Request):
    """Закрыть просроченные заявки, не дожидаясь фонового планировщика."""
    auth = await require(request, "orders.act")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        closed = await orders_service.expire_stale_orders(db)
        await audit.log_action(
            db,
            "admin.order_bulk",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            payload={"action": "expire_stale", "count": len(closed)},
        )
        await db.commit()
    if not closed:
        return flash_redirect("/admin/orders", message="Просроченных заявок нет")
    return flash_redirect("/admin/orders", message=f"Закрыто просроченных заявок: {len(closed)}")
