"""Реферальная программа и помощь."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.config import get_settings
from app.db.models import User
from app.services import referral

logger = logging.getLogger(__name__)
router = Router(name="referral")
settings = get_settings()


@router.callback_query(F.data == "ref:show")
@router.message(F.text == keyboards.BTN_REFERRAL)
async def show_referral(event: Message | CallbackQuery, session: AsyncSession, user: User, bot: Bot) -> None:
    invited, paid = await referral.referral_stats(session, user)
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{user.referral_code}"
    text = texts.REFERRAL.format(
        link=link,
        invited_days=settings.referral_bonus_days_invited,
        referrer_days=settings.referral_bonus_days_referrer,
        invited=invited,
        paid=paid,
    )
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=keyboards.back_to_menu_kb(), disable_web_page_preview=True)
        await event.answer()
    else:
        await event.answer(text, reply_markup=keyboards.back_to_menu_kb(), disable_web_page_preview=True)


@router.callback_query(F.data == "help")
@router.message(F.text == keyboards.BTN_HELP)
async def show_help(event: Message | CallbackQuery) -> None:
    """Помощь + подсказка, как проверить сервис при ограничениях интернета."""
    text = texts.HELP
    if settings.public_base_url:
        text += texts.SERVICE_STATUS_HINT.format(status_url=f"{settings.public_base_url.rstrip('/')}/status")
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=keyboards.support_kb(), disable_web_page_preview=True)
        await event.answer()
    else:
        await event.answer(text, reply_markup=keyboards.support_kb(), disable_web_page_preview=True)
