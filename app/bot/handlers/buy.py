"""Покупка и продление: тарифы → способ оплаты → счёт → подтверждение."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.config import get_settings
from app.db.models import User
from app.panels.base import PanelError
from app.panels.registry import registry
from app.payments.base import PaymentError, PaymentStatus
from app.payments.payload import parse_order_id_from_payload
from app.payments.registry import payments
from app.services import events, notifications, orders, subscriptions

logger = logging.getLogger(__name__)
router = Router(name="buy")
settings = get_settings()

PROVIDER_TITLES = {
    "manual": texts.PROVIDER_MANUAL,
    "crypto": texts.PROVIDER_CRYPTO,
    "stars": texts.PROVIDER_STARS,
    "wata": texts.PROVIDER_WATA,
}


def _expires_text(dt) -> str:
    return dt.strftime("%d.%m.%Y %H:%M") if dt else "—"


async def send_plans(target: Message | CallbackQuery, session: AsyncSession) -> None:
    plans = await orders.list_plans(session)
    if not plans:
        text, markup = texts.WELCOME, keyboards.back_to_menu_kb()
    else:
        text = texts.PLANS_HEADER
        markup = keyboards.plans_kb(plans, show_stars=settings.stars_enabled)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data == "plans")
async def cb_plans(call: CallbackQuery, session: AsyncSession) -> None:
    await send_plans(call, session)


@router.message(F.text == keyboards.BTN_PLANS)
async def msg_plans(message: Message, session: AsyncSession) -> None:
    await send_plans(message, session)


@router.callback_query(F.data.startswith("plan:"))
async def cb_plan_card(call: CallbackQuery, session: AsyncSession) -> None:
    plan_id = int(call.data.split(":", 1)[1])
    plan = await orders.get_plan(session, plan_id)
    if plan is None or not plan.is_active:
        await call.answer("Тариф недоступен", show_alert=True)
        return

    available = payments.available()
    if not available:
        await call.answer("Приём оплаты временно недоступен", show_alert=True)
        return

    stars_line = ""
    if settings.stars_enabled and plan.price_stars:
        stars_line = texts.STARS_LINE.format(stars=plan.price_stars)

    text = texts.PLAN_CARD.format(
        title=plan.title,
        price=plan.price_rub,
        per_month=round(plan.price_rub / max(1, plan.days) * 30),
        days=plan.days,
        devices=plan.devices_limit,
        stars_line=stars_line,
    )
    markup = keyboards.providers_kb(plan.id, [(p.code, PROVIDER_TITLES.get(p.code, p.title)) for p in available])
    await call.message.edit_text(text, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    _, plan_id_raw, provider_code = call.data.split(":")
    plan = await orders.get_plan(session, int(plan_id_raw))
    provider = payments.get(provider_code)
    if plan is None or provider is None:
        await call.answer("Способ оплаты недоступен", show_alert=True)
        return

    order = await orders.create_order(session, user, plan, provider=provider.code)
    title = f"{texts.BRAND}: {plan.title}"

    try:
        invoice = await provider.create_invoice(
            order.id,
            order.amount_rub,
            title,
            # У Stars своя сетка цен — берём цену из тарифа;
            # ручному переводу нужна точная сумма с уникальными копейками,
            # по которым автоплатёж находит заказ.
            price_override=plan.price_stars or None,
            exact_kopecks=order.pay_amount_kopecks,
        )
    except PaymentError as exc:
        logger.warning("Ошибка создания счёта: %s", exc)
        await call.message.edit_text(texts.ERROR_GENERIC, reply_markup=keyboards.back_to_menu_kb())
        await call.answer("Не удалось создать счёт", show_alert=True)
        return

    if order.external_id == f"ord-{order.id}":
        order.external_id = invoice.external_id
        await session.flush()

    if provider.code == "manual":
        text = texts.ORDER_CREATED_MANUAL.format(
            order_id=order.id,
            amount=order.amount_rub,
            instructions=invoice.instructions,
            ttl=settings.order_ttl_minutes,
        )
        markup = keyboards.manual_order_kb(order.id)
    elif provider.code == "crypto":
        text = texts.ORDER_CREATED_CRYPTO.format(order_id=order.id, amount=order.amount_rub)
        markup = keyboards.crypto_order_kb(order.id, invoice.pay_url or "")
    elif provider.code == "wata":
        text = texts.ORDER_CREATED_WATA.format(order_id=order.id, amount=order.amount_rub)
        markup = keyboards.crypto_order_kb(order.id, invoice.pay_url or "")
    else:
        text = texts.ORDER_CREATED_STARS.format(order_id=order.id, amount=order.amount_rub)
        markup = keyboards.stars_order_kb(order.id, invoice.pay_url or "")

    await call.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data.startswith("order:cancel:"))
async def cb_cancel(call: CallbackQuery, session: AsyncSession) -> None:
    order = await orders.get_order(session, int(call.data.rsplit(":", 1)[1]))
    if order is None:
        await call.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    await orders.cancel_order(session, order, reason="canceled by user")
    await call.message.edit_text(
        texts.ORDER_CANCELED.format(order_id=order.id),
        reply_markup=keyboards.back_to_menu_kb(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("order:manual:"))
async def cb_manual_paid(call: CallbackQuery, session: AsyncSession, user: User, bot: Bot) -> None:
    order = await orders.get_order(session, int(call.data.rsplit(":", 1)[1]))
    if order is None or order.status != "pending":
        await call.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    plan = await orders.get_plan(session, order.plan_id) if order.plan_id else None
    await call.message.edit_text(
        texts.ORDER_WAITING_CONFIRM.format(order_id=order.id),
        reply_markup=keyboards.back_to_menu_kb(),
    )
    await call.answer("Передали на проверку ✅")
    await notifications.notify_admins(
        bot,
        texts.ADMIN_NEW_ORDER.format(
            order_id=order.id,
            user=user.display_name,
            tg_id=user.tg_id,
            plan=plan.title if plan else "—",
            amount=order.amount_rub,
            provider=PROVIDER_TITLES.get(order.provider, order.provider),
        ),
    )
    for admin_id in settings.admin_id_list:
        try:
            await bot.send_message(admin_id, "Подтвердить?", reply_markup=keyboards.admin_order_kb(order.id))
        except Exception:  # noqa: BLE001 - админ мог не начать чат с ботом
            logger.warning("Не смог отправить кнопки администратору %s", admin_id)


@router.callback_query(F.data.startswith("order:check:"))
async def cb_check_payment(call: CallbackQuery, session: AsyncSession, user: User, bot: Bot) -> None:
    order = await orders.get_order(session, int(call.data.rsplit(":", 1)[1]))
    if order is None:
        await call.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    if order.status == "paid":
        await call.answer("Заказ уже оплачен ✅")
        return

    provider = payments.get(order.provider)
    if provider is None:
        await call.answer("Способ оплаты недоступен", show_alert=True)
        return

    try:
        check = await provider.check_payment(order.external_id or "")
    except PaymentError as exc:
        logger.warning("Ошибка проверки платежа: %s", exc)
        await call.answer("Не удалось проверить платёж, попробуй позже", show_alert=True)
        return

    if check.status is not PaymentStatus.PAID:
        await call.answer(texts.PAYMENT_NOT_FOUND, show_alert=True)
        return

    await finalize_order(session, order, bot, user)
    await call.answer("Оплата получена ✅")


async def finalize_order(session: AsyncSession, order, bot: Bot, user: User) -> None:
    """Единая точка выдачи доступа после успешной оплаты."""
    panel = registry.primary()
    try:
        sub, already = await orders.mark_paid(session, order, panel)
    except PanelError as exc:
        logger.error("Панель не выдала доступ по заказу %s: %s", order.id, exc)
        await events.log_event(session, events.PANEL_ERROR, user_id=user.id, payload={"order_id": order.id})
        await notifications.notify_admins(bot, f"⚠️ Панель не выдала доступ по заказу #{order.id}: <code>{exc}</code>")
        return

    if already or sub is None:
        return

    await bot.send_message(
        user.tg_id,
        texts.ORDER_PAID.format(expires=_expires_text(sub.expires_at), days=sub.days_left),
    )
    await bot.send_message(
        user.tg_id,
        texts.SUBSCRIPTION_LINK_HINT.format(link=subscriptions.subscription_link(sub.subscription_token)),
    )
    plan = await orders.get_plan(session, order.plan_id) if order.plan_id else None
    await notifications.notify_admins(
        bot,
        f"💰 Оплата: заказ #{order.id}, {order.amount_rub} ₽, {plan.title if plan else '—'}, "
        f"пользователь {user.display_name} (<code>{user.tg_id}</code>)",
    )


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery, session: AsyncSession) -> None:
    """Подтверждение счёта ДО списания звёзд.

    Telegram ждёт ответ 10 секунд и отменяет платёж, если ответа нет, —
    поэтому этот хендлер обязателен для оплаты в Stars.
    """
    order_id = parse_order_id_from_payload(query.invoice_payload)
    order = await orders.get_order(session, order_id) if order_id else None

    if order is None:
        await query.answer(ok=False, error_message="Заказ не найден. Открой меню и оформи заказ заново.")
        return
    if order.status != "pending":
        await query.answer(ok=False, error_message="Этот заказ уже оплачен. Открой «Моя подписка».")
        return

    plan = await orders.get_plan(session, order.plan_id) if order.plan_id else None
    if query.currency != "XTR" or (plan and plan.price_stars and query.total_amount != plan.price_stars):
        logger.warning(
            "Stars: несовпадение счёта order=%s currency=%s amount=%s expected=%s",
            order.id,
            query.currency,
            query.total_amount,
            plan.price_stars if plan else None,
        )
        await query.answer(ok=False, error_message="Сумма счёта не совпадает с тарифом. Оформи заказ заново.")
        return

    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_stars_paid(message: Message, session: AsyncSession, user: User, bot: Bot) -> None:
    """Оплата Telegram Stars приходит апдейтом, а не вебхуком."""
    payment = message.successful_payment
    order_id = parse_order_id_from_payload(payment.invoice_payload)
    if not order_id:
        await message.answer("Оплата получена, но заказ не найден. Напиши в поддержку.")
        return
    order = await orders.get_order(session, order_id)
    if order is None:
        await message.answer(texts.ORDER_NOT_FOUND)
        return

    await finalize_order(session, order, bot, user)

    # Крупные покупки звёздами — под контроль: дешёвые звёзды у перекупов
    # бывают добыты мошенническим путём, и Telegram может списать их с баланса
    # уже после выдачи доступа.
    if payment.total_amount >= settings.stars_watch_threshold:
        await events.log_event(
            session,
            events.ORDER_PAID,
            user_id=user.id,
            payload={
                "order_id": order.id,
                "stars": payment.total_amount,
                "flag": "large_stars_payment",
                "charge_id": payment.telegram_payment_charge_id,
            },
        )
        await notifications.notify_admins(
            bot,
            "⭐️ <b>Крупная оплата звёздами — проверь на мошенничество</b>\n"
            f"Пользователь: {user.display_name} (<code>{user.tg_id}</code>), "
            f"аккаунт создан {user.created_at:%d.%m.%Y}\n"
            f"Заказ #{order.id}: {payment.total_amount} ⭐ ({order.amount_rub} ₽)\n"
            f"Charge ID: <code>{payment.telegram_payment_charge_id}</code>\n\n"
            "Если звёзды окажутся крадеными, Telegram спишет их с баланса — "
            "подписку придётся отключить вручную (/block).",
        )
