"""Тесты приёма платежей через Platega: ссылка, статусы, вебхук, ошибки.

Сеть не используется: httpx-клиент подменяется ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from app.payments.base import PaymentError, PaymentStatus
from app.payments.platega import PlategaProvider, parse_payment_methods

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
    assert body["paymentDetails"] == {"amount": 199.0, "currency": "RUB"}
    assert body["payload"] == "order:42"
    assert body["description"] == "Kometa: 1 месяц"
    assert body["return"] == "https://kometa.example/ok"
    assert body["failedUrl"] == "https://kometa.example/fail"

    # id транзакции генерирует Platega: со своим id API отвечает 400
    # (в схеме запроса additionalProperties: false, поле прямо запрещено).
    assert "id" not in body
    assert "metadata" not in body

    await provider.close()


async def test_create_invoice_takes_transaction_id_from_response():
    """external_id — это transactionId от Platega, а не наш uuid.

    Иначе вебхук (в колбэке нет payload, только id транзакции) не найдёт заказ,
    и оплата не выдаст доступ.
    """
    bodies: list[dict] = []
    served: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        tx = f"b7a1f0b1-0000-4000-8000-0000000000{len(bodies):02d}"
        served.append(tx)
        return httpx.Response(200, json=process_response(transactionId=tx))

    provider = make_provider(handler)
    first = await provider.create_invoice(1, 199, "Kometa")
    second = await provider.create_invoice(1, 199, "Kometa")

    assert "id" not in bodies[0] and "id" not in bodies[1]
    assert first.external_id == served[0]
    assert second.external_id == served[1]
    assert first.external_id != second.external_id
    await provider.close()


async def test_create_invoice_without_transaction_id_is_reported():
    """Нет transactionId — нет ни опроса статуса, ни поиска заказа. Честная ошибка."""

    def handler(request: httpx.Request) -> httpx.Response:
        data = process_response()
        data.pop("transactionId")
        return httpx.Response(200, json=data)

    provider = make_provider(handler)
    with pytest.raises(PaymentError, match="transactionId"):
        await provider.create_invoice(1, 199, "Kometa")
    await provider.close()


async def test_create_invoice_returns_pay_url_and_external_id():
    """Разбор ответа: pay_url из redirect, external_id — transactionId от Platega."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=process_response())

    provider = make_provider(handler, payment_method=11)
    invoice = await provider.create_invoice(42, 199, "Kometa: 1 месяц")

    assert invoice.provider == "platega_card"
    assert invoice.pay_url == REDIRECT
    assert invoice.external_id == "b7a1f0b1-0000-4000-8000-000000000001"
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

    # По умолчанию рубли: 19913 копеек — это 199.13 ₽.
    assert captured["body"]["paymentDetails"]["amount"] == 199.13
    assert "199.13" in invoice.instructions
    await provider.close()


async def test_amount_unit_kopecks_sends_kopecks():
    """Режим копеек оставлен для совместимости: сумма уходит целым числом."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=process_response())

    provider = make_provider(handler, amount_unit="kopecks")
    await provider.create_invoice(42, 199, "Kometa", exact_kopecks=19913)

    assert captured["body"]["paymentDetails"]["amount"] == 19913
    await provider.close()


async def test_amount_unit_rubles_sends_rubles():
    """amount_unit="rubles" (по умолчанию) — сумма уходит рублями с копейками."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=process_response())

    provider = make_provider(handler, amount_unit="rubles")
    await provider.create_invoice(42, 199, "Kometa", exact_kopecks=19913)

    assert captured["body"]["paymentDetails"]["amount"] == 199.13
    await provider.close()


async def test_default_amount_unit_is_rubles():
    """По умолчанию — рубли: актуальная схема API.

    Ошибка в эту сторону безопаснее: при копейках клиент увидел бы счёт
    в 100 раз больше.
    """
    assert PlategaProvider(MERCHANT_ID, SECRET).amount_unit == "rubles"


