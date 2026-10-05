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


# ---------------------------------------------------------------------- WATA
@router.post("/wata/webhook")
async def wata_webhook(request: Request) -> JSONResponse:
    """Уведомления WATA: оплата, отказ, возврат.

    Подпись — RSA-SHA512 в заголовке ``X-Signature``, проверяется по
    публичному ключу WATA. Тело читаем «как есть»: любая пересборка JSON
    ломает подпись.

    Отвечаем 200 даже на отказные статусы: для предоплатного вебхука
    (клиент нажал «Оплатить») любой ответ кроме 200 означает автоматический
    отказ в оплате, а у нас товар всегда есть — отклонять нечего.
    """
    from app.payments.registry import payments as payment_registry
    from app.payments.wata import parse_order_id, verify_webhook_signature

    body = await request.body()
    signature = request.headers.get("X-Signature")

    provider = payment_registry.get("wata")
    if provider is None:
        logger.warning("Пришёл вебхук WATA, но провайдер не настроен (пуст WATA_TOKEN)")
        raise HTTPException(status_code=503, detail="wata is not configured")

    try:
        public_key = await provider.fetch_public_key()
    except Exception as exc:  # noqa: BLE001 - без ключа проверить подпись нельзя
        logger.error("Не смог получить публичный ключ WATA: %s", exc)
        raise HTTPException(status_code=503, detail="cannot fetch public key") from exc

    if not verify_webhook_signature(body, signature, public_key):
        logger.warning("Вебхук WATA с неверной подписью — отклоняю")
        raise HTTPException(status_code=403, detail="invalid signature")

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc

    kind = str(data.get("kind") or "Payment")
    status = str(data.get("transactionStatus") or "")
    order_id = parse_order_id(data.get("orderId"))
    bot: Bot | None = getattr(request.app.state, "bot", None)

    # Возврат: доступ не отключаем автоматически (это решение человека),
    # но админы должны узнать сразу.
    if kind.lower() == "refund":
        logger.info("WATA: возврат по заказу %s на %s", order_id, data.get("amount"))
        if bot is not None:
            await notifications.notify_admins(
                bot,
                f"↩️ <b>Возврат WATA</b>\nЗаказ: {data.get('orderId')}\nСумма: {data.get('amount')} "
                f"{data.get('currency')}\nПроверь подписку в /admin.",
            )
        return JSONResponse({"ok": True, "kind": "refund"})

    if status.lower() != "paid":
        logger.info("WATA: статус %s по заказу %s (до оплаты)", status, data.get("orderId"))
        return JSONResponse({"ok": True, "status": status})

    if order_id is None:
        logger.warning("Вебхук WATA без распознанного заказа: %s", data.get("orderId"))
        return JSONResponse({"ok": True, "skipped": "no order id"})

    from app.db.session import SessionMaker

    async with SessionMaker() as session:
        order = await session.get(Order, order_id)
        if order is None:
            logger.warning("Вебхук WATA по неизвестному заказу %s", order_id)
            return JSONResponse({"ok": True, "skipped": "unknown order"})

        user = await session.get(User, order.user_id)
        sub, already = await orders_service.mark_paid(
            session,
            order,
            registry.primary(),
            provider_payment_id=str(data.get("id") or ""),
        )
        await events.log_event(
            session,
            events.ORDER_PAID,
            user_id=order.user_id,
            payload={
                "order_id": order.id,
                "auto": True,
                "source": "wata_webhook",
                "transaction_type": data.get("transactionType"),
                "commission": data.get("commission"),
            },
        )
        await session.commit()

        if bot is not None and user is not None and sub is not None and not already:
            await _notify_paid(bot, user, sub)
            await notifications.notify_admins(
                bot,
                f"💳 <b>Оплата через WATA</b>\nЗаказ #{order.id}, {data.get('amount')} "
                f"{data.get('currency')} ({data.get('transactionType')})\n"
                f"Пользователь: {user.display_name} (<code>{user.tg_id}</code>)",
            )

    return JSONResponse({"ok": True})
