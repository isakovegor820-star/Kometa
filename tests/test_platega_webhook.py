"""Тесты вебхука Platega: заголовки, перепроверка через API, выдача доступа.

Главная проверка здесь — защита от подделки: у Platega в колбэке нет подписи,
поэтому доступ должен выдаваться только после подтверждения статуса через API.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from app.config import get_settings
from app.payments.base import PaymentCheck, PaymentStatus
from app.payments.registry import payments
from app.services import orders, subscriptions
from app.web.sub import build_app

settings = get_settings()
MERCHANT = "1a021d91-9b26-4762-b303-5d4aac74e921"
SECRET = "test-platega-secret"


@pytest.fixture(autouse=True)
def platega_settings(monkeypatch):
    monkeypatch.setattr(settings, "platega_merchant_id", MERCHANT)
    monkeypatch.setattr(settings, "platega_secret", SECRET)
    monkeypatch.setattr(settings, "platega_methods", "2,10")
    payments.reload()
    yield
    payments.reload()


@pytest.fixture
async def client(session):
    app = await build_app(bot=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def make_order(session, tg_id: int):  # noqa: ANN001
    async def _create():
        user, _ = await subscriptions.get_or_create_user(session, tg_id=tg_id, username=f"pg{tg_id}")
        plan = (await orders.list_plans(session))[0]
        order = await orders.create_order(session, user, plan, provider="platega_sbp")
        await session.commit()
        return user, order

    return _create()


def webhook_body(order_id: int, status: str = "CONFIRMED") -> bytes:
    return json.dumps(
        {
            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "amount": 199.0,
            "currency": "RUB",
            "status": status,
            "paymentMethod": 2,
            "payload": f"order:{order_id}",
        }
    ).encode()


def headers(merchant: str = MERCHANT, secret: str = SECRET) -> dict[str, str]:
    return {"X-MerchantId": merchant, "X-Secret": secret}


def stub_api_check(monkeypatch, provider, status: PaymentStatus):  # noqa: ANN001
    async def fake_check(external_id: str) -> PaymentCheck:
        return PaymentCheck(status=status, raw={"id": external_id, "status": status.value})

    monkeypatch.setattr(provider, "check_payment", fake_check)


async def test_both_methods_are_registered():
    codes = {provider.code for provider in payments.available()}
    assert {"platega_sbp", "platega_card"} <= codes


async def test_webhook_confirms_payment_after_api_check(client, session, panel, monkeypatch):
    user, order = await make_order(session, 9401)
    provider = payments.get("platega_sbp")
    stub_api_check(monkeypatch, provider, PaymentStatus.PAID)

    response = await client.post("/payments/platega/webhook", content=webhook_body(order.id), headers=headers())

    assert response.status_code == 200 and response.json()["ok"] is True
    await session.refresh(order)
    assert order.status == "paid"
    sub = await subscriptions.get_subscription(session, user.id)
    assert sub is not None and sub.status == "active"


async def test_forged_webhook_without_api_confirmation_grants_nothing(client, session, panel, monkeypatch):
    """Секрет утёк и вебхук подделали — но API говорит PENDING, доступ не выдаём."""
    _, order = await make_order(session, 9402)
    provider = payments.get("platega_sbp")
    stub_api_check(monkeypatch, provider, PaymentStatus.PENDING)

    response = await client.post("/payments/platega/webhook", content=webhook_body(order.id), headers=headers())

    assert response.status_code == 200
    assert "api says" in response.json()["skipped"]
    await session.refresh(order)
    assert order.status == "pending"


async def test_webhook_rejects_wrong_secret(client, session, panel):
    _, order = await make_order(session, 9403)

    response = await client.post(
        "/payments/platega/webhook", content=webhook_body(order.id), headers=headers(secret="wrong")
    )

    assert response.status_code == 403
    await session.refresh(order)
    assert order.status == "pending"


async def test_webhook_rejects_foreign_merchant(client, session, panel):
    _, order = await make_order(session, 9404)

    response = await client.post(
        "/payments/platega/webhook", content=webhook_body(order.id), headers=headers(merchant="other-merchant")
    )

    assert response.status_code == 403


async def test_canceled_status_grants_nothing(client, session, panel, monkeypatch):
    _, order = await make_order(session, 9405)
    provider = payments.get("platega_sbp")
    stub_api_check(monkeypatch, provider, PaymentStatus.PAID)

    response = await client.post(
        "/payments/platega/webhook", content=webhook_body(order.id, status="CANCELED"), headers=headers()
    )

    assert response.status_code == 200
    assert response.json()["status"] == "canceled"
    await session.refresh(order)
    assert order.status == "pending"


async def test_webhook_skips_unknown_order(client, session, panel, monkeypatch):
    provider = payments.get("platega_sbp")
    stub_api_check(monkeypatch, provider, PaymentStatus.PAID)

    response = await client.post("/payments/platega/webhook", content=webhook_body(999_999), headers=headers())

    assert response.status_code == 200
    assert response.json()["skipped"] == "unknown order"


async def test_webhook_is_idempotent(client, session, panel, monkeypatch):
    user, order = await make_order(session, 9406)
    provider = payments.get("platega_sbp")
    stub_api_check(monkeypatch, provider, PaymentStatus.PAID)
    body, header = webhook_body(order.id), headers()

    await client.post("/payments/platega/webhook", content=body, headers=header)
    sub = await subscriptions.get_subscription(session, user.id)
    expires_after_first = sub.expires_at

    await client.post("/payments/platega/webhook", content=body, headers=header)

    await session.refresh(sub)
    assert sub.expires_at == expires_after_first


async def test_webhook_returns_503_when_not_configured(client, session, monkeypatch):
    monkeypatch.setattr(settings, "platega_merchant_id", "")
    monkeypatch.setattr(settings, "platega_secret", "")
    payments.reload()

    response = await client.post("/payments/platega/webhook", content=webhook_body(1), headers=headers())

    assert response.status_code == 503


async def test_invalid_json_is_rejected(client, session, panel):
    response = await client.post(
        "/payments/platega/webhook", content=b"not-json", headers={**headers(), "Content-Type": "application/json"}
    )

    assert response.status_code == 400


async def test_order_found_by_transaction_id_when_payload_missing(client, session, panel, monkeypatch):
    """В примере вебхука Platega поля payload нет — заказ ищем по id транзакции.

    Это ключевой риск автовыдачи: без фолбэка оплата прошла бы, а доступ не выдался.
    """
    user, order = await make_order(session, 9408)
    order.external_id = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    await session.commit()

    provider = payments.get("platega_sbp")
    stub_api_check(monkeypatch, provider, PaymentStatus.PAID)

    body = json.dumps(
        {"id": order.external_id, "amount": 199.0, "currency": "RUB", "status": "CONFIRMED", "paymentMethod": 2}
    ).encode()

    response = await client.post("/payments/platega/webhook", content=body, headers=headers())

    assert response.status_code == 200 and response.json()["ok"] is True
    await session.refresh(order)
    assert order.status == "paid"
    sub = await subscriptions.get_subscription(session, user.id)
    assert sub is not None and sub.status == "active"


async def test_order_gets_payment_link_for_selected_method(session, monkeypatch):
    """Ссылка на оплату создаётся через API Platega (проверяем тело запроса)."""
    from app.payments.platega import PlategaProvider

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "paymentMethod": "SBPQR",
                "transactionId": "3fa85f64-5717-4562-b3fc-2c463f66afa6",
                "redirect": "https://pay.platega.io?qrsbp",
                "status": "PENDING",
                "expiresIn": "00:15:00",
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = PlategaProvider(MERCHANT, SECRET, payment_method=2, client=http)

    invoice = await provider.create_invoice(42, 199, "Kometa: 1 месяц", exact_kopecks=19913)

    assert captured["url"].endswith("/transaction/process")
    assert captured["headers"]["x-merchantid"] == MERCHANT
    assert captured["headers"]["x-secret"] == SECRET
    assert captured["body"]["paymentMethod"] == 2
    assert captured["body"]["paymentDetails"]["currency"] == "RUB"
    assert captured["body"]["payload"] == "order:42"
    assert invoice.pay_url == "https://pay.platega.io?qrsbp"
    await provider.close()


async def test_order_created_at_is_recent(session):
    """Заказ для теста создаётся сейчас — иначе автоплатежи его не увидят."""
    _, order = await make_order(session, 9407)
    assert (datetime.now(timezone.utc) - order.created_at).total_seconds() < 60
