"""Гейт обязательной подписки на канал: кнопка «Я подписался».

Экран показывает middleware (``ChannelGateMiddleware``) — здесь только реакция
на кнопку. Проверяем заново, не глядя на кэш: человек только что подписался и
ждёт, что его пустят, а не «попробуй через 12 часов».
"""

from __future__ import annotations

import contextlib

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import gate, keyboards, texts
from app.bot.handlers.start import main_menu_view
from app.db.models import User
from app.services import channel_gate

router = Router(name="channel")


@router.callback_query(F.data == channel_gate.CALLBACK_CHECK)
async def cb_check_subscription(
    call: CallbackQuery, session: AsyncSession, user: User, bot: Bot
) -> None:
    """Проверить подписку по кнопке и открыть меню, если она есть."""
    # force=True: человек только что подписался — проверяем заново, а не
    # отвечаем «не подписан» из кэша пятнадцатисекундной давности.
    result = await channel_gate.verdict(session, bot, user, force=True)

    if result.reason == "off":
        # Кнопка осталась от старого сообщения, а гейт уже выключен.
        await call.answer()
        await open_menu(call, session, user)
        return

    if result.subscribed is True:
        await call.answer(texts.CHANNEL_GATE_OK, show_alert=True)
        await open_menu(call, session, user)
        # Постоянное меню снизу: до гейта человек его не видел.
        if isinstance(call.message, Message):
            await call.message.answer(texts.QUICK_MENU_HINT, reply_markup=keyboards.reply_menu())
        return

    if result.subscribed is False:
        await call.answer(
            texts.CHANNEL_GATE_NOT_YET.format(check_btn=texts.CHANNEL_GATE_BTN_CHECK),
            show_alert=True,
        )
        # Экран оставляем на месте: человеку нужно вернуться в канал и нажать
        # кнопку ещё раз, а не искать меню заново.
        await gate.show_on_call(call)
        return

    # Проверить не удалось: Telegram молчит или бот не админ канала.
    await call.answer(texts.CHANNEL_GATE_UNAVAILABLE, show_alert=True)
    if result.allowed:
        await open_menu(call, session, user)


async def open_menu(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    """Показать главное меню вместо экрана подписки."""
    text, markup = await main_menu_view(session, user)
    message = call.message
    if isinstance(message, Message):
        with contextlib.suppress(TelegramAPIError):
            await message.edit_text(text, reply_markup=markup)
            return
    # Сообщение старое и недоступное — отвечаем новым.
    await call.bot.send_message(call.from_user.id, text, reply_markup=markup)


__all__ = ["open_menu", "router"]
