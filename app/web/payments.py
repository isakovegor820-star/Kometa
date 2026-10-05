"""Вебхуки платёжных систем.

CryptoBot (Crypto Pay) присылает вебхук сразу после оплаты — это быстрее и
надёжнее опроса, поэтому подписка выдаётся мгновенно. Опрос (`job_check_crypto`)
остаётся как страховка, если вебхук не дошёл.

Проверка подписи: заголовок `crypto-pay-api-signature` — это HMAC-SHA256 от
тела запроса, где ключ = SHA256(токен приложения).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from aiogram import Bot
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db.models import Order, User
from app.panels.registry import registry
from app.payments.payload import parse_order_id_from_payload
from app.services import events, notifications, orders as orders_service, subscriptions

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter(prefix="/payments", tags=["payments"])


def verify_cryptobot_signature(body: bytes, signature: str | None, token: str) -> bool:
    """Проверить подпись вебхука Crypto Pay.

    Ключ подписи — SHA256 от токена приложения (так описано в документации
    Crypto Pay), сама подпись — HMAC-SHA256 от тела запроса.
    """
    if not signature or not token:
        return False
    secret = hashlib.sha256(token.encode()).digest()
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/crypto/webhook")
async def cryptobot_webhook(request: Request) -> JSONResponse:
    body = await request.body()
    signature = request.headers.get("crypto-pay-api-signature")

    if not verify_cryptobot_signature(body, signature, settings.cryptobot_token):
        logger.warning("Вебхук Crypto Pay с неверной подписью — отклоняю")
        raise HTTPException(status_code=403, detail="invalid signature")

    try:
        update = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc

    if update.get("update_type") != "invoice_paid":
        return JSONResponse({"ok": True, "skipped": update.get("update_type")})

    invoice = update.get("payload") or {}
    order_id = parse_order_id_from_payload(invoice.get("payload"))
    if order_id is None:
        logger.warning("Вебхук без кода заказа: %s", invoice)
        return JSONResponse({"ok": True, "skipped": "no order id"})

    from app.db.session import SessionMaker

    bot: Bot | None = getattr(request.app.state, "bot", None)
    async with SessionMaker() as session:
        order = await session.get(Order, order_id)
        if order is None:
            logger.warning("Вебхук по неизвестному заказу %s", order_id)
            return JSONResponse({"ok": True, "skipped": "unknown order"})

        user = await session.get(User, order.user_id)
        sub, already = await orders_service.mark_paid(
            session,
            order,
            registry.primary(),
            provider_payment_id=str(invoice.get("invoice_id") or ""),
        )
        await events.log_event(
            session,
            events.ORDER_PAID,
            user_id=order.user_id,
            payload={"order_id": order.id, "auto": True, "source": "crypto_webhook"},
        )
        await session.commit()

        if bot is not None and user is not None and sub is not None and not already:
            await _notify_paid(bot, user, sub)
            await notifications.notify_admins(
                bot,
                f"🪙 <b>Оплата криптой</b>\nЗаказ #{order.id}, {order.amount_rub} ₽\n"
                f"Пользователь: {user.display_name} (<code>{user.tg_id}</code>)",
            )

    return JSONResponse({"ok": True})


async def _notify_paid(bot: Bot, user: User, sub) -> None:  # noqa: ANN001 - Subscription
    expires = sub.expires_at.strftime("%d.%m.%Y %H:%M") if sub.expires_at else "—"
    try:
        await bot.send_message(
            user.tg_id, f"✅ Оплата получена! Подписка активна до <b>{expires}</b> ({sub.days_left} дн.)."
        )
        await bot.send_message(
            user.tg_id,
            f"🔗 Ссылка-подписка:\n<code>{subscriptions.subscription_link(sub.subscription_token)}</code>",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не смог уведомить пользователя %s: %s", user.tg_id, exc)
