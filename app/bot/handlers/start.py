"""Хендлеры: /start, главное меню, навигация."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import gate, keyboards, texts
from app.config import get_settings
from app.db.models import User
from app.panels.registry import registry
from app.services import channel_gate, orders, promo, referral, subscriptions

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
    # Скидку показываем там, где человек точно её увидит.
    promo_row = await promo.available(session, user)
    if promo_row is not None:
        text += texts.MENU_DISCOUNT_HINT.format(percent=promo_row.percent)
    # Цену самого дешёвого тарифа видно прямо в меню: клиент понимает
    # порядок цен, не открывая раздел «Тарифы».
    plans = await orders.list_plans(session)
    min_price = min((plan.price_rub for plan in plans), default=0)
    return text, keyboards.main_menu(
        has_subscription=sub is not None,
        is_active=bool(sub and sub.is_active),
        min_price=min_price,
    )


async def send_referral_greeting(
    message: Message, session: AsyncSession, user: User, referrer: User
) -> None:
    """Поздороваться с приглашённым и сразу показать его скидку."""
    plans = await orders.list_plans(session)
    percent = settings.referral_discount_percent
    examples = "\n".join(
        texts.REFERRAL_GREETING_EXAMPLE.format(
            title=plan.title,
            base=plan.price_rub,
            price=plan.price_rub
            - promo.calc_discount_rub(plan.price_rub, percent, settings.referral_discount_max_rub),
        )
        for plan in plans[:3]
    )
    await message.answer(
        texts.REFERRAL_GREETING.format(
            referrer=referrer.display_name,
            percent=percent,
            examples=examples,
            invited_days=settings.referral_bonus_days_invited,
            code=promo.code_for_referral(referrer.referral_code),
        ),
        reply_markup=keyboards.plans_button_kb(),
        disable_web_page_preview=True,
    )


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, user: User, bot: Bot) -> None:
    payload = ""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        payload = parts[1].strip()

    referrer = None
    if payload.startswith("ref_"):
        referrer = await referral.attach_referrer(session, user, payload[4:])
        if referrer is not None and referrer.id == user.id:
            referrer = None

    # Приветствие со скидкой показываем ДО гейта: ``attach_referrer`` отдаёт
    # пригласившего только в момент привязки, то есть ровно на этом /start. Если
    # отложить его до проверки подписки, друг не увидит обещанный подарок
    # никогда: повторный /start вернёт None, потому что referred_by уже стоит.
    if referrer is not None:
        await send_referral_greeting(message, session, user, referrer)

    # Обязательная подписка на канал: пока её нет (и нет активной подписки на
    # сервис), дальше экрана подписки человек не пройдёт. Пригласившего при
    # этом уже записали — бонус не теряется.
    if not (await channel_gate.verdict(session, bot, user)).allowed:
        await gate.show(message)
        return

    text, markup = await main_menu_view(session, user)
    await message.answer(text, reply_markup=markup)
    await message.answer(texts.QUICK_MENU_HINT, reply_markup=keyboards.reply_menu())


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
