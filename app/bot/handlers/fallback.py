"""Фолбэк для пользователя: что отвечать на непонятное сообщение.

Подключается ПОСЛЕДНИМ: до него срабатывают все осмысленные хендлеры.
Админские команды перехватываются роутером admin (он подключается раньше).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts, view
from app.bot.handlers.start import main_menu_view
from app.db.models import User

router = Router(name="fallback")


@router.message(F.text)
async def fallback_text(message: Message, session: AsyncSession, user: User) -> None:
        menu = await main_menu_view(session, user)
        if message.text and message.text.startswith("/"):
                await view.send_view(message, menu)
                return
        await message.answer(
                "Не понял сообщение.\n\nВот меню — тут всё нужное. Если нужна помощь человека, "
                "напиши в поддержку.",
                reply_markup=keyboards.support_kb(),
        )


@router.message()
async def fallback_any(message: Message) -> None:
        await message.answer(
                "Принимаю только текст и кнопки меню. Пришли вопрос текстом или открой /menu.",
                reply_markup=keyboards.support_kb(),
        )
