"""Тесты приёма платежей через Platega: ссылка, статусы, вебхук, ошибки.

Сеть не используется: httpx-клиент подменяется ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from app.payments.base import PaymentError, PaymentStatus
from app.payments.platega import PlategaProvider

MERCHANT_ID = "test-merchant-id"
SECRET = "test-secret-key"

REDIRECT = "https://pay.platega.io?qrsbp"


def make_provider(handler, **kwargs) -> PlategaProvider:
    """Провайдер поверх MockTransport: наружу (в сеть) не ходим."""
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return PlategaProvider(MERCHANT_ID, SECRET, client=client, **kwargs)


def process_response(**overrides) -> dict:
    """Ответ ``POST /transaction/process`` из документации."""
    data = {
        "paymentMethod": "SBPQR",
        "transactionId": "b7a1f0b1-0000-4000-8000-000000000001",
        "redirect": REDIRECT,
        "return": "https://kometa.example/ok",
        "paymentDetails": "199 RUB",
        "status": "PENDING",
        "expiresIn": "00:15:00",
        "merchantId": MERCHANT_ID,
        "usdtRate": 93.45,
    }
    data.update(overrides)
    return data


# ------------------------------------------------------------ создание платежа
async def test_create_invoice_sends_expected_request():
    """Правильный URL, оба заголовка авторизации и тело запроса."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["merchant"] = request.headers.get("X-MerchantId")
        captured["secret"] = request.headers.get("X-Secret")
        captured["content_type"] = request.headers.get("Content-Type")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=process_response())

    provider = make_provider(
        handler,
        payment_method=2,
        return_url="https://kometa.example/ok",
        failed_url="https://kometa.example/fail",
    )
    await provider.create_invoice(42, 199, "Kometa: 1 месяц")

    assert captured["method"] == "POST"
    assert captured["url"] == "https://app.platega.io/transaction/process"
    assert captured["merchant"] == MERCHANT_ID
    assert captured["secret"] == SECRET
    assert captured["content_type"] == "application/json"

    body = captured["body"]
    assert body["paymentMethod"] == 2
    assert body["paymentDetails"] == {"amount": 19900, "currency": "RUB"}
    assert body["payload"] == "order:42"
    assert body["description"] == "Kometa: 1 месяц"
    assert body["return"] == "https://kometa.example/ok"
    assert body["failedUrl"] == "https://kometa.example/fail"

    # id — наш uuid4 (уникальный на каждый вызов), а не что-то из ответа.
    parsed = uuid.UUID(body["id"])
    assert parsed.version == 4

    await provider.close()


async def test_create_invoice_generates_unique_ids():
    """Повтор того же id Platega отвергает, поэтому uuid не переиспользуется."""
    ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ids.append(json.loads(request.content)["id"])
        return httpx.Response(200, json=process_response())

    provider = make_provider(handler)
    await provider.create_invoice(1, 199, "Kometa")
    await provider.create_invoice(1, 199, "Kometa")

    assert len(set(ids)) == 2
    await provider.close()


async def test_create_invoice_returns_pay_url_and_external_id():
    """Разбор ответа: pay_url из redirect, external_id — наш id транзакции."""
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["id"] = json.loads(request.content)["id"]
        return httpx.Response(200, json=process_response())

    provider = make_provider(handler, payment_method=10)
    invoice = await provider.create_invoice(42, 199, "Kometa: 1 месяц")

    assert invoice.provider == "platega_card"
    assert invoice.pay_url == REDIRECT
    assert invoice.external_id == sent["id"]
    assert invoice.amount_rub == 199
    assert invoice.currency == "RUB"
    assert "199.00" in invoice.instructions and "Карта МИР" in invoice.instructions
    await provider.close()


async def test_create_invoice_omits_empty_return_urls():
    """Если return/failedUrl не настроены — не отправляем пустые строки."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=process_response())

    provider = make_provider(handler)
    await provider.create_invoice(7, 100, "Kometa")

    assert "return" not in captured["body"]
    assert "failedUrl" not in captured["body"]
    await provider.close()


async def test_create_invoice_uses_exact_kopecks():
    """Копеечная надбавка (уникальные копейки) уезжает в amount как есть."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=process_response())

    provider = make_provider(handler)
    invoice = await provider.create_invoice(3, 199, "Kometa", exact_kopecks=19913)

    assert captured["body"]["paymentDetails"]["amount"] == 19913
    assert "199.13" in invoice.instructions
    await provider.close()


