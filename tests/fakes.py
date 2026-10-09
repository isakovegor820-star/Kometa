"""Общие тестовые двойники: заглушка Telegram API и фабрика апдейтов."""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberOwner,
    ChatMemberRestricted,
    Message,
    PhotoSize,
    PreCheckoutQuery,
    SuccessfulPayment,
    Update,
    User as TgUser,
)

BOT_TOKEN = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _chat_member(user_id: int, status: str):
    """Ответ GetChatMember: подписан / вышел / админ канала.

    У администратора и ограниченного участника десятки обязательных полей,
    которые тесту не нужны, — собираем их без валидации.
    """
    member = TgUser(id=user_id, is_bot=False, first_name="Тест")
    if status == "creator":
        return ChatMemberOwner(status="creator", user=member, is_anonymous=False)
    if status == "administrator":
        return ChatMemberAdministrator.model_construct(
            status="administrator", user=member, is_member=True
        )
    if status == "restricted":
        return ChatMemberRestricted.model_construct(
            status="restricted", user=member, is_member=True
        )
    if status == "member":
        return ChatMemberMember(status="member", user=member)
    return ChatMemberLeft(status="left", user=member)


class FakeSession(BaseSession):
    """Заглушка Telegram API: запоминает методы и отдаёт правдоподобные ответы.

    Позволяет проверять сценарии бота целиком (Dispatcher + middleware +
    хендлеры) без сети.
    """

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod] = []
        #: Подписчики канала для GetChatMember: {user_id: статус}. Пусто —
        #: в канале никого; тест добавляет себя, когда «подписался».
        self.chat_members: dict[int, str] = {}
        #: Ответ Telegram вместо результата — проверка «а если API молчит».
        self.chat_member_error: Exception | None = None
        #: Сообщения, отправленные как фото: у них нет текста, только подпись.
        #: Telegram на правку текста такого сообщения отвечает ошибкой
        #: «there is no text in the message to edit» — и раньше заглушка об этом
        #: молчала, поэтому тесты не видели, что кнопки под hero-экраном падают.
        self.photo_messages: set[int] = set()
        #: Что лежит в сообщении: message_id -> (текст, клавиатура). Нужно для
        #: правила «message is not modified» — Telegram ругается на повторную
        #: правку тем же содержимым, а кнопка при этом выглядит сломанной.
        self.content: dict[int, tuple[str, str]] = {}

    @staticmethod
    def _content_key(text: str | None, markup) -> tuple[str, str]:  # noqa: ANN001
        """Отпечаток содержимого сообщения: текст + клавиатура."""
        if markup is None:
            return (text or "", "")
        dump = getattr(markup, "model_dump_json", None)
        return (text or "", dump(exclude_none=True) if dump else repr(markup))

    async def close(self) -> None:  # pragma: no cover
        return None

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        self.requests.append(method)
        name = type(method).__name__
        if name == "GetChatMember":
            if self.chat_member_error is not None:
                raise self.chat_member_error
            user_id = int(getattr(method, "user_id", 0) or 0)
            return _chat_member(user_id, self.chat_members.get(user_id, "left"))
        if name in {"SendMessage", "SendPhoto"}:
            message_id = len(self.requests)
            text = getattr(method, "text", None) or getattr(method, "caption", None) or ""
            self.content[message_id] = self._content_key(text, getattr(method, "reply_markup", None))
            if name == "SendPhoto":
                self.photo_messages.add(message_id)
                return Message.model_construct(
                    message_id=message_id,
                    date=datetime.now(timezone.utc),
                    chat=Chat(id=1, type="private"),
                    photo=[
                        PhotoSize.model_construct(
                            file_id="fake-photo-id", file_unique_id="fake-photo", width=1280, height=720
                        )
                    ],
                    caption=text,
                )
            return Message(
                message_id=message_id,
                date=datetime.now(timezone.utc),
                chat=Chat(id=1, type="private"),
                text=text or "edited",
            )
        if name in {"EditMessageText", "EditMessageCaption"}:
            message_id = int(getattr(method, "message_id", 0) or 0)
            if name == "EditMessageText" and message_id in self.photo_messages:
                # Так отвечает Telegram: у фото нет текста, править нечего.
                raise TelegramBadRequest(
                    method, "Bad Request: there is no text in the message to edit"
                )
            text = getattr(method, "text", None) or getattr(method, "caption", None) or ""
            key = self._content_key(text, getattr(method, "reply_markup", None))
            if self.content.get(message_id) == key:
                raise TelegramBadRequest(
                    method,
                    "Bad Request: message is not modified: specified new message content and "
                    "reply markup are exactly the same as a current content and reply markup "
                    "of the message",
                )
            self.content[message_id] = key
            return Message(
                message_id=message_id,
                date=datetime.now(timezone.utc),
                chat=Chat(id=1, type="private"),
                text=text or "edited",
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
        """Тексты, которые увидел человек.

        У фото-сообщения текста нет — есть подпись (``caption``). Hero-экран
        бота отправляется именно так, поэтому подпись здесь обязательна: иначе
        тесты «что видит клиент» не видели бы половину экранов.
        """
        result: list[str] = []
        for request in self.requests:
            text = getattr(request, "text", None) or getattr(request, "caption", None)
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

    def button_urls(self) -> list[str]:
        """Ссылки из inline-кнопок (и SendMessage, и EditMessageText)."""
        urls: list[str] = []
        for request in self.requests:
            markup = getattr(request, "reply_markup", None)
            if markup is None:
                continue
            for row in getattr(markup, "inline_keyboard", None) or []:
                for button in row:
                    url = getattr(button, "url", None)
                    if url:
                        urls.append(str(url))
        return urls

    def clear(self) -> None:
        self.requests.clear()
        self.photo_messages.clear()
        self.content.clear()


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


def make_photo_callback_update(callback_data: str, user_id: int = 42) -> Update:
    """Нажатие кнопки под **фото-сообщением** (hero-экран бота).

    Главное меню бот отправляет фото с подписью, и Telegram не даёт править
    текст такого сообщения — ``there is no text in the message to edit``.
    Тест на текстовом сообщении (``make_update``) этого не поймает, поэтому
    кнопки под hero проверяются отдельным апдейтом.
    """
    tg_user = TgUser(id=user_id, is_bot=False, first_name="Тест", username="tester")
    chat = Chat(id=user_id, type="private")
    message = Message.model_construct(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=chat,
        photo=[
            PhotoSize.model_construct(file_id="hero-id", file_unique_id="hero", width=1280, height=720)
        ],
        caption="меню",
    )
    return Update(
        update_id=5,
        callback_query=CallbackQuery(
            id="cb-photo",
            from_user=tg_user,
            chat_instance="ci1",
            data=callback_data,
            message=message,
        ),
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
