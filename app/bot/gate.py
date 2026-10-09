"""Экран «подпишись на канал» — один вид на все места, где он показывается.

Живёт отдельно от хендлеров, потому что показывать его приходится и
middleware: иначе получился бы цикл импортов (middleware → хендлеры → middleware).
"""

from __future__ import annotations

from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts, view
from app.config import get_settings
from app.services import channel_gate

settings = get_settings()


def text(note: str = "") -> str:
        """Текст экрана: что сделать и зачем.

    :param note: строка поверх — например «подписки пока не видно».
    """
        body = texts.CHANNEL_GATE_BODY.format(
                subscribe_btn=texts.CHANNEL_GATE_BTN_SUBSCRIBE,
                check_btn=texts.CHANNEL_GATE_BTN_CHECK,
    )
        screen = f"{texts.CHANNEL_GATE_TITLE}\n\n{body}"
        return screen + texts.CHANNEL_GATE_NOTE.format(note=note) if note else screen


def markup() -> InlineKeyboardMarkup:
        """Кнопки: подписаться (ссылка), «я подписался», поддержка.

    Кнопку-ссылку показываем, только если канал задан: пустая кнопка хуже её
    отсутствия, а «Я подписался» работает и без неё — человек найдёт канал сам.
    """
        kb = InlineKeyboardBuilder()
        if settings.channel_link:
                kb.button(text=texts.CHANNEL_GATE_BTN_SUBSCRIBE, url=settings.channel_link)
        kb.button(text=texts.CHANNEL_GATE_BTN_CHECK, callback_data=channel_gate.CALLBACK_CHECK, style="success")
        if settings.support_username:
                kb.button(
                        text=" Поддержка",
                        url=f"https://t.me/{settings.support_username.lstrip('@')}",
        )
        kb.adjust(1)
        return kb.as_markup()


async def show(message: Message, note: str = "") -> None:
        """Показать экран подписки новым сообщением."""
        await message.answer(text(note), reply_markup=markup(), disable_web_page_preview=True)


async def show_on_call(call: CallbackQuery, note: str = "") -> None:
        """Заменить сообщение под нажатой кнопкой экраном подписки.

    У старых сообщений Telegram не отдаёт содержимое (``InaccessibleMessage``),
    поэтому редактируем только то, что действительно можно отредактировать.
    """
        if not isinstance(call.message, Message):
                return
        # Правим и текст, и подпись фото: экран подписки могли показать
        # сообщением с картинкой (см. ``view.edit_screen``).
        await view.edit_screen(call.message, text(note), markup())
