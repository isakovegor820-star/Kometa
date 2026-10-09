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


def stub_api_check(monkeypatch, provider, status: PaymentStatus, amount: int | None = None):  # noqa: ANN001
    async def fake_check(external_id: str) -> PaymentCheck:
        return PaymentCheck(
            status=status, amount=amount, raw={"id": external_id, "status": status.value}
        )

    monkeypatch.setattr(provider, "check_payment", fake_check)


async def test_both_methods_are_registered():
    codes = {provider.code for provider in payments.available()}
    assert {"platega_sbp", "platega_card"} <= codes


async def test_empty_post_is_accepted_as_callback_url_check(client):
    """Platega проверяет Callback URL пустым POST и требует 200 OK.

    Иначе адрес не сохранить в кабинете, и автоматического подтверждения
    платежей не будет вовсе.
    """
    response = await client.post("/payments/platega/webhook", content=b"")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "skipped": "empty body"}


async def test_post_without_transaction_id_is_accepted(client):
    """Мусор без id транзакции тоже не должен ломать проверку адреса."""
    response = await client.post(
        "/payments/platega/webhook", content=b'{"hello": "world"}', headers=headers()
    )

    assert response.status_code == 200
    assert response.json()["skipped"] == "no transaction id"


async def test_empty_post_does_not_change_orders(client, session):
    """Проверка адреса — не оплата: заказы не трогаем."""
    user, order = await make_order(session, 9402)

    await client.post("/payments/platega/webhook", content=b"")

    await session.refresh(order)
    assert order.status == "pending"


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


async def test_invalid_json_is_answered_with_200(client, session, panel):
    """Не-JSON тело не роняем в 400: так Platega проверяет адрес при сохранении.

    Ничего не выдавая, ответ 200 безопасен — платёж без разобранного тела не
    подтверждается (см. test_empty_post_does_not_change_orders).
    """
    response = await client.post(
        "/payments/platega/webhook", content=b"not-json", headers={**headers(), "Content-Type": "application/json"}
    )

    assert response.status_code == 200
    assert response.json()["skipped"] == "empty body"


async def test_chargeback_disables_subscription(client, session, panel, monkeypatch):
    """Возврат денег (CHARGEBACKED) должен отключать доступ."""
    user, order = await make_order(session, 9409)
    provider = payments.get("platega_sbp")
    stub_api_check(monkeypatch, provider, PaymentStatus.PAID)

    # сначала обычная оплата
    await client.post("/payments/platega/webhook", content=webhook_body(order.id), headers=headers())
    await session.refresh(order)
    assert order.status == "paid"
    sub = await subscriptions.get_subscription(session, user.id)
    assert sub.status == "active"

    # затем чарджбэк
    response = await client.post(
        "/payments/platega/webhook", content=webhook_body(order.id, status="CHARGEBACKED"), headers=headers()
    )

    assert response.status_code == 200
    assert response.json()["status"] == "refunded"
    await session.refresh(sub)
    assert sub.status == "blocked"


async def test_chargeback_marks_order_refunded(client, session, panel, monkeypatch):
    """Чарджбэк убирает деньги из выручки: заказ становится ``refunded``.

    Раньше здесь только отключался доступ, а статус заказа оставался ``paid`` —
    возвращённые банком деньги продолжали считаться выручкой и прибылью.
    """
    user, order = await make_order(session, 9413)
    provider = payments.get("platega_sbp")
    stub_api_check(monkeypatch, provider, PaymentStatus.PAID)

    await client.post("/payments/platega/webhook", content=webhook_body(order.id), headers=headers())
    response = await client.post(
        "/payments/platega/webhook", content=webhook_body(order.id, status="CHARGEBACKED"), headers=headers()
    )

    assert response.status_code == 200
    await session.refresh(order)
    assert order.status == "refunded"
    assert order.refunded_at is not None
    assert order.refunded_by == "platega-webhook"


