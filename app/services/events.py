"""Журнал событий: отладка, аналитика, разбор инцидентов."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event

#: Справочник видов событий (чтобы не расползались произвольные строки)
START = "start"
TRIAL_STARTED = "trial_started"
ORDER_CREATED = "order_created"
ORDER_PAID = "order_paid"
ORDER_CANCELED = "order_canceled"
SUBSCRIPTION_EXPIRED = "subscription_expired"
SUBSCRIPTION_EXTENDED = "subscription_extended"
REFERRAL_REWARDED = "referral_rewarded"
PANEL_ERROR = "panel_error"
ERROR = "error"


async def log_event(
    session: AsyncSession,
    kind: str,
    *,
    user_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    session.add(
        Event(
            user_id=user_id,
            kind=kind,
            payload=json.dumps(payload, ensure_ascii=False) if payload else None,
        )
    )
