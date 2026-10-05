"""Хендлеры: /start, главное меню, навигация."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.config import get_settings
from app.db.models import User
from app.panels.registry import registry
from app.services import referral, subscriptions

router = Router(name="start")
settings = get_settings()


async def main_menu_view(session: AsyncSession, user: User) -> tuple[str, object]:
    """Текст и клавиатура главного меню в зависимости от состояния подписки."""
    sub = await subscriptions.get_subscription(session, user.id)
    if sub is None:
        text = texts.WELCOME.format(brand=texts.BRAND, trial_days=settings.trial_days) + "\n\n" + texts.MENU_NO_SUB
    elif sub.is_active:
        text = texts.MENU_ACTIVE
    else:
        text = texts.MENU_EXPIRED
    return text, keyboards.main_menu(has_subscription=sub is not None, is_active=bool(sub and sub.is_active))


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, user: User) -> None:
    payload = ""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        payload = parts[1].strip()

    if payload.startswith("ref_"):
        referrer = await referral.attach_referrer(session, user, payload[4:])
        if referrer is not None:
            await message.answer(
                f"👋 Тебя пригласил {referrer.display_name}. "
                f"После первой оплаты ты получишь +{settings.referral_bonus_days_invited} дня к подписке."
            )

    text, markup = await main_menu_view(session, user)
    await message.answer(text, reply_markup=markup)
    await message.answer("Быстрое меню 👇", reply_markup=keyboards.reply_menu())


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    text, markup = await main_menu_view(session, user)
    await call.message.edit_text(text, reply_markup=markup)
    await call.answer()


@router.message(Command("menu"))
async def cmd_menu(message: Message, session: AsyncSession, user: User) -> None:
    text, markup = await main_menu_view(session, user)
    await message.answer(text, reply_markup=markup)


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Command("support"))
async def cmd_support(message: Message) -> None:
    await message.answer(texts.HELP, reply_markup=keyboards.support_kb())
