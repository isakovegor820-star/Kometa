"""Приём платежей через Platega.io: карты МИР, СБП/QR, зарубежные карты.

Документация: https://platega.io/ (сверена 05.10.2026)

Как это работает у нас:

1. Бот создаёт заказ → провайдер просит у Platega **платёжную ссылку**
   (``POST /transaction/process``) и отдаёт её пользователю кнопкой.
2. Пользователь платит на форме Platega (СБП/QR, карта МИР, зарубежная карта).
3. Platega присылает **callback** на наш URL (заголовки ``X-MerchantId`` и
   ``X-Secret`` — наш же API-ключ) → подписка выдаётся автоматически.
   ``check_payment`` — только страховка на случай, если вебхук потерялся.

Авторизация: в каждом запросе заголовки ``X-MerchantId`` (MerchantId) и
``X-Secret`` (API key). Секреты берутся из ``.env``, в коде их нет.

Методы оплаты (поле ``paymentMethod``):

* **2** — СБП / QR-код;
* **10** — CardRu (карты МИР, 2DS);
* **12** — International.

Идентификатор транзакции — ``id`` — генерируем МЫ (uuid4) и он должен быть
уникальным: при повторе того же ``id`` Platega отвечает
``400 "Transaction <id> already exists."``. Поэтому uuid создаётся заново на
каждый вызов :meth:`PlategaProvider.create_invoice`, а не переиспользуется.

Единицы суммы — ВАЖНОЕ ДОПУЩЕНИЕ. В примерах документации сумма выглядит как
копейки (970 при 9.70 ₽), а совет поддержки «пробовать суммы вида 1001, 2002,
3001» — это ровно суммы с уникальными копейками. Поэтому по умолчанию
(``amount_unit="kopecks"``) мы отправляем **копейки**: из ``exact_kopecks``, а
если он не передан — ``amount_rub * 100``. Допущение НЕ подтверждено
документацией окончательно: его нужно проверить у менеджера Platega (у них есть
песочница и тестовый callback). Если окажется, что API ждёт рубли, достаточно
создать провайдера с ``amount_unit="rubles"`` — код менять не нужно.
"""

from __future__ import annotations

import hmac
import logging
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import httpx

from app.payments.base import (
    Invoice,
    PaymentCheck,
    PaymentError,
    PaymentProvider,
    PaymentStatus,
)
from app.payments.payload import make_order_payload, parse_order_id_from_payload

logger = logging.getLogger(__name__)

#: Боевой адрес API Platega.
PROD_URL = "https://app.platega.io"

#: Код провайдера и человеческое название для каждого метода оплаты.
#: Неизвестный метод → общий код ``platega`` (чтобы заказ не потерялся).
PAYMENT_METHOD_TITLES: dict[int, tuple[str, str]] = {
    2: ("platega_sbp", "СБП / QR-код"),
    10: ("platega_card", "Карта МИР"),
    12: ("platega_intl", "Зарубежная карта"),
}

#: Код и название провайдера по умолчанию (неизвестный метод оплаты).
DEFAULT_CODE = "platega"
DEFAULT_TITLE = "Platega"

#: Допустимые единицы суммы в запросе.
AMOUNT_UNITS = ("kopecks", "rubles")

#: Статусы транзакции Platega → наши статусы.
#: ``FAILED`` сознательно едет в ``CANCELED``: доступа по нему не будет, а
#: отдельного «провал» в нашей модели нет.
_STATUS_MAP: dict[str, PaymentStatus] = {
    "confirmed": PaymentStatus.PAID,
    "pending": PaymentStatus.PENDING,
    "expired": PaymentStatus.EXPIRED,
    "canceled": PaymentStatus.CANCELED,
    "cancelled": PaymentStatus.CANCELED,  # встречается в англоязычных ответах
    "failed": PaymentStatus.CANCELED,
    # Возврат денег плательщику: платёж был успешным, но деньги ушли обратно.
    # Обрабатываем отдельным статусом — по нему отключаем доступ.
    "chargebacked": PaymentStatus.REFUNDED,
    "chargeback": PaymentStatus.REFUNDED,
    "refunded": PaymentStatus.REFUNDED,
}

