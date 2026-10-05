"""Ручная оплата: перевод по СБП/на карту.

Платёж проверяется автоматически: заказу присваивается уникальная сумма
(например, 199.13 ₽), система читает выписку банка и сама находит поступление
(см. `app/services/autopay.py`). Подтверждение администратором остаётся как
страховка — кнопка «Подтвердить» в боте и веб-панели никуда не девается.
"""

from __future__ import annotations

from app.config import get_settings
from app.payments.base import Invoice, PaymentCheck, PaymentProvider, PaymentStatus


class ManualProvider(PaymentProvider):
    code = "manual"
    title = "Перевод по СБП / на карту"
    manual = True

    def __init__(self, details: str = "", note: str = "") -> None:
        settings = get_settings()
        self.details = details or settings.manual_payment_details
        self.note = note or settings.manual_payment_note

    async def create_invoice(
        self,
        order_id: int,
        amount_rub: int,
        title: str,
        *,
        price_override: int | None = None,
        exact_kopecks: int | None = None,
    ) -> Invoice:
        kopecks = exact_kopecks if exact_kopecks is not None else amount_rub * 100
        total = f"{kopecks // 100}.{kopecks % 100:02d}" if kopecks % 100 else str(kopecks // 100)

        instructions = (
            f"💸 Переведи <b>ровно {total} ₽</b> (копейки важны — по ним система "
            f"узнаёт твой платёж):\n\n"
            f"<code>{self.details or 'реквизиты не заполнены в .env'}</code>\n\n"
            f"В комментарии укажи: <code>Kometa {order_id}</code>\n"
            f"{self.note}"
        )
        return Invoice(
            provider=self.code,
            external_id=f"manual:{order_id}",
            amount_rub=amount_rub,
            instructions=instructions,
        )

    async def check_payment(self, external_id: str) -> PaymentCheck:
        # Платёж подтверждает автоплатёж по выписке (autopay) либо админ вручную.
        return PaymentCheck(status=PaymentStatus.PENDING)
