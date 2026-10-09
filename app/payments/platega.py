"""Приём платежей через Platega.io: СБП/QR, карты, крипта, SberPay.

Документация: https://docs.platega.io/ (сверена 08.10.2026, API сменилось!)

Как это работает у нас:

1. Бот создаёт заказ → провайдер просит у Platega **платёжную ссылку**
      (``POST /transaction/process``) и отдаёт её пользователю кнопкой.
2. Пользователь платит на форме Platega.
3. Platega присылает **callback** на наш URL (заголовки ``X-MerchantId`` и
      ``X-Secret`` — наш же API-ключ) → подписка выдаётся автоматически.
      ``check_payment`` — только страховка на случай, если вебхук потерялся.

Авторизация: в каждом запросе заголовки ``X-MerchantId`` (MerchantId) и
``X-Secret`` (API key). Секреты берутся из ``.env``, в коде их нет.

Методы оплаты (поле ``paymentMethod``) — актуальный ``PaymentMethodInt``:

* **2** — СБП (QR-код) + SberPay, если подключён;
* **3** — ЕРИП (Беларусь);
* **11** — карточный эквайринг (МИР/Visa/MC);
* **12** — международная оплата;
* **13** — криптовалюта;
* **14** — SberPay;
* **16** — Alipay (от 10 CNY).

Метод ``6`` — рекуррентные СБП-подписки; это не разовый платёж, у него свои
ручки ``/subscription``, поэтому в списке способов оплаты его нет.

В прежней (gitbook) документации карты шли под номером **10**, а списка
методов выше не было. Чтобы старый ``PLATEGA_METHODS=2,10`` не превратился в
400-ю ошибку на каждом платеже, ``10`` молча переводится в ``11`` — см.
:data:`LEGACY_METHOD_ALIASES`.

Идентификатор транзакции генерирует **сама Platega** и возвращает в поле
``transactionId``. Передавать своё ``id`` в запросе НЕЛЬЗЯ: в схеме
``CreateTransactionRequest`` стоит ``additionalProperties: false``, а в
документации прямо написано «не передавайте поле ``id``». Поэтому
``external_id`` нашего счёта — это ``transactionId`` из ответа; именно его
присылает callback (в теле колбэка поля ``payload`` нет вовсе, см.
``CallbackPayload``) и именно по нему вебхук находит заказ.

Единицы суммы. В актуальной схеме ``amount`` — это ``number/float`` в рублях
(``"paymentDetails": "199 RUB"``, балансы вида ``15000.5 RUB``), поэтому по
умолчанию ``amount_unit="rubles"``: отправляем рубли, например ``199.0``.
Режим ``kopecks`` (отправлять ``19900``) оставлен для совместимости, но
включать его можно только после живой проверки
``python -m app.tools.platega_check --probe``: ошибка в единицах стоит либо
100-кратной переплаты клиента, либо бесплатного доступа.
"""

from __future__ import annotations

import hmac
import logging
import re
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
#: Номера — из актуальной схемы ``PaymentMethodInt`` (docs.platega.io) плюс
#: ``16`` (Alipay) из руководства менеджера Platega от 08.10.2026.
#: Метод ``6`` (рекуррентные СБП-подписки) сюда НЕ входит: это не разовый
#: способ оплаты, у него отдельные ручки ``/subscription``.
PAYMENT_METHOD_TITLES: dict[int, tuple[str, str]] = {
        2: ("platega_sbp", "СБП / QR-код"),
        3: ("platega_erip", "ЕРИП"),
        11: ("platega_card", "Карта МИР"),
        12: ("platega_intl", "Зарубежная карта"),
        13: ("platega_crypto", "Криптовалюта"),
        14: ("platega_sberpay", "SberPay"),
        16: ("platega_alipay", "Alipay"),
}

