"""Пробный доступ."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.config import get_settings
from app.db.models import User
from app.panels.base import PanelError
from app.panels.registry import registry
from app.services import subscriptions

router = Router(name="trial")
settings = get_settings()


@router.callback_query(F.data == "trial:start")
async def cb_start_trial(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    if not settings.sales_enabled:
        # Нода не готова — тестовый доступ тоже не выдаём, иначе клиент
        # получит нерабочий конфиг и уйдёт.
        await call.answer("Сервис готовится к запуску", show_alert=True)
        await call.message.edit_text(
            texts.SALES_CLOSED.format(note=settings.sales_closed_note),
            reply_markup=keyboards.back_to_menu_kb(),
            disable_web_page_preview=True,
        )
        return

    panel = registry.primary()
    try:
        sub, granted = await subscriptions.start_trial(session, user, panel)
    except PanelError:
        await call.message.edit_text(texts.ERROR_GENERIC, reply_markup=keyboards.back_to_menu_kb())
        await call.answer("Сервис временно недоступен", show_alert=False)
        return

    if not granted:
        await call.message.edit_text(
            texts.TRIAL_ALREADY_USED,
            reply_markup=keyboards.back_to_menu_kb(),
        )
        await call.answer()
        return

    link = subscriptions.subscription_link(sub.subscription_token)
    await call.message.edit_text(
        texts.TRIAL_STARTED.format(days=settings.trial_days, gb=settings.trial_gb),
        reply_markup=keyboards.subscription_kb(has_panel_user=bool(sub.panel_user_uuid)),
    )
    await call.message.answer(
        texts.SUBSCRIPTION_LINK_HINT.format(link=link),
        reply_markup=keyboards.connect_kb(link),
        disable_web_page_preview=True,
    )
    await call.answer("Пробный доступ выдан 🎉")
