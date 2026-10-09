"""Приём платежей через WATA: карты РФ и зарубежные, СБП, T-Pay, SberPay.

Документация: https://api.wata.pro/

Как это работает у нас:
1. Бот создаёт заказ → провайдер запрашивает у WATA **платёжную ссылку**
   (`POST /api/h2h/links/`) и отдаёт её пользователю кнопкой.
2. Пользователь платит на форме WATA (карта/СБП/T-Pay/SberPay).
3. WATA присылает **вебхук** с RSA-подписью → подписка выдаётся автоматически
   (см. `app/web/payments.py`). Вебхук — основной путь, опрос только как страховка.

Особенности, которые важно помнить:
* запросы допускаются **только с согласованных IP** — адрес сервера нужно
  сообщить менеджеру WATA;
* GET-запросы ограничены (1 раз в 45 секунд на объект) → не строим логику на
  опросе, опираемся на вебхуки;
* минимальная сумма платежа — 10 RUB, максимальная — 999 999.99;
* срок жизни ссылки — от 10 минут до 30 дней.

Ссылку на оплату WATA создаёт для того терминала, чей access token передан
в заголовке, поэтому ключ в `.env` — это ключ конкретного терминала.
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx

from app.payments.base import Invoice, PaymentCheck, PaymentError, PaymentProvider, PaymentStatus

logger = logging.getLogger(__name__)

PROD_URL = "https://api.wata.pro/api/h2h"
SANDBOX_URL = "https://api-sandbox.wata.pro/api/h2h"

#: Ограничения WATA на платёжную ссылку
MIN_AMOUNT_RUB = 10
MAX_AMOUNT_RUB = Decimal("999999.99")
MIN_LINK_TTL_MINUTES = 10
MAX_LINK_TTL_MINUTES = 30 * 24 * 60

#: Статусы транзакции WATA → наши статусы
_STATUS_MAP: dict[str, PaymentStatus] = {
    "paid": PaymentStatus.PAID,
    "declined": PaymentStatus.CANCELED,
    "created": PaymentStatus.PENDING,
    "pending": PaymentStatus.PENDING,
}

_ORDER_ID_RE = re.compile(r"kometa[-_#]?(\d{1,9})", re.IGNORECASE)


def make_order_id(order_id: int) -> str:
    """Идентификатор заказа в системе WATA (по нему ищем платёж в вебхуке)."""
    return f"kometa-{order_id}"


def parse_order_id(raw: str | None) -> int | None:
    """Достать номер нашего заказа из orderId вебхука."""
    if not raw:
        return None
    match = _ORDER_ID_RE.search(raw)
    if match:
        return int(match.group(1))
    return int(raw) if raw.isdigit() else None


def verify_webhook_signature(raw_body: bytes, signature: str | None, public_key_pem: str) -> bool:
    """Проверить RSA-SHA512 подпись вебхука WATA.

    :param raw_body: НЕобработанное тело запроса — любая пересборка JSON ломает подпись.
    :param signature: значение заголовка ``X-Signature`` (base64).
    :param public_key_pem: публичный ключ WATA (``GET /api/h2h/public-key``).
    """
    if not signature or not public_key_pem:
        return False
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        key = serialization.load_pem_public_key(public_key_pem.encode())
        key.verify(base64.b64decode(signature), raw_body, padding.PKCS1v15(), hashes.SHA512())
        return True
    except InvalidSignature:
        return False
    except Exception as exc:  # noqa: BLE001 - битый ключ или подпись — просто отказ
        logger.warning("Не смог проверить подпись WATA: %s", exc)
        return False


class WataProvider(PaymentProvider):
    """Платёжные ссылки WATA."""

    code = "wata"
    title = "Карта / СБП (WATA)"
    manual = False

    def __init__(
        self,
        token: str,
        *,
        base_url: str = PROD_URL,
        currency: str = "RUB",
        link_ttl_minutes: int = 30,
        success_redirect_url: str = "",
        fail_redirect_url: str = "",
        client: httpx.AsyncClient | None = None,
        public_key: str = "",
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.currency = currency
        self.link_ttl_minutes = max(MIN_LINK_TTL_MINUTES, min(link_ttl_minutes, MAX_LINK_TTL_MINUTES))
        self.success_redirect_url = success_redirect_url
        self.fail_redirect_url = fail_redirect_url
        self._public_key = public_key
        self._client = client if client is not None else httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._owns_client = client is None

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    # --- HTTP -----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self.token:
            raise PaymentError("Не задан токен WATA (WATA_TOKEN)")
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            return await self._client.request(method, url, headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise PaymentError(f"WATA недоступна: {exc}") from exc

    @staticmethod
    def _error_text(response: httpx.Response) -> str:
        try:
            data = response.json()
        except ValueError:
            return f"HTTP {response.status_code}"
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            code = error.get("code") or ""
            message = error.get("message") or data
            return f"{code} {message}".strip()
        return str(error or data)[:300]

    async def _json(self, method: str, path: str, **kwargs) -> dict:
        response = await self._request(method, path, **kwargs)
        if response.status_code == 429:
            raise PaymentError("WATA: превышен лимит запросов (не чаще 1 раза в 45 секунд)")
        if response.status_code >= 400:
            raise PaymentError(f"WATA ответила HTTP {response.status_code}: {self._error_text(response)}")
        try:
            data = response.json()
        except ValueError as exc:
            raise PaymentError("WATA вернула ответ не в формате JSON") from exc
        if not isinstance(data, dict):
            raise PaymentError("WATA вернула неожиданный формат ответа")
        return data

    # --- контракт -------------------------------------------------------
    async def create_invoice(
        self,
        order_id: int,
        amount_rub: int,
        title: str,
        *,
        price_override: int | None = None,
        exact_kopecks: int | None = None,
        payer_user_id: int | str | None = None,  # антифрод: нужен только Platega
        payer_user_name: str = "",
        payer_ip: str = "",
    ) -> Invoice:
        """Создать платёжную ссылку WATA.

        Копеечная надбавка здесь не нужна (платёж привязывается к ``orderId``),
        но если она передана — используем точную сумму.
        """
        kopecks = exact_kopecks if exact_kopecks is not None else amount_rub * 100
        try:
            amount = (Decimal(kopecks) / 100).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError) as exc:
            raise PaymentError(f"Некорректная сумма заказа: {kopecks}") from exc

        if amount < Decimal(MIN_AMOUNT_RUB):
            raise PaymentError(f"WATA принимает платежи от {MIN_AMOUNT_RUB} ₽, а сумма — {amount}")
        if amount > MAX_AMOUNT_RUB:
            raise PaymentError(f"WATA принимает платежи до {MAX_AMOUNT_RUB} ₽, а сумма — {amount}")

        expires = datetime.now(timezone.utc) + timedelta(minutes=self.link_ttl_minutes)
        body: dict[str, object] = {
            "amount": f"{amount:.2f}",
            "currency": self.currency,
            "description": (title or "Подписка Kometa")[:255],
            "orderId": make_order_id(order_id),
            "type": "OneTime",
            "expirationDateTime": expires.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        if self.success_redirect_url:
            body["successRedirectUrl"] = self.success_redirect_url
        if self.fail_redirect_url:
            body["failRedirectUrl"] = self.fail_redirect_url

        data = await self._json("POST", "links/", json=body)
        link_id = data.get("id")
        url = data.get("url")
        if not link_id or not url:
            raise PaymentError("WATA не вернула ссылку на оплату")

        return Invoice(
            provider=self.code,
            # В external_id кладём и ссылку, и заказ: по ссылке можно опросить
            # статус, по заказу — найти транзакцию.
            external_id=f"{link_id}#{order_id}",
            amount_rub=amount_rub,
            pay_url=str(url),
            currency=self.currency,
            instructions=f"К оплате {amount:.2f} ₽ — карта, СБП, T-Pay или SberPay",
        )

    async def check_payment(self, external_id: str) -> PaymentCheck:
        """Страховочная проверка статуса (основной путь — вебхук).

        Ищем транзакцию по нашему ``orderId``. Из-за лимита WATA (1 GET в 45
        секунд на объект) вызывать часто нельзя — только по кнопке пользователя.
        """
        order_id = self._order_id_from_external(external_id)
        if order_id is None:
            raise PaymentError(f"Некорректный идентификатор платежа WATA: {external_id!r}")

        data = await self._json(
            "GET", "v2/transactions", params={"OrderId": make_order_id(order_id), "maxResultCount": 10}
        )
        transaction = self._find_paid_transaction(data, order_id)
        if transaction is None:
            return PaymentCheck(status=PaymentStatus.PENDING, raw=data)

        status = _STATUS_MAP.get(str(transaction.get("transactionStatus") or "").lower(), PaymentStatus.PENDING)
        return PaymentCheck(status=status, raw=transaction)

    @staticmethod
    def _order_id_from_external(external_id: str) -> int | None:
        """external_id имеет вид ``<uuid ссылки>#<номер заказа>``."""
        if not external_id:
            return None
        tail = external_id.rsplit("#", 1)[-1]
        return int(tail) if tail.isdigit() else parse_order_id(tail)

    @staticmethod
    def _find_paid_transaction(data: dict, order_id: int) -> dict | None:
        """Найти успешную оплату в ответе поиска транзакций.

        Формат ответа у WATA менялся (v1/v2), поэтому разбираем аккуратно:
        ищем список транзакций в известных полях и берём первую оплаченную.
        """
        candidates: list = []
        for key in ("items", "transactions", "payments", "result"):
            value = data.get(key)
            if isinstance(value, list):
                candidates = value
                break
        for item in candidates:
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "Payment").lower() != "payment":
                continue
            if str(item.get("transactionStatus") or "").lower() == "paid":
                return item
        return None

    async def fetch_public_key(self) -> str:
        """Публичный ключ для проверки подписи вебхуков (кэшируем в памяти)."""
        if self._public_key:
            return self._public_key
        data = await self._json("GET", "public-key")
        value = str(data.get("value") or "")
        if not value:
            raise PaymentError("WATA не вернула публичный ключ")
        self._public_key = value
        return value

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
