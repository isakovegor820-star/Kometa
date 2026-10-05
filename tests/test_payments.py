"""Тесты платёжных провайдеров.

Сеть не используется вообще:

* CryptoBot — ``httpx.MockTransport``, который отдаёт заранее заготовленные
  ответы Crypto Pay API и записывает ушедшие запросы;
* Stars — ``AsyncMock`` вместо :class:`aiogram.Bot`.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, Callable
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetMe
from aiogram.types import LabeledPrice

from app.payments.base import PaymentError, PaymentStatus
from app.payments.cryptobot import CryptoBotProvider
from app.payments.payload import make_order_payload
from app.payments.stars import (
    StarsProvider,
    parse_order_id_from_payload,
)

BASE_URL = "https://pay.crypt.bot/api"
TOKEN = "12345:TEST-TOKEN"
INVOICE_ID = 987654
BOT_INVOICE_URL = f"https://t.me/CryptoBot?start=IV{INVOICE_ID}"
STARS_LINK = "https://t.me/$-invoice-link"


# --------------------------------------------------------------------------
# Обвязка для Crypto Pay: запись запросов + подставные ответы
# --------------------------------------------------------------------------
def crypto_invoice(
    status: str = "active",
    amount: str = "1.06",
    invoice_id: int = INVOICE_ID,
    **extra: Any,
) -> dict[str, Any]:
    """Объект Invoice в том виде, в каком его отдаёт Crypto Pay API."""
    data: dict[str, Any] = {
        "invoice_id": invoice_id,
        "hash": "dGVzdA==",
        "currency_type": "crypto",
        "asset": "USDT",
        "amount": amount,
        "bot_invoice_url": BOT_INVOICE_URL,
        "mini_app_invoice_url": f"{BOT_INVOICE_URL}-mini",
        "web_app_invoice_url": f"{BOT_INVOICE_URL}-web",
        "description": "Kometa VPN на 1 месяц",
        "status": status,
        "created_at": "2026-01-01T00:00:00+00:00",
        "payload": "order:17",
    }
    data.update(extra)
    return data


def ok(result: Any) -> dict[str, Any]:
    return {"ok": True, "result": result}


def api_error(name: str = "AMOUNT_TOO_SMALL", code: int = 400) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "name": name}}


class Recorded:
    """Запросы, которые провайдер отправил в MockTransport."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    @property
    def last_json(self) -> dict[str, Any]:
        return self.bodies[-1]

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


