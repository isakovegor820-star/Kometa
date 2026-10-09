"""Пробный доступ."""

from __future__ import annotations

import logging

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
logger = logging.getLogger(__name__)


@router.callback_query(F.data == "trial:start")
async def cb_start_trial(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    if not settings.trial_enabled:
        # Закрыт именно пробный доступ (TRIAL_ENABLED). Продажи тут ни при чём:
        # их закрывает отдельный флаг SALES_ENABLED, и при нём пробный доступ
        # как раз работает — так пускаем людей на 3 дня, пока нет оплаты.
        await call.answer("Пробный доступ скоро откроется", show_alert=True)
        await call.message.edit_text(
            texts.TRIAL_CLOSED.format(note=settings.trial_closed_note),
            reply_markup=keyboards.back_to_menu_kb(),
            disable_web_page_preview=True,
        )
        return

    panel = await subscriptions.all_user_panels(session)
    # Заработанные на приглашениях дни показываем, но к триалу не добавляем:
    # они копятся и прибавятся к первой оплате (иначе рефералка обесценивается).
    bonus_days = int(user.bonus_days_balance or 0)
    try:
        sub, granted = await subscriptions.start_trial(session, user, panel)
    except PanelError as exc:
        # Пишем в лог: без этого разбор «клиент не получил доступ» упирается
        # в «Сервис временно недоступен» без единой строчки причины.
        logger.error("Пробный доступ не выдан пользователю %s: %s", user.tg_id, exc)
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
    traffic = "безлимитный трафик" if not settings.trial_gb else f"{settings.trial_gb} ГБ трафика"
    # Дни, реально попавшие в триал: настройка может разрешать добавлять бонусы.
    granted_bonus = (
        bonus_days if settings.trial_applies_bonus_days and bonus_days else 0
    )
    text = texts.TRIAL_STARTED.format(days=settings.trial_days + granted_bonus, traffic=traffic)
    if bonus_days and not granted_bonus:
        text += texts.TRIAL_BONUS_LINE.format(days=bonus_days)
    await call.message.edit_text(
        text,
        reply_markup=keyboards.subscription_kb(has_panel_user=bool(sub.panel_user_uuid)),
    )
    await call.message.answer(
        texts.SUBSCRIPTION_LINK_HINT.format(link=link),
        reply_markup=keyboards.connect_kb(link),
        disable_web_page_preview=True,
    )
    await call.answer("Пробный доступ выдан ")