async def test_webhook_refuses_underpayment(client, session, panel, monkeypatch):
    """Оплата меньше стоимости заказа доступ не выдаёт.

    Сценарий атаки при утёкшем секрете: переиграть дешёвую оплаченную
    транзакцию с payload дорогого заказа. Сверка суммы его закрывает.
    """
    user, order = await make_order(session, 9414)
    assert order.amount_rub > 1, "нужен заказ дороже рубля"
    provider = payments.get("platega_sbp")
    stub_api_check(monkeypatch, provider, PaymentStatus.PAID, amount=1)

    response = await client.post(
        "/payments/platega/webhook", content=webhook_body(order.id), headers=headers()
    )

    assert response.status_code == 200
    assert response.json()["skipped"] == "amount mismatch"
    await session.refresh(order)
    assert order.status == "pending"


async def test_webhook_accepts_payment_with_client_commission(client, session, panel, monkeypatch):
    """Заплатили больше (комиссия переложена на клиента) — доступ выдаём."""
    user, order = await make_order(session, 9415)
    provider = payments.get("platega_sbp")
    stub_api_check(monkeypatch, provider, PaymentStatus.PAID, amount=order.amount_rub + 10)

    await client.post("/payments/platega/webhook", content=webhook_body(order.id), headers=headers())

    await session.refresh(order)
    assert order.status == "paid"


async def test_webhook_prefers_transaction_id_over_payload(client, session, panel, monkeypatch):
    """Заказ ищем по id транзакции: payload из тела подставить нельзя.

    Иначе с утёкшим секретом дешёвая оплаченная транзакция с чужим payload
    выдавала бы доступ по дорогому заказу.
    """
    user_cheap, cheap = await make_order(session, 9416)
    user_dear, dear = await make_order(session, 9417)
    cheap.external_id = "tx-cheap"
    cheap.amount_rub = 1
    dear.external_id = "tx-expensive"
    await session.commit()

    provider = payments.get("platega_sbp")
    stub_api_check(monkeypatch, provider, PaymentStatus.PAID, amount=1)

    body = json.dumps(
        {
            "id": "tx-cheap",
            "amount": 1,
            "currency": "RUB",
            "status": "CONFIRMED",
            "paymentMethod": 2,
            "payload": f"order:{dear.id}",  # подделка: чужой заказ
        }
    ).encode()

    response = await client.post("/payments/platega/webhook", content=body, headers=headers())

    assert response.status_code == 200
    await session.refresh(dear)
    await session.refresh(cheap)
    assert dear.status == "pending", "дорогой заказ не должен оплачиваться чужой транзакцией"
    assert cheap.status == "paid"


async def test_chargeback_status_maps_to_refunded():
    """Статус CHARGEBACKED из документации Platega разбирается как возврат."""
    provider = payments.get("platega_sbp")
    _, status, _ = provider.parse_callback({"id": "x", "status": "CHARGEBACKED", "payload": "order:1"})

    assert status is PaymentStatus.REFUNDED


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


async def test_pay_button_saves_transaction_id_in_order(bot, dispatcher, session, monkeypatch):
    """Заказ хранит id счёта провайдера — иначе вебхук его не найдёт.

    Регрессия: в ``buy.py`` стояла проверка ``external_id == f"ord-{order.id}"``,
    а заказ создаётся с ``ord-<hex>`` — условие не срабатывало никогда. Из этого
    росли две поломки: опрос статуса уходил по чужому id
    (``GET /transaction/ord-…``), а колбэк Platega без payload не мог сопоставить
    оплату с заказом.
    """
    from app.payments.platega import PlategaProvider
    from tests.fakes import make_update

    async def fake_request(self, method, path, **kwargs):  # noqa: ANN001, ARG001
        return httpx.Response(
            200,
            json={
                "transactionId": "tx-from-platega",
                "redirect": "https://pay.platega.io?sbp",
                "paymentDetails": {"amount": 199, "currency": "RUB"},
                "status": "PENDING",
                "expiresIn": "00:15:00",
            },
        )

    monkeypatch.setattr(PlategaProvider, "_request", fake_request)

    await dispatcher.feed_update(bot, make_update("/start", user_id=9601))
    plans = await orders.list_plans(session)
    plan = next(p for p in plans if p.code == "m1")
    await dispatcher.feed_update(bot, make_update(callback_data=f"pay:{plan.id}:platega_sbp", user_id=9601))

    pending = await orders.pending_orders(session)
    assert pending, "заказ не создан"
    assert pending[0].external_id == "tx-from-platega"
