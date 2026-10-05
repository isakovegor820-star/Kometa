"""Оплата Telegram Stars (валюта ``XTR``) прямо внутри бота.

Как это работает у Telegram:

* бот создаёт **ссылку на счёт** методом ``createInvoiceLink``
  (в aiogram — :meth:`aiogram.Bot.create_invoice_link`) и отправляет её
  пользователю кнопкой;
* пользователь платит звёздами, после чего Telegram присылает боту апдейт
  ``pre_checkout_query``, а затем ``message.successful_payment``.

Из этого следуют две вещи:

1. опрашивать статус счёта по API невозможно — «спросить» Telegram, оплачен ли
   счёт, нечем. Факт оплаты приходит апдейтом, который обрабатывает хендлер
   бота (см. :meth:`StarsProvider.check_payment`);
2. связь между оплатой и заказом держится на ``payload`` счёта
   (``order:<order_id>``). Хендлер достаёт id заказа из
   ``successful_payment.invoice_payload`` через
   :func:`app.payments.payload.parse_order_id_from_payload` (реэкспортирован
   отсюда).

Курс звёзд к рублю задаётся конфигом (``stars_per_rub``): Telegram продаёт
звёзды пачками с разной ценой за штуку, поэтому «официального» курса нет.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal, InvalidOperation

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import LabeledPrice

from app.payments.base import (
    Invoice,
    PaymentCheck,
    PaymentError,
    PaymentProvider,
    PaymentStatus,
)

# Формат payload ``order:<order_id>`` живёт в общем модуле app.payments.payload:
# его же использует хендлер successful_payment, поэтому парсер ровно один.
# Отсюда parse_order_id_from_payload реэкспортируется — боту удобно брать его
# рядом с провайдером.
from app.payments.payload import (
    PAYLOAD_PREFIX,
    make_order_payload,
    parse_order_id_from_payload,
)

__all__ = [
    "PAYLOAD_PREFIX",
    "StarsProvider",
    "make_order_payload",
    "parse_order_id_from_payload",
]

#: Запасной заголовок счёта, если переданный окажется пустым.
DEFAULT_TITLE = "Подписка Kometa VPN"

#: Лимиты Telegram: title — 1..32 символа, description — 1..255.
TITLE_LIMIT = 32
DESCRIPTION_LIMIT = 255


class StarsProvider(PaymentProvider):
    """Счёт в Telegram Stars: бот отдаёт пользователю ссылку на оплату."""

    code = "stars"
    title = "Telegram Stars ⭐"
    manual = False

    def __init__(self, bot: Bot, stars_per_rub: float = 0.75) -> None:
        """
        :param bot: экземпляр :class:`aiogram.Bot` — через него создаётся
            ссылка на счёт.
        :param stars_per_rub: сколько звёзд стоит 1 рубль (по умолчанию 0.75,
            то есть 1 звезда ≈ 1.33 ₽).
        """
        self.bot = bot
        self.stars_per_rub = float(stars_per_rub)

    # --- служебное -----------------------------------------------------
    def stars_for_rub(self, amount_rub: int) -> int:
        """Цена в звёздах: округление вверх, минимум 1 звезда.

        Считаем через :class:`~decimal.Decimal`, а не float: иначе
        ``1000 * 0.7`` даёт ``700.0000000000001`` и лишняя звезда.
        """
        if amount_rub <= 0:
            raise PaymentError("Сумма заказа должна быть больше нуля")
        if self.stars_per_rub <= 0:
            raise PaymentError(f"Курс stars_per_rub должен быть больше нуля, а не {self.stars_per_rub!r}")
        try:
            stars = (Decimal(str(amount_rub)) * Decimal(str(self.stars_per_rub))).to_integral_value(
                rounding=ROUND_CEILING
            )
        except (InvalidOperation, ValueError) as exc:
            raise PaymentError(f"Не удалось пересчитать рубли в звёзды по курсу {self.stars_per_rub!r}") from exc
        return max(1, int(stars))

    @staticmethod
    def _invoice_texts(title: str) -> tuple[str, str]:
        """Заголовок и описание счёта с учётом лимитов Telegram (32 и 255)."""
        text = " ".join(str(title or "").split()) or DEFAULT_TITLE
        return text[:TITLE_LIMIT], text[:DESCRIPTION_LIMIT]

    # --- контракт PaymentProvider --------------------------------------
    async def create_invoice(self, order_id: int, amount_rub: int, title: str) -> Invoice:
        """Создать ссылку на счёт в звёздах.

        ``external_id`` — это payload счёта (``order:<order_id>``): другого
        идентификатора у ссылки на счёт нет, а по нему Telegram вернёт заказ
        в апдейте ``successful_payment``.
        """
        stars = self.stars_for_rub(amount_rub)
        payload = make_order_payload(order_id)
        title_text, description = self._invoice_texts(title)

        try:
            pay_url = await self.bot.create_invoice_link(
                title=title_text,
                description=description,
                payload=payload,
                currency="XTR",
                prices=[LabeledPrice(label=title_text, amount=stars)],
            )
        except TelegramAPIError as exc:
            raise PaymentError(f"Telegram не создал счёт Stars: {exc}") from exc

        if not pay_url:
            raise PaymentError("Telegram вернул пустую ссылку на счёт Stars")

        return Invoice(
            provider=self.code,
            external_id=payload,
            amount_rub=amount_rub,
            pay_url=str(pay_url),
            currency="XTR",
            instructions=f"К оплате {stars} ⭐",
        )

    async def check_payment(self, external_id: str) -> PaymentCheck:
        """Всегда ``PENDING`` — опросить статус счёта Stars невозможно.

        В отличие от Crypto Pay, у Telegram нет метода «дай статус счёта»:
        ``createInvoiceLink`` только создаёт ссылку, а факт оплаты приходит
        боту апдейтом ``message.successful_payment``. Поэтому провайдер честно
        сообщает «не знаю», а заказ подтверждает хендлер бота по
        ``successful_payment.invoice_payload`` (см.
        :func:`parse_order_id_from_payload`). Ждать оплату вечно тоже не нужно —
        заказ закрывается по локальному TTL.
        """
        return PaymentCheck(
            status=PaymentStatus.PENDING,
            raw={
                "provider": self.code,
                "external_id": external_id,
                "note": "Stars подтверждаются апдейтом successful_payment, а не опросом API",
            },
        )
