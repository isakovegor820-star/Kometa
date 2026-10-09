"""Абстракция приёма платежей.

Способы оплаты меняются без правки логики подписок: бот работает через
PaymentProvider, а конкретные реализации лежат рядом (manual, cryptobot, stars).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PaymentStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    CANCELED = "canceled"
    EXPIRED = "expired"
    #: Платёж был оплачен, но деньги вернули (чарджбэк по карте). Доступ надо отключить.
    REFUNDED = "refunded"


@dataclass(slots=True)
class Invoice:
    """Счёт, который показываем пользователю."""

    provider: str
    external_id: str
    amount_rub: int = 0
    pay_url: str | None = None
    instructions: str = ""
    currency: str = "RUB"


@dataclass(slots=True)
class PaymentCheck:
    status: PaymentStatus
    amount: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentError(RuntimeError):
    """Ошибка платёжного провайдера."""


class PaymentProvider(ABC):
    code: str = "base"
    title: str = "Способ оплаты"
    #: требует ли подтверждения человеком (ручной перевод)
    manual: bool = False

    @abstractmethod
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
        """Создать счёт для заказа.

        :param price_override: цена в «родных» единицах провайдера (например,
            звёзды для Telegram Stars). Нужна там, где рублёвая цена не
            пересчитывается один-в-один: у Stars свой курс и своя сетка цен.
            Если не задана — провайдер считает сам от ``amount_rub``.
        :param exact_kopecks: точная сумма в копейках (для переводов с
            уникальной копеечной надбавкой, по которой автоплатёж находит заказ).
        :param payer_user_id: кто платит (Telegram id) — нужен провайдерам,
            которые передают данные плательщика в антифрод (Platega
            ``metadata``). Остальные просто игнорируют.
        :param payer_user_name: юзернейм или отображаемое имя плательщика.
        :param payer_ip: IP плательщика, если провайдер его требует.
        """

    @abstractmethod
    async def check_payment(self, external_id: str) -> PaymentCheck:
        """Проверить статус оплаты по внешнему идентификатору."""

    async def close(self) -> None:  # pragma: no cover
        """Освободить ресурсы."""
