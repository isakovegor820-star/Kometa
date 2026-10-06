"""Выгрузки CSV: заказы, клиенты и журнал действий.

Зачем отдельный модуль: выгрузка — это не страница, а файл. У неё другой
контракт (заголовок `Content-Disposition`, кодировка с BOM, разделитель `;`),
и смешивать её с рендером HTML не стоит.

Фильтры повторяют страницы панели один в один: модератор настраивает выборку
в интерфейсе и ожидает в файле ровно те же строки. Формат самого CSV живёт в
:mod:`app.services.exporting` — один на все отчёты, чтобы файлы одинаково
открывались в русском Excel.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timezone

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import String, cast, or_, select

from app.db.models import Event, Order, Plan, Subscription, User
from app.db.session import SessionMaker
from app.services import audit, exporting, subscriptions
from app.web.admin.common import filters, require

logger = logging.getLogger(__name__)
router = APIRouter()

ORDER_FILTER_KEYS = ("q", "status", "provider", "kind", "date_from", "date_to")
USER_FILTER_KEYS = ("q", "status", "tag")
AUDIT_FILTER_KEYS = ("actor", "kind", "date_from", "date_to")

#: Статусы заказов — те же вкладки, что на странице очереди.
ORDER_STATUSES = ("pending", "paid", "refunded", "canceled", "expired", "all")
#: Статусы клиентов — как на странице «Пользователи».
USER_STATUSES = ("all", "trial", "active", "expired", "blocked", "none")

#: Потолок выгрузки: файл «всё за всё время» не должен съесть память процесса.
#: Если строк больше — выгружаем последние: остальное доступно фильтрами.
EXPORT_LIMIT = 20_000

#: Заголовок «не кэшировать»: в выгрузке персональные данные и ссылки-подписки,
#: им не место в кэше прокси или браузера.
NO_STORE = {"Cache-Control": "no-store"}


def _parse_date(value: str, *, end: bool = False) -> datetime | None:
    """Дата из формы (YYYY-MM-DD) → момент UTC; для «по» — конец дня."""
    if not value:
        return None
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
    moment = datetime.combine(day, time.max if end else time.min)
    return moment.replace(tzinfo=timezone.utc)


def _csv_response(body: bytes, prefix: str) -> Response:
    """Отдать файл: имя с датой, чтобы две выгрузки не перетирали друг друга."""
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{exporting.filename(prefix)}"',
            **NO_STORE,
        },
    )


async def _log_export(db, auth, kind: str, current: dict[str, str], rows: int) -> None:  # noqa: ANN001 - Session
    """Каждая выгрузка попадает в журнал: видно, кто и что скачал."""
    await audit.log_action(
        db,
        "admin.export",
        actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
        payload={"kind": kind, "filters": {key: value for key, value in current.items() if value}, "rows": rows},
    )
    await db.commit()


# -------------------------------------------------------------------- заказы
def _search_condition(query: str):  # noqa: ANN202 - SQLAlchemy expression
    """Поиск как в очереди: номер, сумма, копейки, внешний id, промокод, клиент."""
    like = f"%{query.strip()}%"
    user_ids = select(User.id).where(
        or_(
            User.username.ilike(like),
            User.first_name.ilike(like),
            cast(User.tg_id, String).like(like),
        )
    )
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
        conditions.append(cast(Order.pay_kopecks, String).like(f"%{digits}%"))
    return or_(*conditions)


@router.get("/export/orders.csv")
async def export_orders(request: Request):
    auth = await require(request, "finance.export")
    if isinstance(auth, Response):
        return auth

    current = filters(request, ORDER_FILTER_KEYS)
    # Как на странице: без явного статуса выгружаем очередь, а не весь архив.
    status = current["status"] or "pending"
    if status not in ORDER_STATUSES:
        status = "pending"

    async with SessionMaker() as db:
        stmt = select(Order)
        if status != "all":
            stmt = stmt.where(Order.status == status)
        if current["q"]:
            stmt = stmt.where(_search_condition(current["q"]))
        if current["provider"]:
            stmt = stmt.where(Order.provider == current["provider"])
        if current["kind"]:
            stmt = stmt.where(Order.kind == current["kind"])
        date_from = _parse_date(current["date_from"])
        date_to = _parse_date(current["date_to"], end=True)
        if date_from:
            stmt = stmt.where(Order.created_at >= date_from)
        if date_to:
            stmt = stmt.where(Order.created_at <= date_to)

        orders_list = list(
            (await db.scalars(stmt.order_by(Order.created_at.desc()).limit(EXPORT_LIMIT))).all()
        )
        rows = await _load_rows(db, orders_list)
        body = exporting.orders_csv([(row["order"], row["user"], row["plan"]) for row in rows])
        await _log_export(db, auth, "orders", {**current, "status": status}, len(rows))

    return _csv_response(body, "orders")


async def _load_rows(db, orders_list: list[Order]) -> list[dict]:  # noqa: ANN001 - AsyncSession
    """Клиенты и тарифы одним запросом на список, а не на строку."""
    if not orders_list:
        return []
    user_ids = {order.user_id for order in orders_list}
    plan_ids = {order.plan_id for order in orders_list if order.plan_id}
    users = {user.id: user for user in await db.scalars(select(User).where(User.id.in_(user_ids)))}
    plans = {plan.id: plan for plan in await db.scalars(select(Plan).where(Plan.id.in_(plan_ids)))} if plan_ids else {}
    return [
        {"order": order, "user": users.get(order.user_id), "plan": plans.get(order.plan_id)}
        for order in orders_list
    ]


# --------------------------------------------------------------- пользователи
@router.get("/export/users.csv")
async def export_users(request: Request):
    auth = await require(request, "finance.export")
    if isinstance(auth, Response):
        return auth

    current = filters(request, USER_FILTER_KEYS)
    status = current["status"] or "all"
    if status not in USER_STATUSES:
        status = "all"

    async with SessionMaker() as db:
        stmt = select(User, Subscription).outerjoin(Subscription, Subscription.user_id == User.id)
        if current["q"]:
            like = f"%{current['q'].strip()}%"
            digits = "".join(ch for ch in current["q"] if ch.isdigit())
            conditions = [
                User.username.ilike(like),
                User.first_name.ilike(like),
                cast(User.tg_id, String).like(like),
            ]
            if digits:
                conditions.append(cast(User.id, String) == digits)
            stmt = stmt.where(or_(*conditions))
        if current["tag"]:
            stmt = stmt.where(User.tags.ilike(f"%{current['tag']}%"))
        if status == "blocked":
            stmt = stmt.where(User.is_blocked.is_(True))
        elif status == "none":
            stmt = stmt.where(Subscription.id.is_(None))
        elif status != "all":
            stmt = stmt.where(Subscription.status == status)

        pairs = list((await db.execute(stmt.order_by(User.id.desc()).limit(EXPORT_LIMIT))).all())
        # Ссылку-подписку собирает панель: выгрузка не знает про её настройки.
        body = exporting.users_csv(pairs, link_builder=subscriptions.subscription_link)
        await _log_export(db, auth, "users", {**current, "status": status}, len(pairs))

    return _csv_response(body, "users")


# --------------------------------------------------------------------- аудит
@router.get("/export/audit.csv")
async def export_audit(request: Request):
    auth = await require(request, "finance.export")
    if isinstance(auth, Response):
        return auth

    current = filters(request, AUDIT_FILTER_KEYS)

    async with SessionMaker() as db:
        stmt = select(Event).where(Event.kind.like(f"{audit.ACTION_PREFIX}%"))
        if current["actor"]:
            stmt = stmt.where(Event.actor_name == current["actor"])
        if current["kind"]:
            stmt = stmt.where(Event.kind == current["kind"])
        date_from = _parse_date(current["date_from"])
        date_to = _parse_date(current["date_to"], end=True)
        if date_from:
            stmt = stmt.where(Event.created_at >= date_from)
        if date_to:
            stmt = stmt.where(Event.created_at <= date_to)

        events = list((await db.scalars(stmt.order_by(Event.id.desc()).limit(EXPORT_LIMIT))).all())
        body = exporting.audit_csv(events)
        await _log_export(db, auth, "audit", current, len(events))

    return _csv_response(body, "audit")
