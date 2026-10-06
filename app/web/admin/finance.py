"""Финансы: сколько заработали, куда ушло, что выгрузить.

Страница отвечает на три вопроса владельца:
  1. сколько денег пришло и сколько из них вернули;
  2. какая прибыль после комиссий каналов и постоянных расходов;
  3. что выгрузить в бухгалтерию (CSV по заказам, клиентам и журналу).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app.config import get_settings
from app.db.session import SessionMaker
from app.services import stats as stats_service
from app.web.admin.common import page, require

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()

#: Сколько дней показываем на странице. 30 — привычный горизонт отчёта.
PERIODS = (7, 30, 90)


async def _call(name: str, db, **kwargs):  # noqa: ANN001, ANN202
    """Вызвать функцию статистики, если она есть: страница не должна падать."""
    fn = getattr(stats_service, name, None)
    if fn is None:  # pragma: no cover - модуль статистики может быть старее
        return None
    try:
        return await fn(db, **kwargs)
    except Exception as exc:  # noqa: BLE001 - отчёт важнее, но падать не должен
        logger.warning("Не посчитал %s: %s", name, exc)
        return None


@router.get("/finance", response_class=HTMLResponse)
async def finance_page(request: Request, days: int = 30):
    auth = await require(request, "finance.view")
    if isinstance(auth, Response):
        return auth

    period = days if days in PERIODS else 30
    async with SessionMaker() as db:
        snapshot = await stats_service.collect(db)
        profit = await stats_service.profit_summary(db, days=period)
        channels = await stats_service.channel_economics(db, days=period)
        by_day = await _call("revenue_by_day", db, days=period)
        by_plan = await _call("revenue_by_plan", db, days=period)
        refunds = await _call("refund_summary", db, days=period)

    by_day = by_day or []
    by_plan = by_plan or []
    refunds = refunds or {"count": 0, "rub": 0}
    gross = sum(channel.gross_rub for channel in channels) or 0
    refund_share = (refunds["rub"] / gross * 100) if gross else 0.0
    since = (datetime.now(timezone.utc) - timedelta(days=period)).strftime("%d.%m.%Y")

    return await page(
        request,
        "finance.html",
        auth,
        title="Финансы",
        page="finance",
        period=period,
        periods=PERIODS,
        stats=snapshot,
        profit=profit,
        channels=channels,
        by_day=by_day,
        by_plan=by_plan,
        refunds=refunds,
        refund_share=refund_share,
        since=since,
        monthly_costs=settings.monthly_costs_rub,
    )