#: Методы, которые API принимает сегодня. Всё остальное — опечатка в конфиге,
#: и лучше сказать об этом вслух, чем получить 400 на первом же платеже.
VALID_METHODS: tuple[int, ...] = tuple(sorted(PAYMENT_METHOD_TITLES))

#: Старые номера методов → новые. В прежней документации карты были ``10``,
#: теперь карточный эквайринг — ``11``. Молча переводим, чтобы конфиг
#: ``PLATEGA_METHODS=2,10`` продолжал работать.
LEGACY_METHOD_ALIASES: dict[int, int] = {10: 11}

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

#: Подсказка на повтор того же ``id`` транзакции. Сейчас ``id`` генерирует сама
#: Platega, поэтому ошибка означает ровно одно: где-то в запросе ушёл чужой
#: ``id`` (например, старый код).
_ALREADY_EXISTS_HINT = (
        "Platega: транзакция с таким id уже существует — ссылка не создана, "
        "оформите заказ заново"
)

#: Подсказки на 401 от Platega. Коды проверены живыми запросами 08.10.2026:
#: ``Auth:SIGN_1001`` — мерчант есть, а ключ не тот; ``Auth:SIGN_1002`` —
#: такого MerchantId нет вовсе. Разница важна: в первом случае искать надо
#: ключ, во втором — сам ID (частая путаница: в кабинете рядом лежит ID
#: пользователя, он не подходит).
_AUTH_HINTS: tuple[tuple[str, str], ...] = (
        (
                "merchant secret key is not correct",
                "Platega: API-ключ (X-Secret) не подходит к этому MerchantId. "
                "Скопируйте ключ заново из кабинета (Настройки проекта → API ключ); "
                "секрет для выводов (Payout API) здесь не работает.",
        ),
        (
                "merchant not exists",
                "Platega: такого MerchantId нет. Похоже, в PLATEGA_MERCHANT_ID попал "
                "ID пользователя кабинета — нужен именно ID мерчанта (Настройки проекта).",
        ),
        (
                "is not specified",
                "Platega: не переданы X-MerchantId и/или X-Secret — заполните "
                "PLATEGA_MERCHANT_ID и PLATEGA_SECRET в .env.",
        ),
)

_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def normalize_payment_method(method: int | str) -> int:
        """Привести номер метода оплаты к актуальному.

        ``10`` (карты в старой документации) → ``11`` (карточный эквайринг).
        Неизвестный номер возвращается как есть: пусть Platega сама скажет, что
        метод неверный, — глушить его молча хуже.
        """
        try:
                value = int(method)
        except (TypeError, ValueError):
                return -1
        if value in LEGACY_METHOD_ALIASES:
                logger.warning(
                        "Platega: метод %s устарел, использую %s (актуальный номер карточного эквайринга)",
                        value,
                        LEGACY_METHOD_ALIASES[value],
                )
                return LEGACY_METHOD_ALIASES[value]
        return value


def parse_payment_methods(raw: str) -> list[int]:
        """Разобрать ``PLATEGA_METHODS`` («2,11,13») в список актуальных методов.

        Дубликаты схлопываются (после замены ``10 → 11`` они вполне возможны:
        «2,10,11»), неизвестные номера отбрасываются с предупреждением — иначе
        провайдер уехал бы в реестр и падал 400-й на каждом платеже.
        """
        methods: list[int] = []
        for chunk in str(raw or "").replace(" ", "").split(","):
                if not chunk.lstrip("-").isdigit():
                        continue
                method = normalize_payment_method(chunk)
                if method not in PAYMENT_METHOD_TITLES:
                        logger.warning(
                                "Platega: метод %s не входит в актуальный список %s — пропускаю",
                                method,
                                list(VALID_METHODS),
                        )
                        continue
                if method not in methods:
                        methods.append(method)
        return methods


