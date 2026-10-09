"""Экран «Мой профиль»: мини-визитка клиента.

Что здесь, а что нет. Цифры собирает :mod:`app.services.profile` — из подписки,
заказов, тарифов и настроек; этот модуль только раскладывает их по тексту
(:func:`app.bot.texts.profile_card`) и показывает кнопки. Так экран не может
разойтись с тем, что реально настроено: обещания приходят из ``settings``,
факты — из БД, а «не знаем» так и пишется.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.config import get_settings
from app.db.models import User
from app.panels.registry import registry
from app.services import profile as profile_service

router = Router(name="profile")
settings = get_settings()


def render_card(card: profile_service.ProfileCard) -> str:
    """Собрать текст экрана: факты — из карточки, обещания — из настроек."""
    return texts.profile_card(
        name=card.name,
        username=card.username,
        member_since=card.member_since,
        people_count=card.people_count,
        status=card.status,
        expires=card.expires_text,
        days_left=card.days_left,
        forever=card.forever,
        bar=card.bar,
        devices=card.devices,
        traffic_limit_gb=card.traffic_limit_gb,
        traffic_used_gb=card.traffic_used_gb,
        plan_title=card.plan_title,
        paid_rub=card.paid_rub,
        locations_line=card.locations_line,
        invited=card.invited,
        paid_friends=card.paid_friends,
        earned_days=card.earned_days,
        balance_days=card.balance_days,
        rewards_left=card.rewards_left,
        max_rewards=settings.referral_max_rewards_per_month,
        trial_days=settings.trial_days,
        trial_btn=keyboards.BTN_TRIAL,
        percent=settings.referral_discount_percent,
        referrer_days=settings.referral_bonus_days_referrer,
    )


@router.message(Command("profile"))
@router.callback_query(F.data == "profile:show")
@router.message(F.text == keyboards.BTN_PROFILE)
async def show_profile(event: Message | CallbackQuery, session: AsyncSession, user: User) -> None:
    """Показать профиль: команда ``/profile``, кнопка меню или callback.

    Карточка не меняет данные — только читает, поэтому её можно открывать
    сколько угодно раз. Панель спрашиваем ради расхода трафика и только когда
    лимит вообще есть (см. ``app.services.profile.build_card``).
    """
    card = await profile_service.build_card(session, user, panel=registry.primary())
    text = render_card(card)
    markup = keyboards.profile_kb()

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        await event.answer()
        return
    await event.answer(text, reply_markup=markup, disable_web_page_preview=True)
