"""Веб-админ-панель: заказы, пользователи, статистика, ноды, рассылка.

Доступ: пароль из ``ADMIN_PANEL_PASSWORD`` → подписанная cookie.
Панель выключена, пока пароль не задан.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import String, cast, func, or_, select

from app.config import get_settings
from app.db.models import Event, Order, Plan, Subscription, User
from app.db.session import SessionMaker
from app.panels.registry import registry
from app.services import notifications, orders as orders_service, stats, subscriptions
from app.web import security

logger = logging.getLogger(__name__)
settings = get_settings()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(prefix="/admin")

STATUS_LABELS = {
    "trial": "🎁 пробный",
    "active": "✅ активна",
    "expired": "⌛️ истекла",
    "blocked": "⛔️ заблокирована",
    None: "— нет —",
}


# --------------------------------------------------------------------- доступ
def _redirect_if_unauthorized(request: Request) -> RedirectResponse | None:
    if not security.panel_enabled():
        return None
    if not security.verify_session(request.cookies.get(security.COOKIE_NAME)):
        return RedirectResponse("/admin/login", status_code=303)
    return None


def _page(request: Request, name: str, **context) -> HTMLResponse:
    context.setdefault("message", request.query_params.get("message", ""))
    context.setdefault("error", request.query_params.get("error", ""))
    return templates.TemplateResponse(request, name, context)


async def _notify(request: Request, tg_id: int, text: str) -> None:
    bot: Bot | None = getattr(request.app.state, "bot", None)
    if bot is None:
        return
    try:
        await bot.send_message(tg_id, text)
    except Exception as exc:  # noqa: BLE001 - пользователь мог заблокировать бота
        logger.warning("Не смог отправить сообщение %s: %s", tg_id, exc)


# ---------------------------------------------------------------------- вход
@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if security.panel_enabled() and security.verify_session(request.cookies.get(security.COOKIE_NAME)):
        return RedirectResponse("/admin", status_code=303)
    return _page(request, "login.html", enabled=security.panel_enabled())


@router.post("/login")
async def login_submit(request: Request, password: str = Form("")):
    client_key = request.client.host if request.client else "unknown"
    if security.login_throttle.blocked(client_key):
        return RedirectResponse("/admin/login?error=Слишком много попыток, подожди 10 минут", status_code=303)

    if not security.check_password(password):
        security.login_throttle.register_failure(client_key)
        return RedirectResponse("/admin/login?error=Неверный пароль", status_code=303)

    security.login_throttle.reset(client_key)
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        security.COOKIE_NAME,
        security.issue_session(),
        httponly=True,
        samesite="lax",
        max_age=settings.admin_session_hours * 3600,
    )
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(security.COOKIE_NAME)
    return response


# ------------------------------------------------------------------ дашборд
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if (redirect := _redirect_if_unauthorized(request)):
        return redirect

    async with SessionMaker() as session:
        snapshot = await stats.collect(session)
        pending = await orders_service.pending_orders(session, limit=8)
        rows = []
        for order in pending:
            user = await session.get(User, order.user_id)
            plan = await session.get(Plan, order.plan_id) if order.plan_id else None
            rows.append({"order": order, "user": user, "plan": plan})
        recent_events = (
            await session.scalars(select(Event).order_by(Event.id.desc()).limit(12))
        ).all()

    return _page(
        request,
        "dashboard.html",
        stats=snapshot,
        pending=rows,
        events=recent_events,
        panel_ok=await _panel_health(),
        autopay_enabled=settings.autopay_enabled,
    )


async def _panel_health() -> bool:
    try:
        return await registry.primary().health()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Панель недоступна: %s", exc)
        return False


# ------------------------------------------------------------------ заказы
@router.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request, status: str = "pending"):
    if (redirect := _redirect_if_unauthorized(request)):
        return redirect

    async with SessionMaker() as session:
        stmt = select(Order).order_by(Order.id.desc()).limit(200)
        if status != "all":
            stmt = stmt.where(Order.status == status)
        orders_list = (await session.scalars(stmt)).all()

        rows = []
        for order in orders_list:
            user = await session.get(User, order.user_id)
            plan = await session.get(Plan, order.plan_id) if order.plan_id else None
            rows.append({"order": order, "user": user, "plan": plan})

        snapshot = await stats.collect(session)

    return _page(request, "orders.html", rows=rows, current_status=status, stats=snapshot)


@router.post("/orders/{order_id}/confirm")
async def order_confirm(order_id: int, request: Request):
    if (redirect := _redirect_if_unauthorized(request)):
        return redirect

    async with SessionMaker() as session:
        order = await session.get(Order, order_id)
        if order is None or order.status != "pending":
            return RedirectResponse("/admin/orders?error=Заказ не найден или уже обработан", status_code=303)
        user = await session.get(User, order.user_id)
        try:
            sub, already = await orders_service.mark_paid(
                session, order, registry.primary(), confirmed_by=0
            )
        except Exception as exc:  # noqa: BLE001 - панель могла отвалиться
            logger.error("Подтверждение заказа %s не удалось: %s", order_id, exc)
            await session.rollback()
            return RedirectResponse(f"/admin/orders?error=Панель не выдала доступ: {exc}", status_code=303)
        await session.commit()

        if user is not None and sub is not None and not already:
            await _notify(
                request,
                user.tg_id,
                f"✅ Оплата получена! Подписка активна до {sub.expires_at:%d.%m.%Y %H:%M}.\n\n"
                f"Ссылка-подписка: {subscriptions.subscription_link(sub.subscription_token)}",
            )

    return RedirectResponse(f"/admin/orders?message=Заказ #{order_id} подтверждён", status_code=303)


@router.post("/orders/{order_id}/reject")
async def order_reject(order_id: int, request: Request):
    if (redirect := _redirect_if_unauthorized(request)):
        return redirect

    async with SessionMaker() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return RedirectResponse("/admin/orders?error=Заказ не найден", status_code=303)
        user = await session.get(User, order.user_id)
        await orders_service.cancel_order(session, order, reason="rejected in admin panel")
        await session.commit()

        if user is not None:
            await _notify(request, user.tg_id, f"Заказ #{order_id} отклонён. Если оплата прошла — напиши в поддержку.")

    return RedirectResponse(f"/admin/orders?message=Заказ #{order_id} отклонён", status_code=303)


# ------------------------------------------------------------- пользователи
@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, q: str = "", status: str = "all"):
    if (redirect := _redirect_if_unauthorized(request)):
        return redirect

    async with SessionMaker() as session:
        stmt = (
            select(User, Subscription)
            .outerjoin(Subscription, Subscription.user_id == User.id)
            .order_by(User.id.desc())
            .limit(200)
        )
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(User.username.ilike(like), User.first_name.ilike(like), cast(User.tg_id, String).like(like))
            )
        # execute (а не scalars): выбираем две сущности — нужны кортежи
        rows = (await session.execute(stmt)).all()

        items = []
        for user, sub in rows:
            label = STATUS_LABELS.get(sub.status if sub else None, "—")
            if status != "all":
                if status == "none" and sub is not None:
                    continue
                if status != "none" and (sub is None or sub.status != status):
                    continue
            items.append(
                {
                    "user": user,
                    "sub": sub,
                    "status_label": label,
                    "link": subscriptions.subscription_link(sub.subscription_token) if sub else "",
                }
            )
        snapshot = await stats.collect(session)

    return _page(request, "users.html", items=items, q=q, current_status=status, stats=snapshot)


@router.post("/users/{user_id}/grant")
async def user_grant(user_id: int, request: Request, days: int = Form(0)):
    if (redirect := _redirect_if_unauthorized(request)):
        return redirect
    if days == 0:
        return RedirectResponse("/admin/users?error=Укажи число дней", status_code=303)

    async with SessionMaker() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users?error=Пользователь не найден", status_code=303)
        try:
            sub = await subscriptions.extend_days(session, user, days, registry.primary(), reason="admin_panel")
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            return RedirectResponse(f"/admin/users?error=Панель недоступна: {exc}", status_code=303)
        await session.commit()
        if sub is None:
            return RedirectResponse("/admin/users?error=У пользователя нет подписки", status_code=303)
        await _notify(request, user.tg_id, f"🎁 Тебе начислено {days} дн. доступа. Спасибо, что с нами!")

    return RedirectResponse(f"/admin/users?message={user.display_name}: доступ продлён на {days} дн.", status_code=303)


@router.post("/users/{user_id}/block")
async def user_block(user_id: int, request: Request, block: int = Form(1)):
    if (redirect := _redirect_if_unauthorized(request)):
        return redirect

    async with SessionMaker() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users?error=Пользователь не найден", status_code=303)
        user.is_blocked = bool(block)
        sub = await subscriptions.get_subscription(session, user.id)
        if sub is not None:
            await subscriptions.set_enabled(sub, registry.primary(), enabled=not block)
        await session.commit()

    action = "заблокирован" if block else "разблокирован"
    return RedirectResponse(f"/admin/users?message={user.display_name} {action}", status_code=303)


# -------------------------------------------------------------------- ноды
@router.post("/autopay/run")
async def autopay_run(request: Request):
    """Разовый прогон автопроверки выписки — не ждать планировщика."""
    if (redirect := _redirect_if_unauthorized(request)):
        return redirect
    if not settings.autopay_enabled:
        return RedirectResponse("/admin?error=Автоплатёж выключен: поставь AUTOPAY_ENABLED=true", status_code=303)

    from app.services import autopay

    bot = getattr(request.app.state, "bot", None)
    try:
        async with SessionMaker() as session:
            result = await autopay.reconcile(session, registry.primary(), bot)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - показываем ошибку админу, а не 500
        logger.exception("Автопроверка из панели упала")
        return RedirectResponse(f"/admin?error=Автопроверка упала: {exc}", status_code=303)

    message = f"Проверка выписки: {result.as_text()}"
    if result.errors:
        return RedirectResponse(f"/admin?error={message}; ошибки: {'; '.join(result.errors[:3])}", status_code=303)
    return RedirectResponse(f"/admin?message={message}", status_code=303)


@router.get("/nodes", response_class=HTMLResponse)
async def nodes_page(request: Request):
    if (redirect := _redirect_if_unauthorized(request)):
        return redirect

    async with SessionMaker() as session:
        panels = []
        for panel in await registry.all_panels(session):
            entry = {"name": panel.name, "ok": False, "inbounds": [], "error": ""}
            try:
                entry["ok"] = await panel.health()
                entry["inbounds"] = await panel.list_inbounds()
            except Exception as exc:  # noqa: BLE001
                entry["error"] = str(exc)
            panels.append(entry)

        active = await session.scalar(
            select(func.count(Subscription.id)).where(Subscription.status.in_(["trial", "active"]))
        )
        total_users = await session.scalar(select(func.count(User.id)))

    return _page(request, "nodes.html", panels=panels, active=active or 0, total_users=total_users or 0)


# ----------------------------------------------------------------- рассылка
@router.post("/broadcast")
async def broadcast(request: Request, text: str = Form(""), audience: str = Form("active")):
    if (redirect := _redirect_if_unauthorized(request)):
        return redirect
    text = text.strip()
    if not text:
        return RedirectResponse("/admin?error=Пустой текст рассылки", status_code=303)

    bot: Bot | None = getattr(request.app.state, "bot", None)
    if bot is None:
        return RedirectResponse("/admin?error=Бот не подключён к панели", status_code=303)

    asyncio.create_task(_broadcast_task(bot, text, audience))
    return RedirectResponse(f"/admin?message=Рассылка запущена ({audience})", status_code=303)


async def _broadcast_task(bot: Bot, text: str, audience: str) -> None:
    """Рассылка в фоне: не блокируем HTTP-ответ и не роняем панель при ошибке."""
    now = datetime.now(timezone.utc)
    async with SessionMaker() as session:
        stmt = select(User).where(User.is_blocked.is_(False))
        if audience == "active":
            stmt = stmt.join(Subscription, Subscription.user_id == User.id).where(
                Subscription.status.in_(["trial", "active"]),
                Subscription.expires_at > now,
            )
        elif audience == "expired":
            stmt = stmt.join(Subscription, Subscription.user_id == User.id).where(
                Subscription.status == "expired"
            )
        users = (await session.scalars(stmt)).all()

    sent = failed = 0
    for user in users:
        try:
            await bot.send_message(user.tg_id, text, disable_web_page_preview=True)
            sent += 1
        except Exception:  # noqa: BLE001
            failed += 1
        await asyncio.sleep(0.05)  # бережём лимиты Telegram
    await notifications.notify_admins(bot, f"📣 Рассылка завершена: доставлено {sent}, ошибок {failed}")
