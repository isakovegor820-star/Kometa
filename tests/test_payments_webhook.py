"""Тесты вебхука Crypto Pay: подпись, мгновенная выдача подписки."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from app.config import get_settings
from app.services import orders, subscriptions
from app.web.payments import verify_cryptobot_signature
from app.web.sub import build_app

settings = get_settings()
TOKEN = "test-crypto-pay-token"


@pytest.fixture(autouse=True)
def crypto_token(monkeypatch):
    monkeypatch.setattr(settings, "cryptobot_token", TOKEN)
    yield


@pytest.fixture
async def client(session):
    app = await build_app(bot=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def sign(body: bytes, token: str = TOKEN) -> str:
    secret = hashlib.sha256(token.encode()).digest()
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def invoice_payload(order_id: int, invoice_id: str = "inv-1") -> bytes:
    return json.dumps(
        {
            "update_id": 1,
            "update_type": "invoice_paid",
            "payload": {
                "invoice_id": invoice_id,
                "status": "paid",
                "amount": "1.06",
                "payload": f"order:{order_id}",
            },
        }
    ).encode()


async def test_signature_verification():
    body = b'{"update_type":"invoice_paid"}'
    assert verify_cryptobot_signature(body, sign(body), TOKEN)
    assert not verify_cryptobot_signature(body, "deadbeef", TOKEN)
    assert not verify_cryptobot_signature(body, None, TOKEN)
    assert not verify_cryptobot_signature(body, sign(body), "")


async def test_webhook_rejects_bad_signature(client, session):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=9001, username="hacker")
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="crypto")
    await session.commit()

    response = await client.post(
        "/payments/crypto/webhook",
        content=invoice_payload(order.id),
        headers={"crypto-pay-api-signature": "wrong"},
    )

    assert response.status_code == 403
    await session.refresh(order)
    assert order.status == "pending"  # доступ не выдан


async def test_webhook_activates_subscription(client, session, panel):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=9002, username="buyer")
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="crypto")
    await session.commit()

    body = invoice_payload(order.id)
    response = await client.post(
        "/payments/crypto/webhook",
        content=body,
        headers={"crypto-pay-api-signature": sign(body)},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True

    await session.refresh(order)
    assert order.status == "paid"
    sub = await subscriptions.get_subscription(session, user.id)
    assert sub is not None and sub.status == "active"


async def test_webhook_is_idempotent(client, session, panel):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=9003, username="buyer3")
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="crypto")
    await session.commit()

    body = invoice_payload(order.id, invoice_id="inv-dup")
    headers = {"crypto-pay-api-signature": sign(body)}
    await client.post("/payments/crypto/webhook", content=body, headers=headers)
    sub = await subscriptions.get_subscription(session, user.id)
    expires_after_first = sub.expires_at

    # повторная доставка того же вебхука не продлевает подписку
    await client.post("/payments/crypto/webhook", content=body, headers=headers)

    await session.refresh(sub)
    assert sub.expires_at == expires_after_first


async def test_webhook_ignores_other_update_types(client):
    body = json.dumps({"update_type": "invoice_expired", "payload": {}}).encode()
    response = await client.post(
        "/payments/crypto/webhook",
        content=body,
        headers={"crypto-pay-api-signature": sign(body)},
    )

    assert response.status_code == 200
    assert response.json()["skipped"] == "invoice_expired"


async def test_webhook_skips_unknown_order(client):
    body = invoice_payload(999_999)
    response = await client.post(
        "/payments/crypto/webhook",
        content=body,
        headers={"crypto-pay-api-signature": sign(body)},
    )

    assert response.status_code == 200
    assert response.json()["skipped"] == "unknown order"
