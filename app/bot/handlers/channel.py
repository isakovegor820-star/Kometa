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

from app.bot import gate, keyboards, texts, view
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
                # Постоянное меню снизу: до гейта человек его не видел, а теперь оно
                # будет с ним всегда — ставим один раз и больше не повторяем.
                if isinstance(call.message, Message):
                        await call.message.answer(texts.MENU_BELOW, reply_markup=keyboards.reply_menu())
                await open_menu(call, session, user)
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
        """Показать главное меню вместо экрана подписки.

        Гейт стоит на самом входе, поэтому человек, прошедший его впервые, ещё не
        видел бота: показываем hero-экран с картинкой. У кого подписка уже есть —
        обычный текст, без повторной картинки.
        """
        menu = await main_menu_view(session, user, hero=True)
        message = call.message
        if isinstance(message, Message):
                with contextlib.suppress(TelegramAPIError):
                        await view.edit_screen(message, menu.text, reply_markup=menu.markup)
                        return
        # Сообщение старое и недоступное (Telegram не отдаёт содержимое) — отвечаем
        # новым, с картинкой: человек проходит гейт впервые и hero ещё не видел.
        await call.bot.send_photo(
                call.from_user.id,
                view.hero_photo(),
                caption=menu.text,
                reply_markup=menu.markup,
        )


__all__ = ["open_menu", "router"]