async def test_amount_unit_rubles_sends_rubles():
    """amount_unit="rubles" — на случай, если Platega ждёт рубли, а не копейки."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=process_response())

    provider = make_provider(handler, amount_unit="rubles")
    await provider.create_invoice(42, 199, "Kometa", exact_kopecks=19913)

    assert captured["body"]["paymentDetails"]["amount"] == 199.13
    await provider.close()


def test_amount_unit_is_validated():
    with pytest.raises(ValueError, match="amount_unit"):
        PlategaProvider(MERCHANT_ID, SECRET, amount_unit="bitcoins")


async def test_create_invoice_rejects_zero_amount():
    provider = make_provider(lambda request: httpx.Response(200, json=process_response()))
    with pytest.raises(PaymentError, match="больше нуля"):
        await provider.create_invoice(1, 0, "Kometa")
    await provider.close()


def test_code_and_title_depend_on_payment_method():
    assert (PlategaProvider(MERCHANT_ID, SECRET, payment_method=2).code, "СБП / QR-код") == (
        "platega_sbp",
        "СБП / QR-код",
    )
    assert PlategaProvider(MERCHANT_ID, SECRET, payment_method=10).code == "platega_card"
    assert PlategaProvider(MERCHANT_ID, SECRET, payment_method=10).title == "Карта МИР"
    assert PlategaProvider(MERCHANT_ID, SECRET, payment_method=12).code == "platega_intl"
    assert PlategaProvider(MERCHANT_ID, SECRET, payment_method=12).title == "Зарубежная карта"
    unknown = PlategaProvider(MERCHANT_ID, SECRET, payment_method=99)
    assert unknown.code == "platega"
    assert unknown.title


# ------------------------------------------------------------------- ошибки
async def test_no_available_requisites_hint():
    """400 «No available requisites» → подсказка про другую сумму и копейки."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "No available requisites"})

    provider = make_provider(handler)
    with pytest.raises(PaymentError) as exc:
        await provider.create_invoice(1, 100, "Kometa")

    text = str(exc.value)
    assert "другую сумму" in text and "копейки" in text
    await provider.close()


async def test_transaction_already_exists_error():
    """400 «already exists» (повтор нашего id) → понятный русский текст."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "Transaction abc already exists."})

    provider = make_provider(handler)
    with pytest.raises(PaymentError, match="уже существует"):
        await provider.create_invoice(1, 100, "Kometa")
    await provider.close()


async def test_plain_error_is_reported_with_code_and_text():
    """Прочие ошибки — HTTP-код и текст от Platega, без потери деталей."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Invalid credentials"})

    provider = make_provider(handler)
    with pytest.raises(PaymentError, match="401"):
        await provider.create_invoice(1, 100, "Kometa")
    await provider.close()


async def test_non_json_response_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>oops</html>")

    provider = make_provider(handler)
    with pytest.raises(PaymentError, match="не в формате JSON"):
        await provider.create_invoice(1, 100, "Kometa")
    await provider.close()


async def test_response_without_redirect_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "PENDING"})

    provider = make_provider(handler)
    with pytest.raises(PaymentError, match="ссылку на оплату"):
        await provider.create_invoice(1, 100, "Kometa")
    await provider.close()


async def test_network_error_becomes_payment_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети", request=request)

    provider = make_provider(handler)
    with pytest.raises(PaymentError, match="недоступна"):
        await provider.create_invoice(1, 100, "Kometa")
    await provider.close()


async def test_missing_credentials_reported():
    provider = PlategaProvider("", "", client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=process_response())
    )))
    with pytest.raises(PaymentError, match="PLATEGA_MERCHANT_ID"):
        await provider.create_invoice(1, 100, "Kometa")
    await provider.close()


# -------------------------------------------------------------- статус платежа
@pytest.mark.parametrize(
    ("platega_status", "expected"),
    [
        ("CONFIRMED", PaymentStatus.PAID),
        ("PENDING", PaymentStatus.PENDING),
        ("EXPIRED", PaymentStatus.EXPIRED),
        ("CANCELED", PaymentStatus.CANCELED),
        ("FAILED", PaymentStatus.CANCELED),
    ],
)
async def test_check_payment_status_mapping(platega_status: str, expected: PaymentStatus):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["merchant"] = request.headers.get("X-MerchantId")
        captured["secret"] = request.headers.get("X-Secret")
        return httpx.Response(
            200,
            json={
                "id": "b7a1f0b1-0000-4000-8000-000000000001",
                "status": platega_status,
                "paymentDetails": {"amount": 19900, "currency": "RUB"},
                "comission": 0,
                "paymentMethod": "SBPQR",
                "payload": "order:42",
                "externalId": "ext-1",
                "description": "Kometa",
            },
        )

    provider = make_provider(handler)
    check = await provider.check_payment("b7a1f0b1-0000-4000-8000-000000000001")

    assert check.status is expected
    assert captured["method"] == "GET"
    assert captured["url"].endswith("/transaction/b7a1f0b1-0000-4000-8000-000000000001")
    assert captured["merchant"] == MERCHANT_ID and captured["secret"] == SECRET
    await provider.close()


