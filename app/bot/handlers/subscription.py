"""Экран «Моя подписка»: статус, ссылка, инструкции, локации."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.config import get_settings
from app.db.session import SessionMaker
from app.db.models import Node, User
from app.panels.base import PanelError
from app.panels.registry import registry
from app.services import subscriptions
from app.services.probe import (
    PROBE_NOT_CONFIGURED,
    PROBE_OK,
    PROBE_PORT_FAILED,
    PROBE_UNKNOWN,
    probe_verdict,
)

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


def _is_forever(dt) -> bool:
        """Подписка «бессрочная»?

        Бессрочные подписки храним с далёкой датой (иначе фоновые задачи их
        погасят), а в карточке показываем «бессрочно» — иначе клиент видит
        «Осталось: 26000 дн.».
        """
        if dt is None:
                return False
        moment = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return moment - datetime.now(timezone.utc) > timedelta(days=365 * 10)


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
                forever = _is_forever(sub.expires_at)
                text = texts.MY_SUB_ACTIVE.format(
                        status=STATUS_LABELS.get(sub.status, sub.status),
                        expires="бессрочно" if forever else _expires_text(sub.expires_at),
                        days="∞" if forever else sub.days_left,
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


@router.callback_query(F.data == "sub:reserve")
@router.message(F.text == keyboards.BTN_RESERVE)
async def show_reserve_help(event: Message | CallbackQuery) -> None:
        """Инструкция «если локации не открываются».

        Это не реклама, а сервисное сообщение клиенту: рассказываем, что делать,
        и честно говорим про скорость и про то, что профиль может пропадать.
        """
        if isinstance(event, CallbackQuery):
                await event.message.answer(texts.RESERVE_HELP, reply_markup=keyboards.support_kb())
                await event.answer()
        else:
                await event.answer(texts.RESERVE_HELP, reply_markup=keyboards.support_kb())


@router.callback_query(F.data == "sub:locations")
async def cb_locations(call: CallbackQuery) -> None:
        """Локации честно: страна, канал и состояние пробы.

        Раньше экран печатал служебные remark инбаундов («NL-Reality-firefox-u42»)
        и порты — клиенту это ничего не говорит, а внутренние имена и адреса
        наружу отдавать незачем. Теперь показываем то, что у нас действительно
        есть: название локации, канал (обычная / резерв / CDN) и вердикт
        последней пробы «глазами клиента» (``app.services.probe.probe_verdict``,
        её пишет watchdog). Пинг подписан как замер с сервера: задержка от
        сервиса до ноды — не пинг телефона.

        Панели не опрашиваем вовсе: экран не должен зависеть от того, отвечает
        ли чужая панель, и уж точно не должен падать из-за ноды без адреса
        панели. Источник — таблица нод: там же лежит и результат пробы
        (``Node.last_probe_*``, пишет её watchdog). Нет замера — так и говорим,
        а не рисуем «работает».
        """
        state_labels = {
                PROBE_OK: texts.LOCATION_STATE_OK,
                PROBE_PORT_FAILED: texts.LOCATION_STATE_PORT_FAILED,
                PROBE_NOT_CONFIGURED: texts.LOCATION_STATE_NOT_CONFIGURED,
                PROBE_UNKNOWN: texts.LOCATION_STATE_UNKNOWN,
        }
        channel_marks = {
                "reserve": texts.LOCATION_CHANNEL_RESERVE,
                "cdn": texts.LOCATION_CHANNEL_CDN,
        }

        items: list[str] = []
        async with SessionMaker() as session:
                nodes = (
                        await session.scalars(
                                select(Node)
                                .where(Node.is_active.is_(True))
                                .order_by(Node.priority, Node.id)
                        )
                ).all()
                for node in nodes:
                        title = str(node.title or "").strip()
                        if not title:
                                # Название страны не задано — показывать нечего:
                                # служебный код ноды клиенту ничего не говорит.
                                continue
                        channel = channel_marks.get(str(node.channel or "main").strip().lower(), "")
                        verdict = probe_verdict(node)
                        state = state_labels.get(verdict, texts.LOCATION_STATE_UNAVAILABLE)
                        ms = int(node.last_probe_ms or 0)
                        if verdict == PROBE_OK and ms:
                                state = state.format(ms=ms)
                        items.append(
                                texts.LOCATION_ITEM.format(
                                        title=title, channel=channel, state=state
                                )
                        )

        if items:
                text = texts.LOCATIONS_HEADER.format(items="\n".join(items)) + texts.LOCATIONS_FOOTER
        else:
                text = texts.LOCATIONS_HEADER.format(items=texts.LOCATIONS_EMPTY)
        await call.message.answer(
                text, reply_markup=keyboards.back_to_menu_kb(), disable_web_page_preview=True
        )
        await call.answer()