class PlategaProvider(PaymentProvider):
        """Платёжные ссылки Platega.

        Живёт поверх ``httpx.AsyncClient``; клиент можно передать снаружи — так
        тесты подставляют ``httpx.MockTransport`` и не ходят в сеть.

        :param merchant_id: MerchantId из личного кабинета Platega.
        :param secret: API key из личного кабинета Platega.
        :param payment_method: 2 — СБП/QR, 3 — ЕРИП, 11 — карты, 12 — зарубежные
                карты, 13 — крипта, 14 — SberPay. От него зависят ``code``/``title``
                провайдера. Устаревший ``10`` переводится в ``11``.
        :param base_url: адрес API (по умолчанию боевой; для песочницы — свой).
        :param return_url: куда вернуть пользователя после успешной оплаты.
        :param failed_url: куда вернуть пользователя после неудачной оплаты.
        :param amount_unit: единицы суммы в запросе: ``"rubles"`` (по умолчанию,
                актуальная схема API) или ``"kopecks"``.
        :param send_metadata: передавать ли в запросе ``metadata`` (userId, имя,
                IP). У части магазинов Platega это требование антифрода; включать
                после подтверждения менеджером, что поле принимается.
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
                amount_unit: str = "rubles",
                send_metadata: bool = False,
                client: httpx.AsyncClient | None = None,
        ) -> None:
                unit = str(amount_unit or "").strip().lower()
                if unit not in AMOUNT_UNITS:
                        raise ValueError(f"amount_unit должен быть одним из {AMOUNT_UNITS}, а не {amount_unit!r}")

                self.merchant_id = merchant_id
                self.secret = secret
                self.payment_method = normalize_payment_method(payment_method)
                self.base_url = base_url.rstrip("/")
                self.return_url = return_url
                self.failed_url = failed_url
                self.amount_unit = unit
                self.send_metadata = bool(send_metadata)
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
                data = await self._json_any(method, path, **kwargs)
                if not isinstance(data, dict):
                        raise PaymentError("Platega вернула неожиданный формат ответа")
                return data

        async def _json_any(self, method: str, path: str, **kwargs: Any) -> Any:
                """Ответ Platega как JSON любого вида (часть ручек отдаёт массив)."""
                response = await self._request(method, path, **kwargs)
                if response.status_code >= 400:
                        raise PaymentError(self._describe_error(response))
                try:
                        return response.json()
                except ValueError as exc:
                        raise PaymentError(
                                f"Platega вернула ответ не в формате JSON (HTTP {response.status_code})"
                        ) from exc

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
                for needle, hint in _AUTH_HINTS:
                        if needle in low:
                                return hint
                return f"Platega ответила HTTP {response.status_code}: {text}"

        def _amount_value(self, kopecks: int) -> int | float:
                """Сумма для тела запроса в единицах ``amount_unit``.

                ``rubles`` (по умолчанию) — актуальная схема API: ``amount`` там
                ``number/float``, то есть 199.13 ₽ уезжает как ``199.13``.
                ``kopecks`` оставлен для совместимости: 19913 копеек как ``19913``.
                """
                if self.amount_unit == "rubles":
                        try:
                                return float((Decimal(int(kopecks)) / 100).quantize(Decimal("0.01")))
                        except (InvalidOperation, ValueError, TypeError) as exc:
                                raise PaymentError(f"Некорректная сумма заказа: {kopecks!r}") from exc
                return int(kopecks)

        def _metadata(self, user_id: int | str | None, user_name: str, ip: str) -> dict[str, str]:
                """``metadata`` для антифрода Platega (только при ``send_metadata``).

                В схеме ``CreateTransactionRequest`` поля ``metadata`` нет, но в тексте
                документации оно обязательно для части магазинов («отсутствие
                metadata.userId отключает антифрод-защиту и может привести к отключению
                магазина»). Поэтому шлём его только по явному включению и без пустых
                ключей: лишние поля в теле — риск 400.
                """
                if not self.send_metadata or user_id in (None, ""):
                        return {}
                meta = {"userId": str(user_id)}
                if user_name:
                        meta["userName"] = str(user_name)[:64]
                if ip:
                        meta["clientIp"] = str(ip)[:45]
                return meta

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
                payer_user_id: int | str | None = None,
                payer_user_name: str = "",
                payer_ip: str = "",
        ) -> Invoice:
                """Создать платёжную ссылку Platega.

                ``price_override`` не используется: цена подписки и так в рублях.
                ``exact_kopecks`` — точная сумма в копейках (с уникальной надбавкой,
                которая помогает Platega найти реквизиты). Если не передана, берём
                ``amount_rub * 100``.

                ``id`` в запрос не кладём: его генерирует Platega и возвращает в
                ``transactionId`` — это и есть ``external_id`` счёта. ``payload``
                остаётся описанием заказа для нас (в callback он не приходит).
                ``payer_*`` уезжают в ``metadata``, только если включён
                ``send_metadata``.
                """
                kopecks = int(exact_kopecks) if exact_kopecks is not None else int(amount_rub) * 100
                if kopecks <= 0:
                        raise PaymentError("Сумма заказа должна быть больше нуля")

                body: dict[str, Any] = {
                        "paymentMethod": self.payment_method,
                        "paymentDetails": {"amount": self._amount_value(kopecks), "currency": "RUB"},
                        "description": (title or "Подписка Kometa")[:255],
                        "payload": make_order_payload(order_id),
                }
                if self.return_url:
                        body["return"] = self.return_url
                if self.failed_url:
                        body["failedUrl"] = self.failed_url
                metadata = self._metadata(payer_user_id, payer_user_name, payer_ip)
                if metadata:
                        body["metadata"] = metadata

                data = await self._json("POST", "transaction/process", json=body)

                pay_url = data.get("redirect") or data.get("payUrl") or data.get("url")
                if not pay_url:
                        raise PaymentError("Platega не вернула ссылку на оплату (поле redirect)")

                # transactionId генерирует Platega. Без него мы не сможем ни опросить
                # статус (GET /transaction/{id}), ни найти заказ по колбэку, поэтому
                # это честная ошибка, а не «как-нибудь переживём».
                transaction_id = str(data.get("transactionId") or "").strip()
                if not transaction_id:
                        raise PaymentError("Platega не вернула id транзакции (поле transactionId)")

                amount_text = f"{Decimal(kopecks) / 100:.2f}"
                ttl = self._ttl_hint(data.get("expiresIn"))
                instructions = f"К оплате {amount_text} ₽ — {self.title}."
                if ttl:
                        instructions += f" Ссылка действует {ttl}."
                instructions += " Доступ выдаётся автоматически после оплаты."

                return Invoice(
                        provider=self.code,
                        # external_id — id транзакции от Platega: именно его принимает
                        # GET /transaction/{id} и присылает callback.
                        external_id=transaction_id,
                        amount_rub=amount_rub,
                        pay_url=str(pay_url),
                        currency="RUB",
                        instructions=instructions,
                )

        async def check_payment(self, external_id: str) -> PaymentCheck:
                """Проверить статус транзакции: ``GET /transaction/{id}``.

                ``external_id`` — это ``transactionId`` из :meth:`create_invoice`.
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

        async def fetch_balances(self) -> list[dict[str, Any]]:
                """``GET /balance/all`` — балансы по валютам (включая ``frozenBalance``).

                Нужна для проверки доступов и для админки: «сколько лежит и сколько
                заморожено» — первый вопрос при работе с новым посредником.
                """
                data = await self._json_any("GET", "balance/all")
                if isinstance(data, list):
                        return [item for item in data if isinstance(item, dict)]
                # Некоторые сборки отвечают объектом вида {"balances": [...]}.
                if isinstance(data, dict):
                        for key in ("balances", "items", "data"):
                                value = data.get(key)
                                if isinstance(value, list):
                                        return [item for item in value if isinstance(item, dict)]
                        return [data]
                return []

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