@asynccontextmanager
async def crypto_provider(handler: Callable[[httpx.Request], Any], **kwargs: Any):
    """CryptoBotProvider поверх MockTransport (клиент закрываем сами)."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = CryptoBotProvider(token=kwargs.pop("token", TOKEN), client=client, **kwargs)
    try:
        yield provider
    finally:
        await client.aclose()


@asynccontextmanager
async def recording_provider(response: Any, **kwargs: Any):
    """Провайдер, который записывает запросы и всегда отвечает ``response``."""
    recorded = Recorded()

    async def handler(request: httpx.Request) -> httpx.Response:
        recorded.requests.append(request)
        recorded.bodies.append(json.loads(request.content or b"{}"))
        payload = response(request) if callable(response) else response
        return httpx.Response(200, json=payload)

    async with crypto_provider(handler, **kwargs) as provider:
        yield provider, recorded


def make_stars_bot(pay_url: str = STARS_LINK) -> AsyncMock:
    bot = AsyncMock()
    bot.create_invoice_link = AsyncMock(return_value=pay_url)
    return bot


# ==========================================================================
# CryptoBot
# ==========================================================================
async def test_create_invoice_sends_expected_request():
    async with recording_provider(ok(crypto_invoice())) as (provider, recorded):
        invoice = await provider.create_invoice(order_id=17, amount_rub=100, title="Kometa VPN на 1 месяц")

    assert recorded.last.method == "POST"
    assert str(recorded.last.url) == f"{BASE_URL}/createInvoice"
    assert recorded.last.headers["Crypto-Pay-API-Token"] == TOKEN

    body = recorded.last_json
    assert body["asset"] == "USDT"
    assert body["amount"] == "1.06"  # ceil(100 / 95) = 1.06
    assert body["payload"] == "order:17"
    assert body["description"] == "Kometa VPN на 1 месяц"

    assert invoice.provider == "crypto"
    assert invoice.external_id == str(INVOICE_ID)
    assert invoice.pay_url == BOT_INVOICE_URL
    assert invoice.amount_rub == 100
    assert invoice.currency == "USDT"


@pytest.mark.parametrize(
    ("amount_rub", "rub_per_usdt", "expected"),
    [
        (100, 95.0, "1.06"),  # 1.0526… → вверх до копейки
        (95, 95.0, "1.00"),  # ровно 1 USDT
        (10, 95.0, "1.00"),  # ниже минимума → минимум 1 USDT
        (333, 95.0, "3.51"),  # 3.50526… → вверх
        (100, 100.0, "1.00"),
        (5000, 95.0, "52.64"),  # 52.6315… → вверх
    ],
)
async def test_create_invoice_amount_and_minimum(amount_rub, rub_per_usdt, expected):
    async with recording_provider(ok(crypto_invoice()), rub_per_usdt=rub_per_usdt) as (provider, recorded):
        await provider.create_invoice(order_id=1, amount_rub=amount_rub, title="Kometa")

    assert recorded.last_json["amount"] == expected


async def test_create_invoice_amount_is_string_with_two_decimals():
    async with recording_provider(ok(crypto_invoice())) as (provider, recorded):
        await provider.create_invoice(order_id=1, amount_rub=100, title="Kometa")

    amount = recorded.last_json["amount"]
    assert isinstance(amount, str) and amount.count(".") == 1 and len(amount.split(".")[1]) == 2


async def test_create_invoice_rejects_non_positive_amount():
    async with recording_provider(ok(crypto_invoice())) as (provider, recorded):
        with pytest.raises(PaymentError, match="больше нуля"):
            await provider.create_invoice(order_id=1, amount_rub=0, title="Kometa")

    assert recorded.requests == []  # до сети дело не дошло


async def test_create_invoice_sends_expires_in_only_when_set():
    async with recording_provider(ok(crypto_invoice()), expires_in=1800) as (provider, recorded):
        await provider.create_invoice(order_id=1, amount_rub=100, title="Kometa")
    assert recorded.last_json["expires_in"] == 1800

    async with recording_provider(ok(crypto_invoice())) as (provider, recorded):
        await provider.create_invoice(order_id=1, amount_rub=100, title="Kometa")
    assert "expires_in" not in recorded.last_json


async def test_check_payment_paid():
    async with recording_provider(ok({"items": [crypto_invoice(status="paid", amount="2.00")]})) as (
        provider,
        recorded,
    ):
        check = await provider.check_payment(str(INVOICE_ID))

    assert check.status is PaymentStatus.PAID
    assert check.amount == 190  # 2.00 USDT * 95 ₽
    assert check.raw["invoice_id"] == INVOICE_ID

    assert recorded.last.method == "POST"
    assert str(recorded.last.url) == f"{BASE_URL}/getInvoices"
    assert recorded.last_json["invoice_ids"] == str(INVOICE_ID)  # строка, не список


@pytest.mark.parametrize(
    ("api_status", "expected"),
    [
        ("active", PaymentStatus.PENDING),
        ("paid", PaymentStatus.PAID),
        ("expired", PaymentStatus.EXPIRED),
        ("something_new", PaymentStatus.PENDING),  # неизвестный статус не ломает опрос
    ],
)
async def test_check_payment_status_mapping(api_status, expected):
    async with recording_provider(ok({"items": [crypto_invoice(status=api_status)]})) as (provider, _):
        check = await provider.check_payment(str(INVOICE_ID))

    assert check.status is expected


async def test_check_payment_unknown_invoice_is_pending():
    async with recording_provider(ok({"items": [], "count": 0})) as (provider, _):
        check = await provider.check_payment(str(INVOICE_ID))

    assert check.status is PaymentStatus.PENDING
    assert check.raw["items"] == []


async def test_check_payment_accepts_plain_list_result():
    """Документация обещает массив, на практике приходит {"items": [...]} — умеем оба."""
    async with recording_provider(ok([crypto_invoice(status="paid")])) as (provider, _):
        check = await provider.check_payment(str(INVOICE_ID))

    assert check.status is PaymentStatus.PAID


async def test_check_payment_rejects_bad_external_id():
    async with recording_provider(ok({"items": []})) as (provider, recorded):
        with pytest.raises(PaymentError, match="Некорректный идентификатор"):
            await provider.check_payment("order:17")

    assert recorded.requests == []


async def test_api_error_ok_false_raises_payment_error():
    async with recording_provider(api_error("AMOUNT_TOO_SMALL", 400)) as (provider, _):
        with pytest.raises(PaymentError) as exc_info:
            await provider.create_invoice(order_id=1, amount_rub=100, title="Kometa")

    message = str(exc_info.value)
    assert "AMOUNT_TOO_SMALL" in message
    assert "сумма меньше минимально допустимой" in message  # понятная расшифровка
    assert "400" in message


async def test_api_error_unknown_name_still_readable():
    async with recording_provider(api_error("SOMETHING_WEIRD", 422)) as (provider, _):
        with pytest.raises(PaymentError) as exc_info:
            await provider.check_payment(str(INVOICE_ID))

    assert "SOMETHING_WEIRD" in str(exc_info.value)


async def test_http_error_without_json_raises_payment_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>Bad Gateway</html>")

    async with crypto_provider(handler) as provider:
        with pytest.raises(PaymentError, match="502"):
            await provider.create_invoice(order_id=1, amount_rub=100, title="Kometa")


async def test_network_error_raises_payment_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with crypto_provider(handler) as provider:
        with pytest.raises(PaymentError, match="недоступен"):
            await provider.create_invoice(order_id=1, amount_rub=100, title="Kometa")


async def test_missing_token_raises_payment_error_without_request():
    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("запрос не должен уходить без токена")

    async with crypto_provider(handler, token="") as provider:
        with pytest.raises(PaymentError, match="CRYPTOBOT_TOKEN"):
            await provider.create_invoice(order_id=1, amount_rub=100, title="Kometa")


async def test_close_closes_owned_client():
    provider = CryptoBotProvider(token=TOKEN)
    await provider.close()
    assert provider.client.is_closed


async def test_close_keeps_injected_client_open():
    async with crypto_provider(lambda request: httpx.Response(200, json=ok({}))) as provider:
        await provider.close()
        assert not provider.client.is_closed


async def test_invalid_rate_raises_payment_error():
    async with recording_provider(ok(crypto_invoice()), rub_per_usdt=0) as (provider, recorded):
        with pytest.raises(PaymentError, match="rub_per_usdt"):
            await provider.create_invoice(order_id=1, amount_rub=100, title="Kometa")

    assert recorded.requests == []


# ==========================================================================
# Stars
# ==========================================================================
async def test_stars_create_invoice_uses_xtr_and_stars_price():
    bot = make_stars_bot()
    provider = StarsProvider(bot=bot, stars_per_rub=0.75)

    invoice = await provider.create_invoice(order_id=42, amount_rub=200, title="Kometa VPN на 1 месяц")

    bot.create_invoice_link.assert_awaited_once()
    kwargs = bot.create_invoice_link.await_args.kwargs
    assert kwargs["currency"] == "XTR"
    assert kwargs["payload"] == "order:42"
    assert kwargs["title"] == "Kometa VPN на 1 месяц"
    assert kwargs["description"] == "Kometa VPN на 1 месяц"

    assert len(kwargs["prices"]) == 1
    price = kwargs["prices"][0]
    assert isinstance(price, LabeledPrice)
    assert price.amount == 150  # ceil(200 * 0.75)
    assert price.label == "Kometa VPN на 1 месяц"

    assert invoice.provider == "stars"
    assert invoice.external_id == "order:42"
    assert invoice.pay_url == STARS_LINK
    assert invoice.amount_rub == 200
    assert invoice.currency == "XTR"


@pytest.mark.parametrize(
    ("amount_rub", "stars_per_rub", "expected_stars"),
    [
        (200, 0.75, 150),
        (1, 0.75, 1),  # 0.75 → минимум 1 звезда
        (10, 0.75, 8),  # 7.5 → вверх
        (3, 0.75, 3),  # 2.25 → вверх
        (1000, 0.7, 700),  # float дал бы 700.0000000000001 и лишнюю звезду
    ],
)
async def test_stars_price_rounds_up(amount_rub, stars_per_rub, expected_stars):
    bot = make_stars_bot()
    provider = StarsProvider(bot=bot, stars_per_rub=stars_per_rub)

    await provider.create_invoice(order_id=1, amount_rub=amount_rub, title="Kometa")

    price = bot.create_invoice_link.await_args.kwargs["prices"][0]
    assert price.amount == expected_stars


async def test_stars_rejects_non_positive_amount():
    bot = make_stars_bot()
    provider = StarsProvider(bot=bot)

    with pytest.raises(PaymentError, match="больше нуля"):
        await provider.create_invoice(order_id=1, amount_rub=0, title="Kometa")

    bot.create_invoice_link.assert_not_awaited()


async def test_stars_wraps_telegram_error():
    bot = make_stars_bot()
    bot.create_invoice_link = AsyncMock(
        side_effect=TelegramBadRequest(method=GetMe(), message="Bad Request: BUTTON_URL_INVALID")
    )
    provider = StarsProvider(bot=bot)

    with pytest.raises(PaymentError, match="Telegram не создал счёт Stars"):
        await provider.create_invoice(order_id=1, amount_rub=100, title="Kometa")


async def test_stars_check_payment_is_always_pending():
    """Опроса статуса у Stars нет: оплату подтверждает апдейт successful_payment."""
    bot = make_stars_bot()
    provider = StarsProvider(bot=bot)

    check = await provider.check_payment("order:42")

    assert check.status is PaymentStatus.PENDING
    assert check.raw["external_id"] == "order:42"
    bot.create_invoice_link.assert_not_awaited()


# ==========================================================================
# payload «order:<id>»
# ==========================================================================
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("order:1", 1),
        ("order:42", 42),
        ("order:0007", 7),
    ],
)
def test_parse_order_id_from_payload_valid(payload, expected):
    assert parse_order_id_from_payload(payload) == expected


@pytest.mark.parametrize(
    "payload",
    [None, "", "order:", "order:abc", "order:-5", "order:1:2", "order", "pay:5", "17", "order: 1 2", " order:7 "],
)
def test_parse_order_id_from_payload_invalid(payload):
    assert parse_order_id_from_payload(payload) is None


def test_parse_order_id_zero_means_no_order():
    """«order:0» — не заказ: в хендлере оплата проверяется как `if not order_id`."""
    assert not parse_order_id_from_payload("order:0")


def test_stars_reexports_shared_payload_parser():
    """Бот и провайдеры разбирают payload одним и тем же кодом."""
    from app.payments import payload

    assert parse_order_id_from_payload is payload.parse_order_id_from_payload


def test_make_order_payload_roundtrip():
    for order_id in (1, 17, 999):
        assert make_order_payload(order_id) == f"order:{order_id}"
        assert parse_order_id_from_payload(make_order_payload(order_id)) == order_id
