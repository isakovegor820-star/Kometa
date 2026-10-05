"""Общие тестовые двойники: заглушка Telegram API и фабрика апдейтов."""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    PreCheckoutQuery,
    SuccessfulPayment,
    Update,
    User as TgUser,
)

BOT_TOKEN = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


class FakeSession(BaseSession):
    """Заглушка Telegram API: запоминает методы и отдаёт правдоподобные ответы.

    Позволяет проверять сценарии бота целиком (Dispatcher + middleware +
    хендлеры) без сети.
    """

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod] = []

    async def close(self) -> None:  # pragma: no cover
        return None

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        self.requests.append(method)
        name = type(method).__name__
        if name in {"SendMessage", "EditMessageText"}:
            return Message(
                message_id=len(self.requests),
                date=datetime.now(timezone.utc),
                chat=Chat(id=1, type="private"),
                text=getattr(method, "text", "") or "edited",
            )
        if name == "GetMe":
            return TgUser(id=1, is_bot=True, first_name="Kometa", username="kometa_test_bot")
        if name == "CreateInvoiceLink":
            return "https://t.me/invoice/test-link"
        return True

    def by_name(self, name: str) -> list[TelegramMethod]:
        return [request for request in self.requests if type(request).__name__ == name]

    # --- удобные выборки для проверок -----------------------------------
    def texts(self) -> list[str]:
        result: list[str] = []
        for request in self.requests:
            text = getattr(request, "text", None)
            if isinstance(text, str) and text:
                result.append(text)
        return result

    def buttons(self) -> list[str]:
        """Тексты кнопок: цены и варианты живут именно в клавиатурах."""
        result: list[str] = []
        for request in self.requests:
            markup = getattr(request, "reply_markup", None)
            if markup is None:
                continue
            for row in getattr(markup, "inline_keyboard", None) or []:
                result.extend(button.text for button in row)
            for row in getattr(markup, "keyboard", None) or []:
                result.extend(button.text for button in row)
        return result

    def all_text(self) -> str:
        return " ".join(self.texts() + self.buttons())

    def clear(self) -> None:
        self.requests.clear()


def make_update(text: str | None = None, callback_data: str | None = None, user_id: int = 42) -> Update:
    """Собрать апдейт: сообщение или нажатие кнопки от имени пользователя."""
    tg_user = TgUser(id=user_id, is_bot=False, first_name="Тест", username="tester")
    chat = Chat(id=user_id, type="private")
    if callback_data is not None:
        message = Message(message_id=1, date=datetime.now(timezone.utc), chat=chat, text="меню")
        return Update(
            update_id=1,
            callback_query=CallbackQuery(
                id="cb1",
                from_user=tg_user,
                chat_instance="ci1",
                data=callback_data,
                message=message,
            ),
        )
    return Update(
        update_id=2,
        message=Message(message_id=2, date=datetime.now(timezone.utc), chat=chat, from_user=tg_user, text=text),
    )


def make_pre_checkout_update(order_id: int, amount: int, currency: str = "XTR", user_id: int = 42) -> Update:
    """Апдейт pre_checkout_query — Telegram спрашивает разрешение на списание."""
    return Update(
        update_id=3,
        pre_checkout_query=PreCheckoutQuery(
            id="pc1",
            from_user=TgUser(id=user_id, is_bot=False, first_name="Тест", username="tester"),
            currency=currency,
            total_amount=amount,
            invoice_payload=f"order:{order_id}",
        ),
    )


def make_stars_payment_update(order_id: int, amount: int, user_id: int = 42) -> Update:
    """Апдейт successful_payment — звёзды списаны, пора выдать доступ."""
    chat = Chat(id=user_id, type="private")
    payment = SuccessfulPayment(
        currency="XTR",
        total_amount=amount,
        invoice_payload=f"order:{order_id}",
        telegram_payment_charge_id="tg-charge-1",
        provider_payment_charge_id="provider-charge-1",
    )
    return Update(
        update_id=4,
        message=Message(
            message_id=4,
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=TgUser(id=user_id, is_bot=False, first_name="Тест", username="tester"),
            successful_payment=payment,
        ),
    )
