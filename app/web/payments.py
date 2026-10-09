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
            await subscriptions.all_user_panels(session),
            provider_payment_id=str(invoice.get("invoice_id") or ""),
            bot=bot,
        )
        await events.log_event(
            session,
            events.ORDER_PAID,
            user_id=order.user_id,
            payload={"order_id": order.id, "auto": True, "source": "crypto_webhook"},
        )
        await session.commit()

        if bot is not None and user is not None and sub is not None and not already:
            await _notify_paid(bot, user, sub, order)
            await notifications.notify_admins(
                bot,
                await notifications.payment_notice(
                    session,
                    order,
                    user,
                    title="Оплата криптой",
                    provider="CryptoBot",
                ),
            )

    return JSONResponse({"ok": True})


async def _notify_paid(bot: Bot, user: User, sub, order=None) -> None:  # noqa: ANN001 - Subscription, Order
    from app.bot import keyboards

    expires = sub.expires_at.strftime("%d.%m.%Y %H:%M") if sub.expires_at else "—"
    link = subscriptions.subscription_link(sub.subscription_token)
    try:
        await bot.send_message(
            user.tg_id,
            texts.order_paid_text(order, expires=expires, days=sub.days_left, link=link),
            reply_markup=keyboards.connect_kb(link),
            disable_web_page_preview=True,
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
            await subscriptions.all_user_panels(session),
            provider_payment_id=str(data.get("id") or ""),
            bot=bot,
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
            await _notify_paid(bot, user, sub, order)
            await notifications.notify_admins(
                bot,
                await notifications.payment_notice(
                    session,
                    order,
                    user,
                    title="Оплата через WATA",
                    provider=f"WATA ({data.get('transactionType')})",
                    note=f"Платёж: {data.get('amount')} {data.get('currency')}",
                ),
            )

    return JSONResponse({"ok": True})


# ------------------------------------------------------------------- Platega
async def _find_order(session, order_id: int | None, transaction_id: str) -> "Order | None":  # noqa: ANN001
    """Найти заказ по id транзакции провайдера, а при неудаче — по payload.

    Порядок именно такой. ``id`` транзакции приходит от Platega и подтверждается
    запросом к API, а ``payload`` — просто поле в теле вебхука, которое можно
    подставить. Раньше поиск шёл от payload, и с утёкшим секретом хватало
    переиграть дешёвую оплаченную транзакцию с payload дорогого заказа.

    Поиск по payload оставлен для заказов, созданных до того, как заказ начал
    хранить id счёта провайдера.
    """
    from sqlalchemy import select

    order = None
    if transaction_id:
        order = await session.scalar(select(Order).where(Order.external_id == transaction_id))
        if order is not None:
            logger.info("Заказ #%s найден по id транзакции", order.id)
    if order is None and order_id:
        order = await session.get(Order, order_id)
        if order is not None:
            logger.info("Заказ #%s найден по payload вебхука", order.id)
    return order


async def _revoke_after_refund(request: Request, order_id: int | None, transaction_id: str, raw: dict) -> None:
    """Отключить доступ после возврата денег (чарджбэк по карте).

    Возврат оформляем через общий сервис ``orders.refund_order``, а не руками:
    только он переводит заказ в ``refunded`` с датой возврата, и только после
    этого деньги уходят из выручки и попадают в отчёт возвратов. Раньше здесь
    статус заказа оставался ``paid`` — сервис показывал прибыль, которой нет.
    Доступ при этом сохраняется, если у клиента есть более поздняя оплата
    (это тоже логика сервиса).
    """
    from app.db.session import SessionMaker

    bot: Bot | None = getattr(request.app.state, "bot", None)
    async with SessionMaker() as session:
        order = await _find_order(session, order_id, transaction_id)
        if order is None:
            logger.warning("Возврат Platega: заказ не найден (payload=%s, id=%s)", order_id, transaction_id)
            return

        if order.status != "paid":
            logger.info("Возврат Platega по заказу #%s в статусе %s — учитывать нечего", order.id, order.status)
            return

        user = await session.get(User, order.user_id)
        ok, message, _sub = await orders_service.refund_order(
            session,
            order,
            await subscriptions.all_user_panels(session),
            actor="platega-webhook",
            note="чарджбэк Platega",
        )
        await session.commit()

        if bot is not None and user is not None:
            await notifications.notify_admins(
                bot,
                f"↩️ <b>Возврат платежа (Platega)</b>\nЗаказ #{order.id}, {order.amount_rub} ₽\n"
                f"Пользователь: {notifications.safe(user.display_name)} "
                f"(<code>{user.tg_id}</code>)\n"
                f"{notifications.safe(message) if ok else 'не удалось оформить возврат'}",
            )


@router.post("/platega/webhook")
async def platega_webhook(request: Request) -> JSONResponse:
    """Уведомления Platega.io: оплата или отмена.

    Особенность: в колбэке **нет криптографической подписи** — только заголовки
    ``X-MerchantId`` и ``X-Secret``. Доверять телу запроса нельзя: после проверки
    заголовков перепроверяем транзакцию через API и выдаём доступ только если
    Platega сама подтверждает статус CONFIRMED. Подделанный вебхук не даст
    бесплатный доступ, даже если секрет утечёт.

    Отдельный случай — **проверка адреса при сохранении в кабинете**: Platega
    отправляет на Callback URL пустой POST и считает адрес валидным только при
    ответе 200 OK. Поэтому пустое или не-JSON тело мы принимаем молча
    (``skipped``), не трогая ни заказы, ни доступ: терять из-за этого
    возможность сохранить адрес нельзя. Ничего не выдавая, такой ответ
    безопасен — 200 здесь не подтверждает ни один платёж.
    """
    from app.payments.base import PaymentStatus
    from app.payments.registry import payments as payment_registry

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - пустое тело это норма: так проверяют адрес
        logger.info("Platega: пустой или не-JSON POST — отвечаю 200 (проверка Callback URL)")
        return JSONResponse({"ok": True, "skipped": "empty body"})

    if not isinstance(body, dict) or not body.get("id"):
        logger.info("Platega: POST без id транзакции — отвечаю 200 (проверка адреса)")
        return JSONResponse({"ok": True, "skipped": "no transaction id"})

    provider = next((p for p in payment_registry.available() if getattr(p, "merchant_id", None)), None)
    if provider is None:
        logger.warning("Пришёл вебхук Platega, но провайдер не настроен")
        raise HTTPException(status_code=503, detail="platega is not configured")

    if not provider.verify_callback(request.headers.get("X-MerchantId"), request.headers.get("X-Secret")):
        logger.warning("Вебхук Platega с неверными заголовками — отклоняю")
        raise HTTPException(status_code=403, detail="invalid credentials")

    order_id, status, raw = provider.parse_callback(body)
    transaction_id = str(raw.get("id") or "")

    if status is PaymentStatus.REFUNDED:
        # Чарджбэк: платёж был успешным, но деньги вернули. Отключаем доступ.
        await _revoke_after_refund(request, order_id, transaction_id, raw)
        return JSONResponse({"ok": True, "status": status.value})

    if status is not PaymentStatus.PAID:
        logger.info("Platega: статус %s по заказу %s (оплаты нет)", status.value, order_id)
        return JSONResponse({"ok": True, "status": status.value})

    # --- перепроверка через API: тело вебхука не подписано
    try:
        check = await provider.check_payment(transaction_id)
    except Exception as exc:  # noqa: BLE001 - сеть или API могли подвести
        logger.error("Не смог перепроверить транзакцию Platega %s: %s", transaction_id, exc)
        raise HTTPException(status_code=503, detail="cannot verify transaction") from exc

    if check.status is not PaymentStatus.PAID:
        logger.warning(
            "Вебхук Platega утверждает оплату, а API говорит %s (транзакция %s) — доступ не выдаю",
            check.status.value,
            transaction_id,
        )
        return JSONResponse({"ok": True, "skipped": f"api says {check.status.value}"})

    from app.db.session import SessionMaker

    bot: Bot | None = getattr(request.app.state, "bot", None)
    async with SessionMaker() as session:
        order = await _find_order(session, order_id, transaction_id)
        if order is None:
            logger.warning("Вебхук Platega: заказ не найден (payload=%s, id=%s)", order_id, transaction_id)
            return JSONResponse({"ok": True, "skipped": "unknown order"})

        # Сумма: заказ мог быть оплачен «не тем» платежом — например, при утечке
        # секрета дешёвую оплаченную транзакцию переигрывают с чужим payload.
        # Требуем, чтобы заплатили не меньше, чем стоит заказ; больше — можно
        # (Platega умеет перекладывать свою комиссию на клиента, счёт тогда выше).
        expected_rub = order.pay_amount_kopecks // 100
        if check.amount is not None and check.amount < expected_rub:
            logger.warning(
                "Вебхук Platega: по заказу #%s ждали %s ₽, а транзакция %s на %s ₽ — доступ не выдаю",
                order.id,
                expected_rub,
                transaction_id,
                check.amount,
            )
            return JSONResponse({"ok": True, "skipped": "amount mismatch"})

        user = await session.get(User, order.user_id)
        sub, already = await orders_service.mark_paid(
            session, order, await subscriptions.all_user_panels(session), provider_payment_id=transaction_id, bot=bot
        )
        await events.log_event(
            session,
            events.ORDER_PAID,
            user_id=order.user_id,
            payload={
                "order_id": order.id,
                "auto": True,
                "source": "platega_webhook",
                "payment_method": raw.get("paymentMethod"),
            },
        )
        await session.commit()

        if bot is not None and user is not None and sub is not None and not already:
            await _notify_paid(bot, user, sub, order)
            await notifications.notify_admins(
                bot,
                await notifications.payment_notice(
                    session,
                    order,
                    user,
                    title="Оплата через Platega",
                    provider=f"Platega (метод {raw.get('paymentMethod')})",
                ),
            )

    return JSONResponse({"ok": True})
