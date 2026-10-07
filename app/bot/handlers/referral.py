"""Реферальная программа, промокоды и помощь.

Логика наград живёт в :mod:`app.services.referral`, проверка кодов —
в :mod:`app.services.promo`. Здесь только экраны и диалог ввода кода.
"""

from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.config import get_settings
from app.db.models import User
from app.services import orders, promo, referral

logger = logging.getLogger(__name__)
router = Router(name="referral")
settings = get_settings()

#: Что считаем попыткой ввести промокод в свободном тексте: латиница, цифры,
#: дефис. Русские слова сюда не попадают и уходят в обычный фолбэк.
CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_]{2,31}$")


class PromoForm(StatesGroup):
    code = State()


def referral_link(bot_username: str, user: User) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user.referral_code}"


@router.callback_query(F.data == "ref:show")
@router.message(F.text == keyboards.BTN_REFERRAL)
async def show_referral(event: Message | CallbackQuery, session: AsyncSession, user: User, bot: Bot) -> None:
    me = await bot.get_me()
    link = referral_link(me.username, user)
    code = await promo.ensure_referral_code(session, user)
    stats = await referral.overview(session, user)
    balance_line = (
        texts.REFERRAL_BALANCE_LINE.format(balance=stats["balance"]) if stats["balance"] else ""
    )
    text = texts.REFERRAL.format(
        percent=settings.referral_discount_percent,
        referrer_days=settings.referral_bonus_days_referrer,
        invited_days=settings.referral_bonus_days_invited,
        max_rewards=settings.referral_max_rewards_per_month,
        link=link,
        code=code.code,
        balance_line=balance_line,
        **stats,
    )
    markup = keyboards.referral_kb(link)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup, disable_web_page_preview=True)


@router.callback_query(F.data == "ref:code")
async def show_promo_code(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    code = await promo.ensure_referral_code(session, user)
    await call.message.edit_text(
        texts.PROMO_MY.format(
            code=code.code,
            percent=code.percent,
            referrer_days=settings.referral_bonus_days_referrer,
        ),
        reply_markup=keyboards.back_to_menu_kb(),
        disable_web_page_preview=True,
    )
    await call.answer()


@router.callback_query(F.data == "ref:friends")
async def show_friends(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    stats = await referral.overview(session, user)
    friends = await referral.list_invited(session, user)
    if not friends:
        text = texts.REFERRAL_FRIENDS_EMPTY.format(
            percent=settings.referral_discount_percent,
            referrer_days=settings.referral_bonus_days_referrer,
        )
    else:
        items = "\n".join(
            texts.REFERRAL_FRIEND_ITEM.format(
                name=friend.user.display_name,
                status=(
                    texts.REFERRAL_STATUS_PAID.format(days=friend.bonus_days)
                    if friend.paid
                    else texts.REFERRAL_STATUS_WAITING
                ),
            )
            for friend in friends
        )
        text = texts.REFERRAL_FRIENDS.format(items=items, **stats)
    await call.message.edit_text(text, reply_markup=keyboards.back_to_menu_kb(), disable_web_page_preview=True)
    await call.answer()


# ------------------------------------------------------------------ ввод кода
@router.callback_query(F.data == "promo:enter")
async def ask_promo(call: CallbackQuery, state: FSMContext, session: AsyncSession, user: User) -> None:
    example = promo.code_for_referral(user.referral_code)
    await state.set_state(PromoForm.code)
    await call.message.edit_text(
        texts.PROMO_ENTER.format(example=example),
        reply_markup=keyboards.back_to_menu_kb(),
        disable_web_page_preview=True,
    )
    await call.answer()


@router.message(PromoForm.code, F.text)
async def apply_promo_text(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    """Пользователь ввёл код после нажатия кнопки «У меня есть промокод»."""
    await state.clear()
    await _apply_code(message, session, user, message.text or "")


@router.message(F.text.regexp(CODE_PATTERN.pattern))
async def apply_promo_free(message: Message, session: AsyncSession, user: User) -> None:
    """Человек просто прислал код сообщением — самый частый сценарий.

    Обычный текст («Happ», «привет») не перехватываем: если код не похож на
    наш (``KOMETA-...``) и такого кода нет в базе, отдаём сообщение фолбэку.
    """
    raw = (message.text or "").strip()
    if not raw.upper().startswith(promo.CODE_PREFIX) and await promo.get_by_code(session, raw) is None:
        raise SkipHandler()
    await _apply_code(message, session, user, raw)


async def _apply_code(message: Message, session: AsyncSession, user: User, raw: str) -> None:
    code, reason = await promo.check_code(session, user, raw)
    if code is None:
        text = texts.PROMO_FAIL.get(reason or "", texts.PROMO_FAIL["not_found"])
        await message.answer(text, reply_markup=keyboards.back_to_menu_kb())
        return

    user.promo_code = code.code
    await session.flush()
    plans = await orders.list_plans(session)
    examples = "\n".join(
        texts.PROMO_EXAMPLE.format(
            title=plan.title,
            base=plan.price_rub,
            price=plan.price_rub - promo.calc_discount_rub(plan.price_rub, code.percent, code.max_discount_rub),
        )
        for plan in plans[:3]
    )
    await message.answer(
        texts.PROMO_OK.format(code=code.code, percent=code.percent, examples=examples),
        reply_markup=keyboards.back_to_menu_kb(),
        disable_web_page_preview=True,
    )
    # Сразу показываем тарифы: цены в кнопках уже со скидкой.
    from app.bot.handlers.buy import send_plans

    await send_plans(message, session, user)


# ------------------------------------------------------------------ помощь
@router.callback_query(F.data == "help")
@router.message(F.text == keyboards.BTN_HELP)
async def show_help(event: Message | CallbackQuery) -> None:
    """Помощь + ссылка на публичную страницу состояния сервиса."""
    text = texts.HELP
    if settings.public_base_url:
        text += texts.SERVICE_STATUS_HINT.format(status_url=f"{settings.public_base_url.rstrip('/')}/status")
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=keyboards.support_kb(), disable_web_page_preview=True)
        await event.answer()
    else:
        await event.answer(text, reply_markup=keyboards.support_kb(), disable_web_page_preview=True)