async def test_check_payment_converts_amount_to_rubles():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "CONFIRMED", "paymentDetails": {"amount": 19913, "currency": "RUB"}},
        )

    provider = make_provider(handler)
    check = await provider.check_payment("tx-1")

    assert check.status is PaymentStatus.PAID
    assert check.amount == 199
    assert check.raw["paymentDetails"]["amount"] == 19913
    await provider.close()


async def test_check_payment_reads_string_amount_and_rubles_unit():
    """``"199.13 RUB"`` строкой и режим рублей — тоже разбираются."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "CONFIRMED", "paymentDetails": "199.13 RUB"})

    provider = make_provider(handler, amount_unit="rubles")
    check = await provider.check_payment("tx-1")

    assert check.amount == 199
    await provider.close()


async def test_check_payment_amount_is_none_when_unparsable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "PENDING", "paymentDetails": "не число"})

    provider = make_provider(handler)
    check = await provider.check_payment("tx-1")

    assert check.status is PaymentStatus.PENDING
    assert check.amount is None
    await provider.close()


async def test_check_payment_unknown_status_is_pending():
    """Незнакомый статус — не выдаём доступ: считаем платёж незавершённым."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "SOMETHING_NEW"})

    provider = make_provider(handler)
    check = await provider.check_payment("tx-1")

    assert check.status is PaymentStatus.PENDING
    await provider.close()


async def test_check_payment_requires_external_id():
    provider = make_provider(lambda request: httpx.Response(200, json={"status": "PENDING"}))
    with pytest.raises(PaymentError, match="идентификатор"):
        await provider.check_payment("")
    await provider.close()


# -------------------------------------------------------------------- вебхук
def test_verify_callback_accepts_our_credentials():
    provider = PlategaProvider(MERCHANT_ID, SECRET)
    assert provider.verify_callback(MERCHANT_ID, SECRET) is True


@pytest.mark.parametrize(
    ("merchant_id", "secret"),
    [
        (MERCHANT_ID, "чужой-секрет"),
        ("чужой-merchant", SECRET),
        (MERCHANT_ID, None),
        (None, SECRET),
        (None, None),
        ("", ""),
    ],
)
def test_verify_callback_rejects_bad_credentials(merchant_id, secret):
    provider = PlategaProvider(MERCHANT_ID, SECRET)
    assert provider.verify_callback(merchant_id, secret) is False


def test_verify_callback_rejects_when_provider_not_configured():
    """Пустой конфиг не должен «совпадать» с пустыми заголовками."""
    provider = PlategaProvider("", "")
    assert provider.verify_callback("", "") is False
    assert provider.verify_callback(None, None) is False


def test_parse_callback_extracts_order_and_status():
    provider = PlategaProvider(MERCHANT_ID, SECRET)

    order_id, status, raw = provider.parse_callback(
        {
            "id": "b7a1f0b1-0000-4000-8000-000000000001",
            "amount": 199.0,
            "currency": "RUB",
            "status": "CONFIRMED",
            "paymentMethod": 2,
            "payload": "order:42",
        }
    )

    assert order_id == 42
    assert status is PaymentStatus.PAID
    assert raw["id"] == "b7a1f0b1-0000-4000-8000-000000000001"


def test_parse_callback_maps_canceled():
    provider = PlategaProvider(MERCHANT_ID, SECRET)
    order_id, status, _ = provider.parse_callback({"status": "CANCELED", "payload": "order:7"})
    assert (order_id, status) == (7, PaymentStatus.CANCELED)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"status": None},
        {"payload": None, "status": "CONFIRMED"},
        {"payload": "мусор", "status": 123},
        {"payload": 42, "status": "CONFIRMED"},  # payload не строка
        {"payload": "order:не-число", "status": "CONFIRMED"},
        {"amount": 0.0, "currency": "string"},
    ],
)
def test_parse_callback_survives_garbage(body):
    """Мусор в теле вебхука не должен ронять хендлер: заказ не находим, но отвечаем."""
    provider = PlategaProvider(MERCHANT_ID, SECRET)
    order_id, status, raw = provider.parse_callback(body)

    assert raw == body
    assert order_id is None
    assert isinstance(status, PaymentStatus)
    # Единственный распознаваемый статус в этих телах — CONFIRMED.
    assert status is (PaymentStatus.PAID if body.get("status") == "CONFIRMED" else PaymentStatus.PENDING)


def test_parse_callback_handles_non_dict_body():
    """Даже если хендлер ошибся и передал не словарь — не падаем."""
    order_id, status, raw = PlategaProvider.parse_callback(None)  # type: ignore[arg-type]
    assert (order_id, status, raw) == (None, PaymentStatus.PENDING, {})


# -------------------------------------------------------------------- close()
async def test_close_closes_only_own_client():
    external = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    provider = PlategaProvider(MERCHANT_ID, SECRET, client=external)

    await provider.close()
    assert external.is_closed is False

    own = PlategaProvider(MERCHANT_ID, SECRET)
    await own.close()
    assert own.client.is_closed is True

    await external.aclose()
