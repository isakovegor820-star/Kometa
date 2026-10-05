"""Ручная оплата: перевод по СБП/на карту и подтверждение администратором.

Самый «всеядный» способ для РФ-аудитории: работает всегда, не зависит от
эквайринга и крипты. Плата за это — ручное подтверждение.
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
    ) -> Invoice:
        instructions = (
            f"Переведи <b>{amount_rub} ₽</b> по реквизитам:\n\n"
            f"<code>{self.details or 'реквизиты не заполнены в .env'}</code>\n\n"
            f"{self.note}\n"
            f"Номер заказа: <b>#{order_id}</b>"
        )
        return Invoice(
            provider=self.code,
            external_id=f"manual:{order_id}",
            amount_rub=amount_rub,
            instructions=instructions,
        )

    async def check_payment(self, external_id: str) -> PaymentCheck:
        # Подтверждает человек (админ) в боте: см. handlers/admin.py
        return PaymentCheck(status=PaymentStatus.PENDING)
