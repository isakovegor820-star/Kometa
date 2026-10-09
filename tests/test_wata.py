"""Тесты приёма платежей через WATA: ссылки, подпись вебхука, выдача доступа."""

from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.config import get_settings
from app.payments.base import PaymentError, PaymentStatus
from app.payments.registry import payments
from app.payments.wata import WataProvider, make_order_id, parse_order_id, verify_webhook_signature
from app.services import orders, subscriptions
from app.web.sub import build_app

settings = get_settings()
TOKEN = "test-wata-token"


# --------------------------------------------------------------------- ключи
@pytest.fixture(scope="module")
def rsa_keys() -> tuple[bytes, bytes]:
    """Пара ключей: приватным подписываем вебхуки, публичный отдаём приложению."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def sign_body(body: bytes, private_pem: bytes) -> str:
    private = serialization.load_pem_private_key(private_pem, password=None)
    signature = private.sign(body, padding.PKCS1v15(), hashes.SHA512())
    return base64.b64encode(signature).decode()


# ------------------------------------------------------------------ провайдер
def make_provider(**kwargs) -> WataProvider:
    transport = httpx.MockTransport(kwargs.pop("handler"))
    client = httpx.AsyncClient(transport=transport)
    return WataProvider(token=TOKEN, client=client, **kwargs)


def test_order_id_roundtrip():
    assert make_order_id(42) == "kometa-42"
    assert parse_order_id("kometa-42") == 42
    assert parse_order_id("42") == 42
    assert parse_order_id("чужой-заказ") is None


async def test_create_invoice_sends_expected_body():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "3a241f8f-685b-da75-e20c-9bc1e4a03679",
                "url": "https://payment.wata.pro/pay-form/3a241f8f-685b-da75-e20c-9bc1e4a03679",
                "status": "Opened",
                "orderId": "kometa-42",
            },
        )

    provider = make_provider(handler=handler)
    invoice = await provider.create_invoice(42, 199, "Kometa: 1 месяц")

    assert captured["url"].endswith("/links/")
    assert captured["auth"] == f"Bearer {TOKEN}"
    assert captured["body"]["amount"] == "199.00"
    assert captured["body"]["currency"] == "RUB"
    assert captured["body"]["orderId"] == "kometa-42"
    assert captured["body"]["type"] == "OneTime"
    assert captured["body"]["expirationDateTime"].endswith("Z")

    assert invoice.provider == "wata"
    assert invoice.external_id == "3a241f8f-685b-da75-e20c-9bc1e4a03679#42"
    assert invoice.pay_url.startswith("https://payment.wata.pro/pay-form/")
    await provider.close()


async def test_create_invoice_uses_exact_kopecks():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "x", "url": "https://pay/x"})

    provider = make_provider(handler=handler)
    await provider.create_invoice(7, 199, "Kometa", exact_kopecks=19913)

    assert captured["body"]["amount"] == "199.13"
    await provider.close()


async def test_create_invoice_rejects_too_small_amount():
    provider = make_provider(handler=lambda request: httpx.Response(200, json={}))
    with pytest.raises(PaymentError, match="от 10"):
        await provider.create_invoice(1, 5, "Kometa")
    await provider.close()


async def test_create_invoice_reports_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": "Payment:PL_1001", "message": "Некорректная сумма платежной ссылки"}},
        )

    provider = make_provider(handler=handler)
    with pytest.raises(PaymentError, match="PL_1001"):
        await provider.create_invoice(1, 199, "Kometa")
    await provider.close()


async def test_check_payment_detects_paid_transaction():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["OrderId"] == "kometa-42"
        return httpx.Response(
            200,
            json={
                "items": [
                    {"kind": "Payment", "transactionStatus": "Declined"},
                    {"kind": "Payment", "transactionStatus": "Paid", "amount": 199.0},
                ]
            },
        )

    provider = make_provider(handler=handler)
    check = await provider.check_payment("link-uuid#42")

    assert check.status is PaymentStatus.PAID
    await provider.close()


async def test_check_payment_returns_pending_when_not_paid():
    provider = make_provider(handler=lambda request: httpx.Response(200, json={"items": []}))
    check = await provider.check_payment("link-uuid#42")
    assert check.status is PaymentStatus.PENDING
    await provider.close()


async def test_check_payment_rejects_broken_external_id():
    provider = make_provider(handler=lambda request: httpx.Response(200, json={"items": []}))
    with pytest.raises(PaymentError, match="Некорректный идентификатор"):
        await provider.check_payment("без-номера")
    await provider.close()


async def test_public_key_is_cached(rsa_keys):
    calls = {"n": 0}
    _, public_pem = rsa_keys

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"value": public_pem.decode()})

    provider = make_provider(handler=handler)
    first = await provider.fetch_public_key()
    second = await provider.fetch_public_key()

    assert first == second
    assert calls["n"] == 1
    await provider.close()


def test_signature_verification(rsa_keys):
    private_pem, public_pem = rsa_keys
    body = b'{"transactionStatus":"Paid","orderId":"kometa-42"}'

    assert verify_webhook_signature(body, sign_body(body, private_pem), public_pem.decode())
    assert not verify_webhook_signature(body + b" ", sign_body(body, private_pem), public_pem.decode())
    assert not verify_webhook_signature(body, "не-base64", public_pem.decode())
    assert not verify_webhook_signature(body, None, public_pem.decode())
