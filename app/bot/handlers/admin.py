"""Админские команды: статистика, подтверждение платежей, управление доступом."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.bot.filters import IsAdmin
from app.config import get_settings
from app.db.models import Order, Subscription, User
from app.panels.registry import registry
from app.services import notifications, orders, stats, subscriptions

logger = logging.getLogger(__name__)
router = Router(name="admin")
settings = get_settings()
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class Broadcast(StatesGroup):
    text = State()


def web_panel_url() -> str:
    """Публичный адрес админ-панели (пусто, если он локальный)."""
    base = settings.public_base_url.rstrip("/")
    if not base or "127.0.0.1" in base or "localhost" in base:
        return ""
    return f"{base}/admin"


@router.message(Command("admin"))
async def cmd_admin(message: Message, session: AsyncSession) -> None:
    snapshot = await stats.collect(session)
    url = web_panel_url()
    text = snapshot.as_text()
    if not settings.admin_panel_password:
        text += "\n\n🌐 Веб-панель выключена: задай <code>ADMIN_PANEL_PASSWORD</code> в .env"
    elif url:
        text += f"\n\n🌐 Веб-панель: {url}"
    else:
        text += (
            f"\n\n🌐 Веб-панель: <code>{settings.public_base_url.rstrip('/')}/admin</code>"
            "\n(адрес локальный — с телефона не откроется, пока не будет домена)"
        )
    await message.answer(text, reply_markup=keyboards.admin_panel_kb(pending_count=snapshot.pending_orders, panel_url=url or None))


@router.message(Command("stats"))
async def cmd_stats(message: Message, session: AsyncSession) -> None:
    await message.answer((await stats.collect(session)).as_text())


@router.callback_query(F.data == "admin:stats")
async def cb_stats(call: CallbackQuery, session: AsyncSession) -> None:
    await call.message.edit_text((await stats.collect(session)).as_text(), reply_markup=keyboards.back_to_menu_kb())
    await call.answer()


@router.callback_query(F.data == "admin:orders")
@router.message(Command("orders"))
async def show_pending_orders(event: Message | CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    pending = await orders.pending_orders(session)
    target = event.message if isinstance(event, CallbackQuery) else event
    if not pending:
        await target.answer("Ожидающих заявок нет ✅")
    else:
        for order in pending:
            user = await session.get(User, order.user_id)
            plan = await orders.get_plan(session, order.plan_id) if order.plan_id else None
            await target.answer(
                texts.ADMIN_NEW_ORDER.format(
                    order_id=order.id,
                    user=user.display_name if user else "—",
                    tg_id=user.tg_id if user else "—",
                    plan=plan.title if plan else "—",
                    amount=order.amount_rub,
                    provider=order.provider,
                ),
                reply_markup=keyboards.admin_order_kb(order.id),
            )
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.callback_query(F.data.startswith("admin:confirm:"))
async def cb_confirm_order(call: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    order_id = int(call.data.rsplit(":", 1)[1])
    order = await orders.get_order(session, order_id)
    if order is None:
        await call.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    if order.status != "pending":
        await call.answer(f"Заказ уже в статусе {order.status}", show_alert=True)
        return

    user = await session.get(User, order.user_id)
    panel = registry.primary()
    sub, already = await orders.mark_paid(session, order, panel, confirmed_by=call.from_user.id)
    await call.message.edit_text(texts.ADMIN_CONFIRMED.format(order_id=order.id))
    await call.answer("Подтверждено ✅")

    if user is not None and sub is not None and not already:
        expires = sub.expires_at.strftime("%d.%m.%Y %H:%M") if sub.expires_at else "—"
        await bot.send_message(
            user.tg_id,
            texts.ORDER_PAID.format(
                expires=expires,
                days=sub.days_left,
                link=subscriptions.subscription_link(sub.subscription_token),
            ),
        )
        await bot.send_message(
            user.tg_id,
            texts.SUBSCRIPTION_LINK_HINT.format(link=subscriptions.subscription_link(sub.subscription_token)),
        )


@router.callback_query(F.data.startswith("admin:reject:"))
async def cb_reject_order(call: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    order_id = int(call.data.rsplit(":", 1)[1])
    order = await orders.get_order(session, order_id)
    if order is None:
        await call.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    await orders.cancel_order(session, order, reason=f"rejected by {call.from_user.id}")
    await call.message.edit_text(texts.ADMIN_REJECTED.format(order_id=order.id))
    await call.answer("Отклонено")
    user = await session.get(User, order.user_id)
    if user is not None:
        try:
            await bot.send_message(
                user.tg_id,
                f"Заказ #{order.id} отклонён. Если оплата прошла — напиши в поддержку, разберёмся.",
            )
        except Exception:  # noqa: BLE001
            logger.warning("Не смог уведомить пользователя %s", user.tg_id)


@router.message(Command("user"))
async def cmd_user(message: Message, session: AsyncSession) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Использование: <code>/user 123456789</code>")
        return
    user = await subscriptions.get_user_by_tg(session, int(parts[1]))
    if user is None:
        await message.answer("Пользователь не найден")
        return
    sub = await subscriptions.get_subscription(session, user.id)
    orders_list = (
        await session.scalars(select(Order).where(Order.user_id == user.id).order_by(Order.created_at.desc()).limit(5))
    ).all()
    lines = [
        f"👤 <b>{user.display_name}</b> (<code>{user.tg_id}</code>)",
        f"Создан: {user.created_at:%d.%m.%Y}",
        f"Код: <code>{user.referral_code}</code>",
        f"Подписка: {sub.status if sub else '—'}",
    ]
    if sub:
        lines.append(f"До: {sub.expires_at:%d.%m.%Y %H:%M} ({sub.days_left} дн.)")
        lines.append(f"Ссылка: <code>{subscriptions.subscription_link(sub.subscription_token)}</code>")
    if orders_list:
        lines.append("\nПоследние заказы:")
        lines.extend(f"#{o.id} {o.amount_rub} ₽ — {o.status} ({o.provider})" for o in orders_list)
    await message.answer("\n".join(lines))


@router.message(Command("grant"))
async def cmd_grant(message: Message, session: AsyncSession, bot: Bot) -> None:
    parts = (message.text or "").split()
    if len(parts) < 3 or not parts[1].isdigit() or not parts[2].lstrip("-").isdigit():
        await message.answer("Использование: <code>/grant 123456789 7</code> (дней, можно отрицательное)")
        return
    tg_id, days = int(parts[1]), int(parts[2])
    user = await subscriptions.get_user_by_tg(session, tg_id)
    if user is None:
        await message.answer("Пользователь не найден")
        return
    panel = registry.primary()
    if days >= 0:
        sub = await subscriptions.extend_days(session, user, days, panel, reason=f"admin:{message.from_user.id}")
    else:
        sub = await subscriptions.get_subscription(session, user.id)
        if sub is None:
            await message.answer("У пользователя нет подписки")
            return
        sub.expires_at = sub.expires_at + timedelta(days=days)
    if sub is None:
        await message.answer("Не удалось изменить подписку")
        return
    await message.answer(f"✅ {user.display_name}: до {sub.expires_at:%d.%m.%Y %H:%M}")
    try:
        await bot.send_message(user.tg_id, f"Тебе начислено {days} дн. доступа. Спасибо, что с нами 💙")
    except Exception:  # noqa: BLE001
        pass


@router.message(Command("block"))
@router.message(Command("unblock"))
async def cmd_block(message: Message, session: AsyncSession) -> None:
    block = (message.text or "").startswith("/block")
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/block 123456789</code>")
        return
    user = await subscriptions.get_user_by_tg(session, int(parts[1]))
    if user is None:
        await message.answer("Пользователь не найден")
        return
    user.is_blocked = block
    sub = await subscriptions.get_subscription(session, user.id)
    if sub is not None:
        await subscriptions.set_enabled(sub, registry.primary(), enabled=not block)
    await message.answer(f"{'⛔️ Заблокирован' if block else '✅ Разблокирован'}: {user.display_name}")


@router.message(Command("nodes"))
@router.callback_query(F.data == "admin:nodes")
async def cmd_nodes(event: Message | CallbackQuery, session: AsyncSession) -> None:
    lines = ["🖥 <b>Панели и ноды</b>\n"]
    for panel in await registry.all_panels(session):
        try:
            inbounds = await panel.list_inbounds()
            ok = await panel.health()
            status = "✅" if ok else "⚠️"
            lines.append(f"{status} <b>{panel.name}</b>: инбаундов {len(inbounds)}")
            for ib in inbounds:
                lines.append(f"   • {ib.remark} ({ib.protocol}, {ib.port})")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"❌ <b>{panel.name}</b>: ошибка — <code>{exc}</code>")
    text = "\n".join(lines)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=keyboards.back_to_menu_kb())
        await event.answer()
    else:
        await event.answer(text)


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    await state.set_state(Broadcast.text)
    await message.answer("Пришли текст рассылки (HTML разрешён). /cancel — отмена.")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено")


@router.message(Broadcast.text)
async def do_broadcast(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    await state.clear()
    recipients = (await session.scalars(select(User).where(User.is_blocked.is_(False)))).all()
    sent = failed = 0
    await message.answer(texts.ADMIN_BROADCAST_START.format(count=len(recipients)))
    for user in recipients:
        try:
            await bot.send_message(user.tg_id, message.html_text, disable_web_page_preview=True)
            sent += 1
        except Exception:  # noqa: BLE001
            failed += 1
    await message.answer(texts.ADMIN_BROADCAST_DONE.format(sent=sent, failed=failed))


@router.message(Command("help_admin"))
async def cmd_help_admin(message: Message) -> None:
    await message.answer(
        "<b>Админ-команды</b>\n"
        "/admin — панель\n"
        "/stats — статистика\n"
        "/orders — заявки на оплату\n"
        "/user &lt;tg_id&gt; — карточка пользователя\n"
        "/grant &lt;tg_id&gt; &lt;дней&gt; — начислить/списать дни\n"
        "/block | /unblock &lt;tg_id&gt;\n"
        "/nodes — состояние панелей\n"
        "/broadcast — рассылка"
    )


@router.message()
async def fallback(message: Message, session: AsyncSession, user: User) -> None:
    """Неизвестная команда — показываем меню, чтобы человек не терялся."""
    from app.bot.handlers.start import main_menu_view

    if message.text and message.text.startswith("/"):
        text, markup = await main_menu_view(session, user)
        await message.answer(text, reply_markup=markup)
        return
    await message.answer(
        "Не понял сообщение. Нажми /menu — там всё нужное, или напиши в поддержку.",
    )
