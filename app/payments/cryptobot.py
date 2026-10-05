"""Приём оплаты в криптовалюте через Crypto Pay API (@CryptoBot).

Документация: https://help.crypt.bot/crypto-pay-api
Базовый URL: ``https://pay.crypt.bot/api`` (тестнет: ``https://testnet-pay.crypt.bot/api``).
Авторизация: заголовок ``Crypto-Pay-API-Token``.
Параметры можно передавать в query string, телом JSON, form-data — здесь
используется POST с телом ``application/json`` (документация поддерживает
и GET, и POST). Любой ответ — JSON-объект: ``{"ok": true, "result": ...}``
при успехе и ``{"ok": false, "error": {"code": ..., "name": ...}}`` при ошибке.

Почему счёт выставляется в USDT, а не в TON:

* цена подписки фиксирована в рублях, а USDT привязан к доллару — за время
  жизни счёта (в проекте это ``order_ttl_minutes``, по умолчанию 30 минут)
  рублёвая выручка не «уплывает» из-за волатильности TON, которая на таком
  горизонте легко даёт 1–3 %;
* USDT принимается в нескольких сетях (TRC-20, ERC-20, TON), поэтому
  пользователю проще заплатить;
* у TON пришлось бы делать двойную конвертацию рубль → USD → TON.

API курса рубля не отдаёт (``getExchangeRates`` даёт курсы к USD), поэтому
курс ``rub_per_usdt`` приходит из конфига, а не из сети.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

import httpx

from app.payments.base import (
    Invoice,
    PaymentCheck,
    PaymentError,
    PaymentProvider,
    PaymentStatus,
)

# Формат payload ``order:<order_id>`` — общий для всех провайдеров
# (см. app.payments.payload, там же парсер для хендлеров бота).
from app.payments.payload import make_order_payload

#: ``Invoice.status`` в Crypto Pay → наш статус.
_STATUS_MAP: dict[str, PaymentStatus] = {
    "active": PaymentStatus.PENDING,
    "paid": PaymentStatus.PAID,
    "expired": PaymentStatus.EXPIRED,
}

#: Пояснения к частым ошибкам API (``error.name``), чтобы текст был понятен админу.
_ERROR_HINTS: dict[str, str] = {
    "TOKEN_INVALID": "неверный токен приложения Crypto Pay",
    "UNAUTHORIZED": "приложение не авторизовано",
    "AMOUNT_TOO_SMALL": "сумма меньше минимально допустимой",
    "AMOUNT_REQUIRED": "не указана сумма счёта",
    "ASSET_INVALID": "такая валюта счёта не поддерживается",
    "INVOICE_NOT_FOUND": "счёт не найден",
    "METHOD_NOT_FOUND": "такой метод API не существует",
    "PARAM_INVALID": "некорректный параметр запроса",
}


def _describe_error(error: Any, http_status: int | None = None) -> str:
    """Собрать понятный текст ошибки из поля ``error`` ответа Crypto Pay."""
    code: Any = None
    name = ""
    if isinstance(error, dict):
        code = error.get("code")
        name = str(error.get("name") or error.get("message") or "").strip()
    elif error:
        # Документация допускает и короткое строковое описание ошибки.
        name = str(error).strip()

    details: list[str] = []
    if name:
        hint = _ERROR_HINTS.get(name.upper())
        details.append(f"{name} — {hint}" if hint else name)
    if code not in (None, ""):
        details.append(f"код {code}")
    if http_status is not None:
        details.append(f"HTTP {http_status}")
    return "Crypto Pay API: " + ("; ".join(details) if details else "неизвестная ошибка")


class CryptoBotProvider(PaymentProvider):
    """Счета в крипте через Crypto Pay API.

    Живёт поверх ``httpx.AsyncClient``; клиент можно передать снаружи
    (так тесты подставляют ``httpx.MockTransport`` и не ходят в сеть).
    """

    code = "crypto"
    title = "Крипта (CryptoBot)"
    manual = False

    #: Валюта счёта. USDT — см. обоснование в докстринге модуля.
    ASSET = "USDT"
    #: Минимальная сумма счёта: у Crypto Pay лимиты порядка 1 USD.
    MIN_INVOICE_AMOUNT = Decimal("1")

    def __init__(
        self,
        token: str,
        rub_per_usdt: float = 95.0,
        base_url: str = "https://pay.crypt.bot/api",
        client: httpx.AsyncClient | None = None,
        expires_in: int | None = None,
    ) -> None:
        """
        :param token: токен приложения Crypto Pay (``CRYPTOBOT_TOKEN``).
        :param rub_per_usdt: сколько рублей стоит 1 USDT. API курса рубля не
            отдаёт, поэтому курс задаётся конфигом.
        :param base_url: адрес API (тестнет — ``https://testnet-pay.crypt.bot/api``).
        :param client: готовый httpx-клиент; если не передан — создаётся свой.
        :param expires_in: необязательный срок жизни счёта в секундах
            (Crypto Pay принимает 1…2678400). Удобно передавать
            ``order_ttl_minutes * 60``.
        """
        self.token = token
        self.rub_per_usdt = float(rub_per_usdt)
        self.base_url = base_url.rstrip("/")
        self.expires_in = expires_in
        self._client = client if client is not None else httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        self._owns_client = client is None

    # --- служебное -----------------------------------------------------
    @property
    def client(self) -> httpx.AsyncClient:
        """httpx-клиент провайдера (созданный внутри или переданный снаружи)."""
        return self._client

    def amount_in_asset(self, amount_rub: int) -> Decimal:
        """Перевести рубли в USDT: округление вверх до копейки, минимум 1 USDT.

        Округление вверх — чтобы сервис не терял деньги на курсе.
        """
        if amount_rub <= 0:
            raise PaymentError("Сумма заказа должна быть больше нуля")
        if self.rub_per_usdt <= 0:
            raise PaymentError(f"Курс rub_per_usdt должен быть больше нуля, а не {self.rub_per_usdt!r}")
        try:
            amount = Decimal(str(amount_rub)) / Decimal(str(self.rub_per_usdt))
            amount = amount.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        except (InvalidOperation, ValueError) as exc:
            raise PaymentError(f"Не удалось пересчитать рубли в USDT по курсу {self.rub_per_usdt!r}") from exc
        return max(amount, self.MIN_INVOICE_AMOUNT)

    async def _call(self, method: str, **params: Any) -> Any:
        """Вызвать метод Crypto Pay API и вернуть ``result`` или бросить PaymentError."""
        if not self.token:
            raise PaymentError("Не задан токен Crypto Pay (CRYPTOBOT_TOKEN)")

        body = {key: value for key, value in params.items() if value is not None}
        url = f"{self.base_url}/{method}"
        try:
            response = await self._client.post(
                url,
                json=body,
                headers={"Crypto-Pay-API-Token": self.token},
            )
        except httpx.HTTPError as exc:
            raise PaymentError(f"Crypto Pay API недоступен: {exc}") from exc

        try:
            data: Any = response.json()
        except ValueError:
            data = None

        if not isinstance(data, dict):
            if response.status_code >= 400:
                raise PaymentError(f"Crypto Pay API: HTTP {response.status_code} без тела ответа")
            raise PaymentError("Crypto Pay API вернул ответ не в формате JSON")
        if not data.get("ok"):
            http_status = response.status_code if response.status_code >= 400 else None
            raise PaymentError(_describe_error(data.get("error"), http_status))
        return data.get("result")

    # --- контракт PaymentProvider --------------------------------------
    async def create_invoice(
        self,
        order_id: int,
        amount_rub: int,
        title: str,
        *,
        price_override: int | None = None,
    ) -> Invoice:
        """Создать счёт в USDT на сумму заказа.

        ``price_override`` не используется: у крипты цена считается от рублей,
        а не от «родных» единиц (звёзд), как у Telegram Stars.
        """
        amount = self.amount_in_asset(amount_rub)
        params: dict[str, Any] = {
            "asset": self.ASSET,
            "amount": f"{amount:.2f}",  # API ждёт строку вида "125.50"
            "description": title,
            "payload": make_order_payload(order_id),
        }
        if self.expires_in:
            params["expires_in"] = int(self.expires_in)

        result = await self._call("createInvoice", **params)
        if not isinstance(result, dict):
            raise PaymentError("Crypto Pay API не вернул счёт в ответе createInvoice")

        invoice_id = result.get("invoice_id")
        pay_url = (
            result.get("bot_invoice_url")
            or result.get("mini_app_invoice_url")
            or result.get("web_app_invoice_url")
            or result.get("pay_url")  # устаревшее поле, но пусть будет фолбэком
        )
        if invoice_id is None or not pay_url:
            raise PaymentError("Crypto Pay API вернул счёт без invoice_id или ссылки на оплату")

        return Invoice(
            provider=self.code,
            external_id=str(invoice_id),
            amount_rub=amount_rub,
            pay_url=str(pay_url),
            currency=self.ASSET,
            instructions=f"К оплате {amount:.2f} USDT",
        )

    async def check_payment(self, external_id: str) -> PaymentCheck:
        """Проверить статус счёта через ``getInvoices``.

        ``external_id`` — это ``invoice_id`` из :meth:`create_invoice`.
        ``PaymentCheck.amount`` — ориентировочная сумма счёта в рублях
        (сумма счёта в USDT, умноженная на ``rub_per_usdt``): API курса рубля
        не знает, а бот сравнивает оплату с ценой заказа.
        """
        invoice_id = str(external_id).strip()
        if not invoice_id.isdigit():
            raise PaymentError(f"Некорректный идентификатор счёта Crypto Pay: {external_id!r}")

        result = await self._call("getInvoices", invoice_ids=invoice_id)
        invoice = self._first_invoice(result)
        if invoice is None:
            # Счёт не найден (удалён или создан другим приложением) — считаем,
            # что оплаты не было; локальный TTL заказа всё равно его закроет.
            return PaymentCheck(
                status=PaymentStatus.PENDING,
                raw={"items": [], "invoice_id": invoice_id},
            )

        status = _STATUS_MAP.get(str(invoice.get("status") or "").lower(), PaymentStatus.PENDING)
        return PaymentCheck(status=status, amount=self._amount_rub(invoice), raw=invoice)

    async def close(self) -> None:
        """Закрыть httpx-клиент. Внешний (переданный в конструктор) не трогаем."""
        if self._owns_client:
            await self._client.aclose()

    # --- разбор ответа --------------------------------------------------
    @staticmethod
    def _first_invoice(result: Any) -> dict[str, Any] | None:
        """Достать первый счёт из ответа ``getInvoices``.

        Документация обещает массив, на практике приходит
        ``{"items": [...], "count": N}`` — поддерживаем оба варианта.
        """
        items: Any = result
        if isinstance(result, dict):
            items = result.get("items", result.get("result", []))
        if isinstance(items, dict):
            items = [items]
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        return None

    def _amount_rub(self, invoice: dict[str, Any]) -> int | None:
        """Сумма счёта (в валюте счёта) → рубли по курсу ``rub_per_usdt``."""
        try:
            return round(float(invoice["amount"]) * self.rub_per_usdt)
        except (KeyError, TypeError, ValueError):
            return None
