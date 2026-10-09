"""Поиск для командной палитры (⌘K).

Один запрос — три вида результата: разделы панели, клиенты и заказы. Смысл в
том, чтобы не ходить по меню: «клиент 8300512489», «заказ 47», «ноды» — и ты
там же. Разделы отдаёт шаблон (их список зависит от прав роли), а этот модуль
ищет данные и уважает те же права: поддержка не увидит заказов.

Ответ — JSON, потому что палитра рисуется на клиенте и не должна перезагружать
страницу ради каждой буквы.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import String, cast, or_, select

from app.db.models import Order, Subscription, User
from app.db.session import SessionMaker
from app.web import ui
from app.web.admin.common import require

logger = logging.getLogger(__name__)
router = APIRouter()

#: Сколько строк каждого вида показывать. Больше — уже не палитра, а список.
USER_LIMIT = 6
ORDER_LIMIT = 4
#: Короче двух символов — это ещё не запрос: вернём только разделы.
MIN_QUERY = 2


def _order_label(provider: str) -> str:
    return ui.PROVIDER_LABELS.get(provider, provider or "—")


async def _find_users(db, query: str, limit: int = USER_LIMIT) -> list[dict[str, str]]:
    like = f"%{query}%"
    digits = "".join(ch for ch in query if ch.isdigit())
    conditions = [
        User.username.ilike(like),
        User.first_name.ilike(like),
        cast(User.tg_id, String).like(f"%{digits}%") if digits else User.tg_id.is_(None),
    ]
    if digits:
        conditions.append(cast(User.id, String) == digits)
    rows = (
        await db.execute(
            select(User, Subscription)
            .outerjoin(Subscription, Subscription.user_id == User.id)
            .where(or_(*conditions))
            .order_by(User.id.desc())
            .limit(limit)
        )
    ).all()

    items: list[dict[str, str]] = []
    for user, sub in rows:
        status = ui.SUBSCRIPTION_STATUS.get(sub.status if sub else "", ("без подписки", "neutral"))[0]
        parts = [str(user.tg_id)]
        if user.username:
            parts.append(f"@{user.username}")
        parts.append(f"#{user.id}")
        if sub:
            parts.append(status)
        items.append(
            {
                "kind": "user",
                "title": user.display_name,
                "sub": " · ".join(parts),
                "url": f"/admin/users/{user.id}",
                "icon": "users",
            }
        )
    return items


async def _find_orders(db, query: str, limit: int = ORDER_LIMIT) -> list[dict[str, str]]:
    digits = "".join(ch for ch in query if ch.isdigit())
    conditions = [Order.external_id.ilike(f"%{query}%"), Order.comment.ilike(f"%{query}%")]
    if digits:
        conditions.append(cast(Order.id, String) == digits)
        conditions.append(cast(Order.id, String).like(f"%{digits}%"))
        conditions.append(cast(Order.amount_rub, String).like(f"%{digits}%"))
        conditions.append(cast(Order.pay_kopecks, String).like(f"%{digits}%"))
    rows = (
        await db.scalars(select(Order).where(or_(*conditions)).order_by(Order.id.desc()).limit(limit))
    ).all()

    items: list[dict[str, str]] = []
    for order in rows:
        user = await db.get(User, order.user_id)
        status = ui.ORDER_STATUS.get(order.status, (order.status, "neutral"))[0]
        items.append(
            {
                "kind": "order",
                "title": f"Заказ #{order.id} · {order.pay_amount_text} ₽",
                "sub": f"{user.display_name if user else '—'} · {status} · {_order_label(order.provider)}",
                "url": f"/admin/orders?q={order.id}",
                "icon": "inbox",
            }
        )
    return items


@router.get("/search")
async def search(request: Request, q: str = ""):
    """Найти клиентов и заказы для палитры. Разделы добавляет клиент."""
    auth = await require(request)
    if isinstance(auth, Response):
        return auth

    query = (q or "").strip()
    items: list[dict[str, str]] = []
    # Цифры ищем даже одной цифрой: «5» — это осмысленный номер заказа.
    if len(query) >= MIN_QUERY or query.isdigit():
        async with SessionMaker() as db:
            if ui.can(auth.role, "users.view"):
                items.extend(await _find_users(db, query))
            if ui.can(auth.role, "orders.view"):
                items.extend(await _find_orders(db, query))

    return JSONResponse({"q": query, "items": items})