#: Подсказка на ошибку «No available requisites»: у Platega P2P-реквизиты
#: подбираются под сумму, поэтому круглые суммы часто «не находятся».
_NO_REQUISITES_HINT = (
    "Platega: на эту сумму не нашлось реквизитов — попробуйте другую сумму "
    "(у Platega для P2P нужны уникальные копейки, например 1001, 2002, 3001)"
)

#: Подсказка на повтор нашего же ``id`` транзакции.
_ALREADY_EXISTS_HINT = (
    "Platega: транзакция с таким id уже существует — ссылка не создана, "
    "оформите заказ заново"
)

_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


class PlategaProvider(PaymentProvider):
    """Платёжные ссылки Platega.

    Живёт поверх ``httpx.AsyncClient``; клиент можно передать снаружи — так
    тесты подставляют ``httpx.MockTransport`` и не ходят в сеть.

    :param merchant_id: MerchantId из личного кабинета Platega.
    :param secret: API key из личного кабинета Platega.
    :param payment_method: 2 — СБП/QR, 10 — CardRu (МИР), 12 — International.
        От него зависят ``code``/``title`` провайдера.
    :param base_url: адрес API (по умолчанию боевой; для песочницы — свой).
    :param return_url: куда вернуть пользователя после успешной оплаты.
    :param failed_url: куда вернуть пользователя после неудачной оплаты.
    :param amount_unit: единицы суммы в запросе: ``"kopecks"`` (по умолчанию,
        см. допущение в докстринге модуля) или ``"rubles"``.
    :param client: готовый httpx-клиент; если не передан — создаётся свой.
    """

    manual = False

    def __init__(
        self,
        merchant_id: str,
        secret: str,
        *,
        payment_method: int = 2,
        base_url: str = PROD_URL,
        return_url: str = "",
        failed_url: str = "",
        amount_unit: str = "kopecks",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        unit = str(amount_unit or "").strip().lower()
        if unit not in AMOUNT_UNITS:
            raise ValueError(f"amount_unit должен быть одним из {AMOUNT_UNITS}, а не {amount_unit!r}")

        self.merchant_id = merchant_id
        self.secret = secret
        self.payment_method = int(payment_method)
        self.base_url = base_url.rstrip("/")
        self.return_url = return_url
        self.failed_url = failed_url
        self.amount_unit = unit
        # code/title у PaymentProvider — атрибуты класса, поэтому для конкретного
        # метода оплаты подменяем их у экземпляра.
        self.code, self.title = PAYMENT_METHOD_TITLES.get(
            self.payment_method, (DEFAULT_CODE, DEFAULT_TITLE)
        )
        self._client = client if client is not None else httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._owns_client = client is None

    # --- служебное ------------------------------------------------------
    @property
    def client(self) -> httpx.AsyncClient:
        """httpx-клиент провайдера (созданный внутри или переданный снаружи)."""
        return self._client

    def _headers(self) -> dict[str, str]:
        """Заголовки авторизации Platega (нужны и в API-запросах, и в вебхуках)."""
        return {
            "X-MerchantId": self.merchant_id,
            "X-Secret": self.secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _ensure_configured(self) -> None:
        if not self.merchant_id or not self.secret:
            raise PaymentError("Не заданы доступы Platega (PLATEGA_MERCHANT_ID / PLATEGA_SECRET)")

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self._ensure_configured()
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            return await self._client.request(method, url, headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise PaymentError(f"Platega недоступна: {exc}") from exc

    async def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._request(method, path, **kwargs)
        if response.status_code >= 400:
            raise PaymentError(self._describe_error(response))
        try:
            data = response.json()
        except ValueError as exc:
            raise PaymentError(
                f"Platega вернула ответ не в формате JSON (HTTP {response.status_code})"
            ) from exc
        if not isinstance(data, dict):
            raise PaymentError("Platega вернула неожиданный формат ответа")
        return data

    @staticmethod
    def _error_text(response: httpx.Response) -> str:
        """Достать текст ошибки из ответа Platega (JSON или просто тело)."""
        try:
            data: Any = response.json()
        except ValueError:
            text = (response.text or "").strip()
            return text[:300] if text else f"HTTP {response.status_code}"
        if isinstance(data, dict):
            for key in ("message", "error", "detail", "description", "title"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:300]
                if isinstance(value, dict):
                    nested = value.get("message") or value.get("error")
                    if isinstance(nested, str) and nested.strip():
                        return nested.strip()[:300]
            return str(data)[:300]
        return str(data)[:300]

    @staticmethod
    def _describe_error(response: httpx.Response) -> str:
        """Понятный русский текст ошибки по ответу Platega."""
        text = PlategaProvider._error_text(response)
        low = text.lower()
        if "no available requisites" in low:
            return _NO_REQUISITES_HINT
        if "already exists" in low:
            return _ALREADY_EXISTS_HINT
        return f"Platega ответила HTTP {response.status_code}: {text}"

    def _amount_value(self, kopecks: int) -> int | float:
        """Сумма для тела запроса в единицах ``amount_unit``.

        По умолчанию — копейки (см. допущение в докстринге модуля).
        """
        if self.amount_unit == "rubles":
            try:
                return float((Decimal(int(kopecks)) / 100).quantize(Decimal("0.01")))
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise PaymentError(f"Некорректная сумма заказа: {kopecks!r}") from exc
        return int(kopecks)

    @staticmethod
    def _ttl_hint(expires_in: Any) -> str:
        """``"00:15:00"`` из ответа Platega → ``"15 мин"`` для текста пользователю."""
        raw = str(expires_in or "").strip()
        if not raw:
            return ""
        parts = raw.split(":")
        if len(parts) == 3 and all(part.strip().isdigit() for part in parts):
            hours, minutes, seconds = (int(part) for part in parts)
            total_minutes = hours * 60 + minutes
            if not total_minutes and seconds:
                return f"{seconds} сек"
            return f"{total_minutes} мин"
        return raw

    # --- контракт PaymentProvider ---------------------------------------
    async def create_invoice(
        self,
        order_id: int,
        amount_rub: int,
        title: str,
        *,
        price_override: int | None = None,
        exact_kopecks: int | None = None,
    ) -> Invoice:
        """Создать платёжную ссылку Platega.

        ``price_override`` не используется: цена подписки и так в рублях.
        ``exact_kopecks`` — точная сумма в копейках (с уникальной надбавкой,
        которая помогает Platega найти реквизиты). Если не передана, берём
        ``amount_rub * 100``.

        В ``payload`` кладём ``order:<order_id>`` — по нему :meth:`parse_callback`
        находит заказ в вебхуке.
        """
        kopecks = int(exact_kopecks) if exact_kopecks is not None else int(amount_rub) * 100
        if kopecks <= 0:
            raise PaymentError("Сумма заказа должна быть больше нуля")

        # id транзакции генерируем сами: он должен быть уникальным, повтор
        # того же id Platega отвергает (400 "Transaction <id> already exists.").
        transaction_id = str(uuid.uuid4())
        body: dict[str, Any] = {
            "paymentMethod": self.payment_method,
            "id": transaction_id,
            "paymentDetails": {"amount": self._amount_value(kopecks), "currency": "RUB"},
            "description": (title or "Подписка Kometa")[:255],
            "payload": make_order_payload(order_id),
        }
        if self.return_url:
            body["return"] = self.return_url
        if self.failed_url:
            body["failedUrl"] = self.failed_url

        data = await self._json("POST", "transaction/process", json=body)

        pay_url = data.get("redirect") or data.get("payUrl") or data.get("url")
        if not pay_url:
            raise PaymentError("Platega не вернула ссылку на оплату (поле redirect)")

        amount_text = f"{Decimal(kopecks) / 100:.2f}"
        ttl = self._ttl_hint(data.get("expiresIn"))
        instructions = f"К оплате {amount_text} ₽ — {self.title}."
        if ttl:
            instructions += f" Ссылка действует {ttl}."
        instructions += " Доступ выдаётся автоматически после оплаты."

        return Invoice(
            provider=self.code,
            # external_id — наш же id транзакции: именно его принимает
            # GET /transaction/{id} и присылает в callback.
            external_id=transaction_id,
            amount_rub=amount_rub,
            pay_url=str(pay_url),
            currency="RUB",
            instructions=instructions,
        )

    async def check_payment(self, external_id: str) -> PaymentCheck:
        """Проверить статус транзакции: ``GET /transaction/{id}``.

        ``external_id`` — это ``id`` из :meth:`create_invoice` (наш uuid4).
        ``PaymentCheck.amount`` — сумма в рублях; она приводится из
        ``paymentDetails`` в тех же единицах, что заданы ``amount_unit``
        (см. допущение в докстринге модуля). Если разобрать сумму не удалось —
        возвращаем ``None``, а не выдуманное число.
        """
        transaction_id = str(external_id or "").strip()
        if not transaction_id:
            raise PaymentError("Не задан идентификатор транзакции Platega")

        data = await self._json("GET", f"transaction/{quote(transaction_id, safe='')}")
        raw_status = str(data.get("status") or "").strip().lower()
        try:
            status = _STATUS_MAP[raw_status]
        except KeyError:
            # Незнакомый статус — не рискуем выдать доступ, но и не роняем опрос.
            logger.warning("Platega вернула неизвестный статус %r — считаем платёж незавершённым", raw_status)
            status = PaymentStatus.PENDING
        return PaymentCheck(status=status, amount=self._amount_rub(data), raw=data)

    async def close(self) -> None:
        """Закрыть httpx-клиент. Внешний (переданный в конструктор) не трогаем."""
        if self._owns_client:
            await self._client.aclose()

    # --- вебхук ---------------------------------------------------------
    def verify_callback(self, merchant_id: str | None, secret: str | None) -> bool:
        """Проверить, что callback пришёл от Platega, а не от постороннего.

        Platega подписывает вебхуки нашими же заголовками ``X-MerchantId`` и
        ``X-Secret`` (то есть это фактически «секрет в заголовке», а не
        подпись тела), поэтому сравниваем их постоянным по времени
        ``hmac.compare_digest``. Пустые значения и незаполненный конфиг —
        отказ.
        """
        if not merchant_id or not secret:
            return False
        if not self.merchant_id or not self.secret:
            return False
        return hmac.compare_digest(
            merchant_id.encode("utf-8"), self.merchant_id.encode("utf-8")
        ) and hmac.compare_digest(secret.encode("utf-8"), self.secret.encode("utf-8"))

    @staticmethod
    def parse_callback(body: dict[str, Any]) -> tuple[int | None, PaymentStatus, dict[str, Any]]:
        """Разобрать тело вебхука: ``(номер заказа, статус, сырые данные)``.

        Тело: ``{"id": ..., "amount": ..., "currency": ..., "status":
        "CONFIRMED|CANCELED", "paymentMethod": ...}`` — плюс наш ``payload``
        (``order:<id>``), который мы передавали при создании ссылки.
        Функция устойчива к мусору: неизвестные поля и отсутствующий
        ``payload`` дают ``(None, PaymentStatus.PENDING, body)`` без исключений.
        """
        data: dict[str, Any] = body if isinstance(body, dict) else {}
        raw_payload = data.get("payload")
        order_id = parse_order_id_from_payload(raw_payload if isinstance(raw_payload, str) else None)
        status = _STATUS_MAP.get(str(data.get("status") or "").strip().lower(), PaymentStatus.PENDING)
        return order_id, status, data

    # --- разбор ответа --------------------------------------------------
    def _amount_rub(self, data: dict[str, Any]) -> int | None:
        """Сумма из ответа Platega → рубли (или ``None``, если не разобрали).

        ``paymentDetails`` приходит и объектом (``{"amount": 2000}``), и строкой
        вида ``"100 RUB"`` — поддерживаем оба варианта. Единицы те же, что мы
        отправляли (``amount_unit``): при копейках делим на 100.
        """
        details = data.get("paymentDetails")
        raw: Any = details.get("amount") if isinstance(details, dict) else details
        if raw is None:
            return None
        if isinstance(raw, bool):
            return None
        if isinstance(raw, (int, float)):
            value = Decimal(str(raw))
        elif isinstance(raw, str):
            match = _NUMBER_RE.search(raw)
            if match is None:
                return None
            try:
                value = Decimal(match.group(0).replace(",", "."))
            except InvalidOperation:
                return None
        else:
            return None

        try:
            if self.amount_unit == "rubles":
                return int(value.to_integral_value())
            return int((value / 100).to_integral_value())
        except (InvalidOperation, ValueError, OverflowError):  # pragma: no cover - защита от мусора
            return None
