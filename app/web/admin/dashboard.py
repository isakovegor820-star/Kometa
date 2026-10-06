"""Обзор: что происходит сегодня и что требует человека прямо сейчас.

Порядок блоков — по частоте действий модератора, а не по красоте:
сначала «требует внимания» (алерты и очередь оплат), потом деньги и графики.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import Alert, Event, Node, Order, Plan, User
from app.db.session import SessionMaker
from app.panels.registry import registry
from app.services import alerts as alerts_service
from app.services import audit, autopay, orders as orders_service, stats as stats_service
from app.web.admin.common import flash_redirect, page, require

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()


async def _revenue_by_day(db, days: int = 14):  # noqa: ANN001 - AsyncSession
    """Выручка по дням для мини-графика. Нет данных — пустой список, не падаем."""
    fn = getattr(stats_service, "revenue_by_day", None)
    if fn is None:  # pragma: no cover - модуль статистики может быть старее
        return []
    try:
        return await fn(db, days=days)
    except Exception as exc:  # noqa: BLE001 - график не повод ронять обзор
        logger.warning("Не посчитал выручку по дням: %s", exc)
        return []


async def _refund_summary(db, days: int = 30):  # noqa: ANN001
    fn = getattr(stats_service, "refund_summary", None)
    if fn is None:  # pragma: no cover
        return {"count": 0, "rub": 0}
    try:
        return await fn(db, days=days)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не посчитал возвраты: %s", exc)
        return {"count": 0, "rub": 0}


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    auth = await require(request, "orders.view")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        snapshot = await stats_service.collect(db)
        profit = await stats_service.profit_summary(db, days=30)
        channels = await stats_service.channel_economics(db, days=30)
        pending_rows = []
        for order in await orders_service.pending_orders(db, limit=6):
            pending_rows.append(
                {
                    "order": order,
                    "user": await db.get(User, order.user_id),
                    "plan": await db.get(Plan, order.plan_id) if order.plan_id else None,
                }
            )
        alerts = await alerts_service.list_alerts(db, status="open", limit=5)
        alert_counts = await alerts_service.summary(db)
        day_points = await _revenue_by_day(db, days=14)
        refunds = await _refund_summary(db, days=30)
        actions = await audit.recent_actions(db, limit=6)
        nodes = list(await db.scalars(select(Node).order_by(Node.priority, Node.id)))
        recent_events = list((await db.scalars(select(Event).order_by(Event.id.desc()).limit(8))).all())
        trial_today = int(
            await db.scalar(
                select(func.count(User.id)).where(User.created_at >= datetime.now(timezone.utc) - timedelta(days=1))
            )
            or 0
        )

    return await page(
        request,
        "dashboard.html",
        auth,
        title="Дашборд",
        page="dashboard",
        stats=snapshot,
        profit=profit,
        channels=channels[:5],
        pending=pending_rows,
        alerts=alerts,
        alert_counts=alert_counts,
        revenue_days=day_points,
        refunds=refunds,
        actions=actions,
        nodes=nodes,
        events=recent_events,
        new_users_today=trial_today,
        panel_ok=await _panel_health(),
        autopay_enabled=settings.autopay_enabled,
        sales_enabled=settings.sales_enabled,
    )


async def _panel_health() -> bool:
    try:
        return await registry.primary().health()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Панель недоступна: %s", exc)
        return False


# --------------------------------------------------------------- быстрые дела
@router.post("/autopay/run")
async def autopay_run(request: Request):
    """Разовый прогон автопроверки выписки — не ждать планировщика."""
    auth = await require(request, "orders.act")
    if isinstance(auth, Response):
        return auth
    if not settings.autopay_enabled:
        return flash_redirect("/admin", error="Автоплатёж выключен: поставь AUTOPAY_ENABLED=true", query=True)

    bot = getattr(request.app.state, "bot", None)
    try:
        async with SessionMaker() as db:
            result = await autopay.reconcile(db, registry.primary(), bot)
            await audit.log_action(
                db,
                "admin.autopay_run",
                actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
                payload={
                    "fetched": result.fetched,
                    "confirmed": len(result.confirmed),
                    "unmatched": len(result.unmatched),
                    "errors": list(result.errors or [])[:3],
                },
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - показываем ошибку админу, а не 500
        logger.exception("Автопроверка из панели упала")
        return flash_redirect("/admin", error=f"Автопроверка упала: {exc}", query=True)

    message = f"Проверка выписки: {result.as_text()}"
    if result.errors:
        return flash_redirect("/admin", error=f"{message}; ошибки: {'; '.join(result.errors[:3])}", query=True)
    return flash_redirect("/admin", message=message, query=True)


@router.post("/sync")
async def sync_clients(request: Request):
    """Раздать всех клиентов на все активные ноды (после подключения страны)."""
    from app.services import subscriptions

    auth = await require(request, "system.act")
    if isinstance(auth, Response):
        return auth

    try:
        async with SessionMaker() as db:
            panels = await subscriptions.all_user_panels(db)
            checked, failed = await subscriptions.sync_all_subscriptions(db, panels)
            await audit.log_action(
                db,
                "admin.sync_nodes",
                actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
                payload={"checked": checked, "failed": failed},
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Синхронизация нод упала")
        return flash_redirect("/admin", error=f"Синхронизация не прошла: {exc}")

    if failed:
        return flash_redirect(
            "/admin", error=f"Проверено подписок: {checked}. Не ответили ноды: {', '.join(failed)}"
        )
    return flash_redirect("/admin", message=f"Клиенты синхронизированы на все ноды (подписок: {checked})")


@router.post("/watch")
async def watchdog_run(request: Request):
    """Аудит клиентов панели: «вечные» доступы и аномальный трафик."""
    from app.services import notifications, watchdog

    auth = await require(request, "system.act")
    if isinstance(auth, Response):
        return auth

    bot = getattr(request.app.state, "bot", None)
    try:
        report = await watchdog.run_watch(registry.primary())
    except Exception as exc:  # noqa: BLE001
        logger.exception("Аудит клиентов упал")
        return flash_redirect("/admin/nodes", error=f"Аудит не прошёл: {exc}")

    if report is None:
        return flash_redirect("/admin/nodes", error="Панель не отдала список клиентов")

    text = watchdog.format_report(report)
    async with SessionMaker() as db:
        await audit.log_action(
            db,
            "admin.watchdog_run",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            payload={"total": report.total, "new": len(report.new), "gone": len(report.gone)},
        )
        await db.commit()

    if bot is not None:
        await notifications.notify_admins(bot, text)
    return flash_redirect("/admin/nodes", message=f"Аудит клиентов: проверено {report.total}, новых {len(report.new)}")


@router.post("/alerts/{alert_id}/resolve")
async def resolve_alert(alert_id: int, request: Request, note: str = ""):
    auth = await require(request, "alerts.act")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        alert = await db.get(Alert, alert_id)
        if alert is None:
            return flash_redirect("/admin/alerts", error="Алерт не найден")
        await alerts_service.resolve_alert(db, alert, by=auth.name, note=note)
        await audit.log_action(
            db,
            "admin.alert_resolved",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            payload={"alert_id": alert_id, "kind": alert.kind, "note": note},
        )
        await db.commit()
        title = alert.title

    return flash_redirect("/admin/alerts", message=f"Алерт закрыт: {title}")
