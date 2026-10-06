"""Экран «Моя подписка»: статус, ссылка, инструкции, локации."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.config import get_settings
from app.db.models import User
from app.panels.base import PanelError
from app.panels.registry import registry
from app.services import subscriptions

router = Router(name="subscription")
settings = get_settings()

STATUS_LABELS = {
    "trial": texts.STATUS_TRIAL,
    "active": texts.STATUS_ACTIVE,
    "expired": texts.STATUS_EXPIRED,
    "blocked": texts.STATUS_BLOCKED,
}


def _expires_text(dt) -> str:
    return dt.strftime("%d.%m.%Y %H:%M") if dt else "—"


@router.callback_query(F.data == "sub:show")
@router.message(F.text == keyboards.BTN_MY_SUB)
async def show_subscription(event: Message | CallbackQuery, session: AsyncSession, user: User) -> None:
    sub = await subscriptions.get_subscription(session, user.id)
    is_call = isinstance(event, CallbackQuery)

    if sub is None:
        text = texts.MY_SUB_NONE.format(trial_days=settings.trial_days)
        markup = keyboards.main_menu(has_subscription=False, is_active=False)
    elif not sub.is_active:
        text = texts.MY_SUB_EXPIRED.format(ago=f"({_expires_text(sub.expires_at)})")
        markup = keyboards.subscription_kb(has_panel_user=bool(sub.panel_user_uuid))
    else:
        text = texts.MY_SUB_ACTIVE.format(
            status=STATUS_LABELS.get(sub.status, sub.status),
            expires=_expires_text(sub.expires_at),
            days=sub.days_left,
            devices=sub.devices_limit,
        )
        markup = keyboards.subscription_kb(has_panel_user=bool(sub.panel_user_uuid))

    if is_call:
        await event.message.edit_text(text, reply_markup=markup)
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup)


@router.callback_query(F.data == "sub:link")
async def cb_link(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    sub = await subscriptions.get_subscription(session, user.id)
    if sub is None:
        await call.answer("Сначала получи доступ", show_alert=True)
        return
    link = subscriptions.subscription_link(sub.subscription_token)
    await call.message.answer(
        texts.SUBSCRIPTION_LINK_HINT.format(link=link),
        reply_markup=keyboards.connect_kb(link),
        disable_web_page_preview=True,
    )
    await call.answer()


@router.callback_query(F.data == "sub:copy")
async def cb_copy_link(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    """Ссылка отдельным сообщением — тапом по <code> копируется целиком."""
    sub = await subscriptions.get_subscription(session, user.id)
    if sub is None:
        await call.answer("Сначала получи доступ", show_alert=True)
        return
    link = subscriptions.subscription_link(sub.subscription_token)
    await call.message.answer(
        texts.SUBSCRIPTION_COPY.format(link=link),
        reply_markup=keyboards.connect_kb(link),
        disable_web_page_preview=True,
    )
    await call.answer("Скопировано — вставь в приложении")


@router.callback_query(F.data == "sub:refresh")
async def cb_refresh(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    sub = await subscriptions.get_subscription(session, user.id)
    if sub is None or not sub.panel_user_uuid:
        await call.answer("Подписки нет", show_alert=True)
        return
    panel = registry.primary()
    try:
        panel_user = await panel.get_user(sub.panel_user_uuid)
    except PanelError:
        panel_user = None

    if panel_user is None:
        await call.answer("Панель недоступна, попробуй позже", show_alert=True)
        return

    if panel_user.expires_at:
        sub.expires_at = panel_user.expires_at
    sub.devices_limit = panel_user.devices_limit or sub.devices_limit
    await session.flush()
    await call.answer("Данные обновлены ✅")


@router.callback_query(F.data == "sub:howto")
@router.message(F.text == keyboards.BTN_HOWTO)
async def show_howto(event: Message | CallbackQuery) -> None:
    if isinstance(event, CallbackQuery):
        await event.message.answer(texts.HOWTO, reply_markup=keyboards.support_kb(), disable_web_page_preview=True)
        await event.answer()
    else:
        await event.answer(texts.HOWTO, reply_markup=keyboards.support_kb(), disable_web_page_preview=True)


@router.callback_query(F.data == "sub:locations")
async def cb_locations(call: CallbackQuery) -> None:
    panel = registry.primary()
    try:
        inbounds = await panel.list_inbounds()
    except PanelError:
        inbounds = []
    if not inbounds:
        await call.answer("Локации появятся после настройки нод", show_alert=True)
        return
    items = "\n".join(f"• <b>{ib.remark}</b> — {ib.protocol}, порт {ib.port}" for ib in inbounds)
    await call.message.answer(texts.LOCATIONS.format(items=items), reply_markup=keyboards.back_to_menu_kb())
    await call.answer()
