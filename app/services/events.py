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
#: Деньги вернули плательщику (чарджбэк) — доступ отключён.
ORDER_REFUNDED = "order_refunded"
SUBSCRIPTION_EXPIRED = "subscription_expired"
SUBSCRIPTION_EXTENDED = "subscription_extended"
#: Доступ отозван вручную (возврат денег, подозрение на шеринг).
SUBSCRIPTION_REVOKED = "subscription_revoked"
#: Доступ вернули после отзыва или блокировки.
SUBSCRIPTION_RESTORED = "subscription_restored"
REFERRAL_REWARDED = "referral_rewarded"
#: По реферальной ссылке пришёл новый человек.
REFERRAL_JOINED = "referral_joined"
#: Награда не потерялась, а легла в накопительный баланс дней.
REFERRAL_BONUS_ACCRUED = "referral_bonus_accrued"
#: Накопленные дни применились к новой подписке.
BONUS_DAYS_APPLIED = "bonus_days_applied"
#: Промокод применён к заказу (скидка уже в цене).
PROMO_APPLIED = "promo_applied"
#: Заказ со скидкой оплачен — скидка использована.
PROMO_REDEEMED = "promo_redeemed"
PANEL_ERROR = "panel_error"
ERROR = "error"
#: Период простоя: открыт, закрыт, компенсация начислена.
DOWNTIME_STARTED = "downtime_started"
DOWNTIME_FINISHED = "downtime_finished"
DOWNTIME_GRANTED = "downtime_granted"
#: Подарочный сертификат: куплен и активирован получателем.
GIFT_BOUGHT = "gift_bought"
GIFT_ACTIVATED = "gift_activated"
#: Автосценарий жизненного цикла (приглашение, win-back, апселл).
LIFECYCLE_SENT = "lifecycle_sent"
#: Партнёрская программа: партнёр создан, ему начислена выплата, выплата закрыта.
PARTNER_CREATED = "partner_created"
PARTNER_REWARDED = "partner_rewarded"
PARTNER_PAID_OUT = "partner_paid_out"
#: Персональная ссылка под конкретного человека: создана и активирована.
PERSONAL_LINK_CREATED = "personal_link_created"
PERSONAL_LINK_USED = "personal_link_used"
#: Персональные данные удалены: по запросу клиента или задачей ретенции.
USER_ANONYMIZED = "user_anonymized"
#: Задача ретенции почистила старые журналы (события/алерты/рассылки).
RETENTION_PURGED = "retention_purged"


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
