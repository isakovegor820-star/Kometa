"""Покупка и продление: тарифы → способ оплаты → счёт → подтверждение."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts, view
from app.config import get_settings
from app.db.models import Order, User
from app.panels.base import PanelError
from app.panels.registry import registry
from app.payments.base import PaymentError, PaymentStatus
from app.payments.payload import parse_order_id_from_payload
from app.payments.registry import payments, platega_provider
from app.services import events, notifications, notify_bot, orders, promo, subscriptions

logger = logging.getLogger(__name__)
router = Router(name="buy")
settings = get_settings()

PROVIDER_TITLES = {
        "manual": texts.PROVIDER_MANUAL,
        "crypto": texts.PROVIDER_CRYPTO,
        "stars": texts.PROVIDER_STARS,
        "platega_sbp": "💳 СБП / QR-код",
        "platega_card": "💳 Карта МИР",
        "platega_intl": "💳 Зарубежная карта",
        "platega_crypto": "💳 Криптовалюта",
        "platega_sberpay": "💳 SberPay",
        "platega_erip": "💳 ЕРИП",
}


def _expires_text(dt) -> str:
        return dt.strftime("%d.%m.%Y %H:%M") if dt else "—"


async def send_plans(target: Message | CallbackQuery, session: AsyncSession, user: User | None = None) -> None:
        """Экран тарифов.

        Тарифы показываем ВСЕГДА — и пока оплата не подключена: банк-партнёр и клиент
        должны видеть, сколько и за что платят. Отличается только шапка: при закрытых
        продажах честно пишем, что оплата по СБП включится в ближайшие дни.
        """
        plans = await orders.list_plans(session)
        if not plans:
                text, markup = texts.WELCOME, keyboards.back_to_menu_kb()
        elif not settings.sales_enabled:
                text = texts.PLANS_HEADER_SOON.format(
                        devices=plans[0].devices_limit,
                        locations=settings.locations_note,
                )
                markup = keyboards.plans_kb(plans)
        else:
                promo_row = await promo.available(session, user) if user is not None else None
                percent = promo_row.percent if promo_row else 0
                if percent:
                        text = texts.PLANS_HEADER_DISCOUNT.format(percent=percent)
                else:
                        text = texts.PLANS_HEADER + texts.REFERRAL_TEASER.format(
                                percent=settings.referral_discount_percent,
                                referrer_days=settings.referral_bonus_days_referrer,
                        )
                markup = keyboards.plans_kb(
                        plans,
                        show_stars=settings.stars_enabled,
                        discount_percent=percent,
                        max_discount_rub=promo_row.max_discount_rub if promo_row else 0,
                        show_promo_button=percent == 0,
                )
        if isinstance(target, CallbackQuery):
                await view.edit_screen(target.message, text, reply_markup=markup, disable_web_page_preview=True)
                await target.answer()
        else:
                await target.answer(text, reply_markup=markup, disable_web_page_preview=True)


async def show_payment_soon(target: Message | CallbackQuery, plan) -> None:  # noqa: ANN001 - Plan
        """Заглушка «оплата по СБП скоро будет доступна».

        Показывается после выбора тарифа и нажатия СБП, пока канал не подключён:
        клиент видит путь оплаты целиком, но денег мы не берём и заявок не создаём.
        """
        text = texts.PAYMENT_SOON.format(
                title=plan.title,
                price=texts.format_rub(plan.price_rub),
                days=plan.days,
                support=settings.support_contact or "кнопка «☎️ Поддержка»",
        )
        markup = keyboards.docs_back_kb()
        if isinstance(target, CallbackQuery):
                await view.edit_screen(target.message, text, reply_markup=markup, disable_web_page_preview=True)
        else:
                await target.answer(text, reply_markup=markup, disable_web_page_preview=True)


@router.callback_query(F.data == "plans")
async def cb_plans(call: CallbackQuery, session: AsyncSession, user: User) -> None:
        await send_plans(call, session, user)


@router.message(F.text == keyboards.BTN_PLANS)
async def msg_plans(message: Message, session: AsyncSession, user: User) -> None:
        await send_plans(message, session, user)


@router.callback_query(F.data.startswith("plan:"))
async def cb_plan_card(call: CallbackQuery, session: AsyncSession, user: User) -> None:
        plan_id = int(call.data.split(":", 1)[1])
        plan = await orders.get_plan(session, plan_id)
        if plan is None or not plan.is_active:
                await call.answer("Тариф недоступен", show_alert=True)
                return

        available = payments.available()
        # Пока оплата не подключена, показываем карточку тарифа и путь оплаты
        # целиком: тариф → СБП → заглушка «скоро». Деньги не принимаем.
        if not settings.sales_enabled:
                await _show_plan_card(call, plan, providers=[], sbp_soon=True)
                return

        if not available:
                await call.answer("Приём оплаты временно недоступен", show_alert=True)
                return

        await _show_plan_card(
                call,
                plan,
                providers=[(p.code, PROVIDER_TITLES.get(p.code, p.title)) for p in available],
                session=session,
                user=user,
        )


async def _show_plan_card(
        call: CallbackQuery,
        plan,  # noqa: ANN001 - Plan
        *,
        providers: list[tuple[str, str]],
        sbp_soon: bool = False,
        session: AsyncSession | None = None,
        user: User | None = None,
) -> None:
        """Карточка тарифа с ценами, скидкой и способами оплаты."""
        promo_row = await promo.available(session, user) if (session is not None and user is not None) else None
        discount = promo.make_discount(promo_row, plan.price_rub) if promo_row else None
        price = discount.amount_rub if discount else plan.price_rub

        stars_line = ""
        if settings.stars_enabled and plan.price_stars and not sbp_soon:
                if discount and discount.discount_rub:
                        stars_line = texts.STARS_LINE_DISCOUNT.format(
                                stars=discount.stars_for(plan.price_stars), base_stars=plan.price_stars
                        )
                else:
                        stars_line = texts.STARS_LINE.format(stars=plan.price_stars)

        if discount and discount.discount_rub:
                price_line = texts.PRICE_LINE_DISCOUNT.format(
                        base=plan.price_rub,
                        price=price,
                        per_month=round(price / max(1, plan.days) * 30),
                        percent=discount.percent,
                        stars_line=stars_line,
                )
        else:
                price_line = texts.PRICE_LINE.format(
                        price=price,
                        per_month=round(price / max(1, plan.days) * 30),
                        stars_line=stars_line,
                )

        text = texts.PLAN_CARD.format(
                title=plan.title,
                price_line=price_line,
                days=plan.days,
                devices=plan.devices_limit,
        )
        if sbp_soon:
                text += texts.PLAN_CARD_SOON_NOTE

        markup = keyboards.providers_kb(plan.id, providers, sbp_soon=sbp_soon)
        await view.edit_screen(call.message, text, reply_markup=markup)
        await call.answer()


@router.callback_query(F.data.startswith("sbp:soon:"))
async def cb_sbp_soon(call: CallbackQuery, session: AsyncSession) -> None:
        """СБП выбран, но канал ещё не подключён — показываем заглушку."""
        plan = await orders.get_plan(session, int(call.data.rsplit(":", 1)[1]))
        if plan is None:
                await call.answer("Тариф недоступен", show_alert=True)
                return
        await show_payment_soon(call, plan)
        await call.answer()


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(call: CallbackQuery, session: AsyncSession, user: User) -> None:
        _, plan_id_raw, provider_code = call.data.split(":")

        if not settings.sales_enabled:
                # Защита от старых кнопок: даже если у клиента осталось сообщение
                # с тарифом, деньги не принимаем — показываем заглушку «скоро».
                plan = await orders.get_plan(session, int(plan_id_raw))
                await call.answer("Оплата по СБП скоро будет доступна")
                if plan is not None:
                        await show_payment_soon(call, plan)
                return

        plan = await orders.get_plan(session, int(plan_id_raw))
        provider = payments.get(provider_code)
        if plan is None or provider is None:
                await call.answer("Способ оплаты недоступен", show_alert=True)
                return

        order = await orders.create_order(session, user, plan, provider=provider.code)
        title = f"{texts.BRAND}: {plan.title}"
        # Точная цена в звёздах для этого заказа: со скидкой она ниже тарифной.
        stars_price = order.stars_amount or plan.price_stars

        try:
                invoice = await provider.create_invoice(
                        order.id,
                        order.amount_rub,
                        title,
                        # У Stars своя сетка цен — берём цену заказа (уже со скидкой);
                        # ручному переводу нужна точная сумма с уникальными копейками,
                        # по которым автоплатёж находит заказ.
                        price_override=stars_price or None,
                        exact_kopecks=order.pay_amount_kopecks,
                        # Platega просит metadata для антифрода; остальные провайдеры
                        # эти аргументы игнорируют.
                        payer_user_id=user.tg_id,
                        payer_user_name=f"@{user.username}" if user.username else (user.display_name or ""),
                )
        except PaymentError as exc:
                logger.warning("Ошибка создания счёта: %s", exc)
                await view.edit_screen(call.message, texts.ERROR_GENERIC, reply_markup=keyboards.back_to_menu_kb())
                await call.answer("Не удалось создать счёт", show_alert=True)
                return

        # Связываем заказ со счётом провайдера. Раньше здесь стояла проверка
        # «external_id == ord-<id>», но заказ создаётся с ``ord-<hex>`` — условие не
        # срабатывало никогда, и заказ оставался со своим ярлыком. Из этого росли две
        # поломки: опрос статуса уходил по чужому id (``GET /transaction/ord-…``), а
        # вебхук Platega без payload не мог найти заказ по id транзакции.
        order.external_id = invoice.external_id
        await session.flush()

        discount_note = ""
        if order.discount_rub:
                discount_note = texts.DISCOUNT_NOTE.format(discount=order.discount_rub, code=order.promo_code)

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
        elif provider.code.startswith("platega"):
                text = texts.ORDER_CREATED_PLATEGA.format(
                        order_id=order.id,
                        amount=order.amount_rub,
                        method=PROVIDER_TITLES.get(provider.code, provider.title).lstrip("💳 "),
                )
                markup = keyboards.crypto_order_kb(order.id, invoice.pay_url or "")
        else:
                text = texts.ORDER_CREATED_STARS.format(order_id=order.id, amount=order.amount_rub)
                if settings.stars_reseller_url:
                        text += texts.STARS_NO_BALANCE_HINT.format(stars=stars_price)
                markup = keyboards.stars_order_kb(
                        order.id,
                        invoice.pay_url or "",
                        reseller_url=settings.stars_reseller_url,
                        stars=stars_price,
                )

        await view.edit_screen(call.message,
                discount_note + text, reply_markup=markup, disable_web_page_preview=True
        )
        await call.answer()


@router.callback_query(F.data.startswith("order:cancel:"))
async def cb_cancel(call: CallbackQuery, session: AsyncSession) -> None:
        order = await orders.get_order(session, int(call.data.rsplit(":", 1)[1]))
        if order is None:
                await call.answer(texts.ORDER_NOT_FOUND, show_alert=True)
                return
        await orders.cancel_order(session, order, reason="canceled by user")
        await view.edit_screen(call.message,
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
        await view.edit_screen(call.message,
                texts.ORDER_WAITING_CONFIRM.format(order_id=order.id),
                reply_markup=keyboards.back_to_menu_kb(),
        )
        await call.answer("Передали на проверку ✅")
        # Заявка уходит в чат команды вместе с кнопками решения: подтвердить оплату
        # с телефона, не заходя в админку. Кнопки живут у того бота, который
        # отправил сообщение (namespace), иначе тап по ним никто не обработает.
        await notifications.notify_admins_with_buttons(
                bot,
                texts.ADMIN_NEW_ORDER.format(
                        order_id=order.id,
                        user=notifications.safe(user.display_name),
                        tg_id=user.tg_id,
                        plan=notifications.safe(plan.title) if plan else "—",
                        amount=order.amount_rub,
                        provider=PROVIDER_TITLES.get(order.provider, order.provider),
                ),
                keyboards.admin_order_kb(order.id, namespace=notify_bot.namespace()),
        )


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
        if provider is None and (order.provider or "").startswith("platega"):
                # Метод могли убрать из настроек (например, карты отключили) — заказ
                # всё равно надо довести до оплаты: данные у методов Platega одни.
                provider = platega_provider(order.provider)
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
        """Единая точка выдачи доступа после успешной оплаты.

        Сюда приходят только подтверждённые платежи: вебхук, опрос платёжной
        системы, оплата звёздами, кнопка «Проверить оплату». Поэтому заказ,
        который успел закрыться (клиент платил с телефона и не вернулся в бот),
        здесь открывается заново и обслуживается как обычный: человек заплатил —
        он не должен ждать, пока владелец посмотрит телефон и подтвердит вручную.
        """
        if order.status in {"canceled", "expired"}:
                logger.info(
                        "Заказ #%s был закрыт (%s), но оплата подтверждена — открываю и выдаю доступ",
                        order.id,
                        order.status,
                )
                await events.log_event(
                        session,
                        events.ORDER_PAID,
                        user_id=user.id,
                        payload={"order_id": order.id, "reopened_from": order.status},
                )
                # Открываем заказ условным UPDATE, а не присваиванием в памяти:
                # пока мы читали статус и ходили в платёжную систему, тот же
                # заказ мог подтвердить другой путь (опрос, вебхук, кнопка).
                # Присваивание затёрло бы его результат и позволило выдать
                # доступ второй раз (инцидент 09.10.2026, заказ #8).
                await session.execute(
                        update(Order)
                        .where(Order.id == order.id, Order.status.in_(("canceled", "expired")))
                        .values(status="pending")
                )
                await session.refresh(order)
                if order.status != "pending":
                        logger.info(
                                "Заказ #%s уже обработан другим путём (%s) — второй раз не выдаю",
                                order.id,
                                order.status,
                        )
                        return

        panel = await subscriptions.all_user_panels(session)
        try:
                sub, already = await orders.mark_paid(session, order, panel, bot=bot)
        except PanelError as exc:
                logger.error("Панель не выдала доступ по заказу %s: %s", order.id, exc)
                await events.log_event(session, events.PANEL_ERROR, user_id=user.id, payload={"order_id": order.id})
                await notifications.notify_admins(bot, f"⚠️ Панель не выдала доступ по заказу #{order.id}: <code>{exc}</code>")
                return

        if already or sub is None:
                # Подарок: доступ покупателю не выдаём — ему уходит сертификат,
                # а дни получит тот, кому он его передаст.
                if not already and order.gift_token:
                        from app.services import gift as gift_service

                        sent = await gift_service.notify_buyer(session, order, bot, user)
                        plan = await orders.get_plan(session, order.plan_id) if order.plan_id else None
                        await notifications.notify_admins(
                                bot,
                                f"🎁 Подарок оплачен: заказ #{order.id}, {order.amount_rub} ₽, "
                                f"{notifications.safe(plan.title) if plan else '—'}, покупатель "
                                f"{notifications.safe(user.display_name)} (<code>{user.tg_id}</code>)"
                                + ("" if sent else "\n⚠️ Сертификат не доставлен — напиши покупателю вручную."),
                        )
                        return
                # Заказ мог быть отменён (например, клиент выбрал другой тариф или
                # способ оплаты, а деньги по старому счёту всё-таки пришли).
                # Штатно такой заказ открывается выше и обслуживается сам; сюда
                # попадаем, только если выдача не сложилась — тогда зовём админа.
                if sub is None and not already:
                        await notifications.notify_admins(
                                bot,
                                f"⚠️ <b>Оплата подтверждена, но доступ по заказу #{order.id} не выдан</b>\n"
                                f"Пользователь: {notifications.safe(user.display_name)} "
                                f"(<code>{user.tg_id}</code>)\n"
                                f"Сумма: {order.amount_rub} ₽, способ: {notifications.safe(order.provider)}\n"
                                f"Причина: {notifications.safe(order.grant_last_error or 'панель не ответила')}\n\n"
                                "Выдачу повторю автоматически (job_grant_paid). "
                                "Если не получится — проверь заказ в /admin.",
                        )
                return

        link = subscriptions.subscription_link(sub.subscription_token)
        text = texts.order_paid_text(
                order, expires=_expires_text(sub.expires_at), days=sub.days_left, link=link
        )
        await bot.send_message(
                user.tg_id,
                text,
                reply_markup=keyboards.connect_kb(link),
                disable_web_page_preview=True,
        )
        plan = await orders.get_plan(session, order.plan_id) if order.plan_id else None
        await notifications.notify_admins(
                bot,
                await notifications.payment_notice(
                        session,
                        order,
                        user,
                        provider=PROVIDER_TITLES.get(order.provider, order.provider),
                        plan_title=plan.title if plan else "",
                ),
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
        # Сверяем со снимком цены в заказе: со скидкой она ниже тарифной.
        expected = order.stars_amount or (plan.price_stars if plan else 0)
        if query.currency != "XTR" or (expected and query.total_amount != expected):
                logger.warning(
                        "Stars: несовпадение счёта order=%s currency=%s amount=%s expected=%s",
                        order.id,
                        query.currency,
                        query.total_amount,
                        expected,
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
                        f"Пользователь: {notifications.safe(user.display_name)} "
                        f"(<code>{user.tg_id}</code>), "
                        f"аккаунт создан {user.created_at:%d.%m.%Y}\n"
                        f"Заказ #{order.id}: {payment.total_amount} ⭐ ({order.amount_rub} ₽)\n"
                        f"Charge ID: <code>{payment.telegram_payment_charge_id}</code>\n\n"
                        "Если звёзды окажутся крадеными, Telegram спишет их с баланса — "
                        "подписку придётся отключить вручную (/block).",
                )