# ------------------------------------------------------------------ metadata
async def test_metadata_is_sent_only_when_enabled():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=process_response())

    provider = make_provider(handler, send_metadata=True)
    await provider.create_invoice(
        42, 199, "Kometa", payer_user_id=926194553, payer_user_name="@egor", payer_ip="10.0.0.1"
    )

    assert captured["body"]["metadata"] == {
        "userId": "926194553",
        "userName": "@egor",
        "clientIp": "10.0.0.1",
    }
    await provider.close()

    captured.clear()
    off = make_provider(handler)  # по умолчанию metadata не шлём
    await off.create_invoice(42, 199, "Kometa", payer_user_id=926194553)
    assert "metadata" not in captured["body"]
    await off.close()


async def test_metadata_skips_empty_fields():
    """Без имени и IP уезжает только userId: лишние ключи — риск 400."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=process_response())

    provider = make_provider(handler, send_metadata=True)
    await provider.create_invoice(42, 199, "Kometa", payer_user_id=5)

    assert captured["body"]["metadata"] == {"userId": "5"}
    await provider.close()


async def test_metadata_absent_without_payer():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=process_response())

    provider = make_provider(handler, send_metadata=True)
    await provider.create_invoice(42, 199, "Kometa")

    assert "metadata" not in captured["body"]
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
    assert PlategaProvider(MERCHANT_ID, SECRET, payment_method=11).code == "platega_card"
    assert PlategaProvider(MERCHANT_ID, SECRET, payment_method=11).title == "Карта МИР"
    assert PlategaProvider(MERCHANT_ID, SECRET, payment_method=12).code == "platega_intl"
    assert PlategaProvider(MERCHANT_ID, SECRET, payment_method=12).title == "Зарубежная карта"
    assert PlategaProvider(MERCHANT_ID, SECRET, payment_method=13).code == "platega_crypto"
    assert PlategaProvider(MERCHANT_ID, SECRET, payment_method=14).code == "platega_sberpay"
    unknown = PlategaProvider(MERCHANT_ID, SECRET, payment_method=99)
    assert unknown.code == "platega"
    assert unknown.title


def test_legacy_card_method_10_becomes_11():
    """Старая настройка «карты = 10» не должна превращаться в 400 на платеже."""
    provider = PlategaProvider(MERCHANT_ID, SECRET, payment_method=10)
    assert provider.payment_method == 11
    assert provider.code == "platega_card"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2,11", [2, 11]),
        ("2, 10", [2, 11]),  # 10 — старый номер карт
        ("2,10,11", [2, 11]),  # дубликат после замены схлопывается
        ("2,99,7", [2]),  # чужие номера отбрасываются
        ("", []),
        ("мусор", []),
    ],
)
def test_parse_payment_methods(raw, expected):
    assert parse_payment_methods(raw) == expected


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


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # Реальные ответы API, проверены живыми запросами 08.10.2026.
        ("Merchant secret key is not correct.", "API-ключ"),
        ("Merchant not exists.", "ID мерчанта"),
        ("X-MerchantId or X-Secret is not specified.", "PLATEGA_MERCHANT_ID"),
    ],
)
async def test_auth_errors_explain_what_to_fix(message, expected):
    """401 от Platega должен называть, что именно поправить, а не «HTTP 401»."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "Auth:SIGN_1001", "message": message})

    provider = make_provider(handler)
    with pytest.raises(PaymentError) as exc:
        await provider.create_invoice(1, 100, "Kometa")

    assert expected in str(exc.value)
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
    """В режиме рублей сумма из paymentDetails — уже рубли."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "CONFIRMED", "paymentDetails": {"amount": 199, "currency": "RUB"}},
        )

    provider = make_provider(handler)
    check = await provider.check_payment("tx-1")

    assert check.status is PaymentStatus.PAID
    assert check.amount == 199
    assert check.raw["paymentDetails"]["amount"] == 199
    await provider.close()


async def test_check_payment_kopecks_mode_divides_by_100():
    """В режиме копеек та же сумма — 19913 копеек = 199 ₽."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "CONFIRMED", "paymentDetails": {"amount": 19913, "currency": "RUB"}},
        )

    provider = make_provider(handler, amount_unit="kopecks")
    check = await provider.check_payment("tx-1")

    assert check.amount == 199
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
