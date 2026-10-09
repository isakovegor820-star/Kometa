"""Покупка подписки в подарок и активация сертификата.

Поток покупателя:
        🎁 Подарить подписку → срок → открытка (необязательно) → оплата → сертификат.

Поток получателя:
        ссылка ``?start=gift_<токен>`` (или ввод кода текстом) → дни начисляются.

Подарок намеренно стоит дороже обычной подписки (``GIFT_MARKUP_PERCENT``): это
не скидка, а отдельный продукт с отсрочкой активации. Экономика и расчёт —
в ``docs/МАРКЕТИНГ-ЭКОНОМИКА.md``.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards
from app.bot.handlers.buy import PROVIDER_TITLES
from app.config import get_settings
from app.db.models import User
from app.payments import registry as payments
from app.services import events, gift, orders, subscriptions

logger = logging.getLogger(__name__)
router = Router(name="gift")
settings = get_settings()


class GiftForm(StatesGroup):
        """Шаги оформления подарка."""

        waiting_recipient = State()
        waiting_message = State()


async def _plan_choices(session: AsyncSession) -> list[tuple[str, str, int]]:
        """Тарифы для подарка: цена уже с наценкой за подарочный статус."""
        plans = await orders.list_plans(session)
        return [(plan.code, plan.title, gift.gift_price_rub(plan.price_rub)) for plan in plans]


@router.callback_query(F.data == "gift:show")
async def show_gifts(call: CallbackQuery, session: AsyncSession) -> None:
        """Витрина подарков: срок и цена с наценкой."""
        if not settings.gift_enabled:
                await call.answer("Подарки скоро появятся", show_alert=True)
                return
        if not settings.sales_enabled:
                await call.answer("Оплата скоро будет доступна", show_alert=True)
                return
        choices = await _plan_choices(session)
        lines = "\n".join(f"• {title} — <b>{price} ₽</b>" for _, title, price in choices)
        await call.message.edit_text(
                "🎁 <b>Подарить подписку</b>\n\n"
                "Сертификат можно активировать в любой момент — хоть сегодня, хоть через месяц. "
                "Срок начнёт идти только после активации.\n\n"
                f"{lines}\n\n"
                f"<i>Активировал подарок — тебе {settings.gift_buyer_bonus_days} дней на подписку в благодарность.</i>",
                reply_markup=keyboards.gifts_kb(list(choices)),
                disable_web_page_preview=True,
        )
        await call.answer()


@router.callback_query(F.data.startswith("gift:plan:"))
async def choose_plan(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
        """Запомнить срок подарка и спросить, кому дарим."""
        code = call.data.rsplit(":", 1)[1]
        plans = await orders.list_plans(session)
        plan = next((row for row in plans if row.code == code), None)
        if plan is None:
                await call.answer("Такой тариф больше недоступен", show_alert=True)
                return

        await state.set_state(GiftForm.waiting_recipient)
        await state.update_data(gift_plan_id=plan.id, gift_plan_title=plan.title)
        await call.message.edit_text(
                f"🎁 <b>{plan.title}</b> — {gift.gift_price_rub(plan.price_rub)} ₽\n\n"
                "Напиши имя того, кому даришь: так подарок будет приятнее. "
                "Или нажми кнопку ниже — пришлю ссылку без имени.",
                reply_markup=keyboards.gift_recipient_kb(),
                disable_web_page_preview=True,
        )
        await call.answer()


@router.callback_query(F.data == "gift:skip_name")
async def skip_name(call: CallbackQuery, state: FSMContext, session: AsyncSession, user: User) -> None:
        """Получателя не указываем — просто выдаём ссылку на оплату."""
        data = await state.get_data()
        if not data.get("gift_plan_id"):
                await call.answer("Начни заново: «Подарить подписку»", show_alert=True)
                return
        await state.clear()
        await _send_payment(call, session, user, data["gift_plan_id"])


@router.message(GiftForm.waiting_recipient, F.text)
async def got_recipient(message: Message, state: FSMContext) -> None:
        """Имя получателя: сохраняем и спрашиваем текст открытки."""
        name = " ".join((message.text or "").split())[:40]
        await state.update_data(gift_recipient=name)
        await state.set_state(GiftForm.waiting_message)
        await message.answer(
                f"Записал: <b>{name}</b>.\n\n"
                "Хочешь добавить пару слов к подарку? Напиши текст открытки "
                "или отправь «-», чтобы пропустить.",
                reply_markup=keyboards.back_to_menu_kb(),
        )


@router.message(GiftForm.waiting_message, F.text)
async def got_message(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
        """Текст открытки (или «-») → создаём подарочный заказ и показываем оплату."""
        data = await state.get_data()
        plan_id = data.get("gift_plan_id")
        if not plan_id:
                await state.clear()
                await message.answer("Начни заново: «Подарить подписку».", reply_markup=keyboards.back_to_menu_kb())
                return

        raw = " ".join((message.text or "").split())
        note = "" if raw in {"-", "—", "нет"} else raw[:200]
        await state.clear()
        await _send_payment(
                message, session, user, plan_id, recipient=data.get("gift_recipient", ""), note=note
        )


async def _send_payment(
        event: Message | CallbackQuery,
        session: AsyncSession,
        user: User,
        plan_id: int,
        *,
        recipient: str = "",
        note: str = "",
) -> None:
        """Создать подарочный заказ и показать способы оплаты."""
        if not settings.sales_enabled:
                await event.answer("Приём оплаты скоро откроется")
                return

        plan = await orders.get_plan(session, plan_id)
        if plan is None:
                await event.answer("Тариф больше недоступен")
                return

        # Подарок — отдельный продукт: скидки на первую оплату к нему не применяем,
        # иначе смысл подарка и его цена перестают сходиться.
        order = await orders.create_order(
                session, user, plan, provider="manual", kind=orders.KIND_GIFT, with_discount=False
        )
        await gift.attach_gift(session, order, message=note)
        # Цену подарка фиксируем в самом заказе: она выше обычной, и оплата
        # обязана сойтись с тем, что человек видел на экране.
        order.amount_rub = gift.gift_price_rub(plan.price_rub)
        order.base_amount_rub = order.amount_rub
        await events.log_event(
                session,
                events.GIFT_BOUGHT,
                user_id=user.id,
                payload={"order_id": order.id, "plan": plan.code, "recipient": recipient},
        )

        price = gift.gift_price_rub(plan.price_rub)
        text = (
                f"🎁 <b>Подарок: {plan.title}</b>\n\n"
                f"К оплате: <b>{price} ₽</b>\n\n"
                "Выбери, как удобнее оплатить. После подтверждения оплаты я пришлю готовую "
                "ссылку-подарок — её можно переслать сразу."
        )
        available = payments.available()
        markup = keyboards.gift_pay_kb(
                order.id,
                [(p.code, PROVIDER_TITLES.get(p.code, p.title)) for p in available],
        )
        if isinstance(event, CallbackQuery):
                await event.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
                await event.answer()
        else:
                await event.answer(text, reply_markup=markup, disable_web_page_preview=True)


# ------------------------------------------------------------------ активация
async def activate_from_payload(
        message: Message,
        session: AsyncSession,
        user: User,
        bot: Bot,
        payload: str,
) -> bool:
        """Активировать подарок по deep-link. True — payload был подарочным."""
        if not payload.startswith("gift_"):
                return False
        token = payload[len("gift_"):]
        await _activate(message, session, user, bot, token)
        return True


@router.message(F.text.regexp(r"(?i)^(kometa-)?gift[-_ ]?[a-z0-9]{4,12}$"))
async def activate_by_text(message: Message, session: AsyncSession, user: User, bot: Bot) -> None:
        """Человек прислал код подарка текстом — тоже активируем."""
        raw = (message.text or "").strip()
        if await gift.get_by_token(session, raw) is None:
                raise SkipHandler()
        await _activate(message, session, user, bot, raw)


async def _activate(
        message: Message,
        session: AsyncSession,
        user: User,
        bot: Bot,
        token: str,
) -> None:
        """Общая активация: найти заказ, выдать дни, поблагодарить покупателя."""
        order = await gift.get_by_token(session, token)
        if order is None:
                await message.answer(
                        "Не нашёл такой подарок. Проверь код: он выглядит как "
                        "<code>KOMETA-GIFT-XXXXXXXX</code>.",
                        reply_markup=keyboards.back_to_menu_kb(),
                )
                return

        panel = await subscriptions.all_user_panels(session)
        try:
                activation = await gift.redeem(session, order, user, panel)
        except gift.GiftError as exc:
                await message.answer(exc.message, reply_markup=keyboards.back_to_menu_kb())
                return

        await message.answer(gift.activation_text(activation), reply_markup=keyboards.back_to_menu_kb())

        if activation.already_activated:
                return

        if activation.buyer is not None and not activation.buyer.is_blocked:
                try:
                        await bot.send_message(
                                activation.buyer.tg_id,
                                gift.buyer_thanks_text(activation),
                                disable_web_page_preview=True,
                        )
                except Exception:  # noqa: BLE001 - покупатель мог заблокировать бота
                        logger.info("Не смог поблагодарить покупателя подарка #%s", order.id)


# ------------------------------------------------------------------ оплата
@router.callback_query(F.data.startswith("gift:pay:"))
async def pay_gift(call: CallbackQuery, session: AsyncSession, user: User) -> None:
        """Счёт на подарочный сертификат: сумма уже с наценкой за подарок."""
        if not settings.sales_enabled:
                await call.answer("Приём оплаты скоро откроется")
                return

        _, _, order_raw, provider_code = call.data.split(":")
        order = await orders.get_order(session, int(order_raw))
        provider = payments.get(provider_code)
        if order is None or provider is None or order.user_id != user.id:
                await call.answer("Заказ недоступен", show_alert=True)
                return
        if order.status != "pending":
                await call.answer("Заказ уже не активен", show_alert=True)
                return

        order.provider = provider.code
        await session.flush()

        plan = await orders.get_plan(session, order.plan_id) if order.plan_id else None
        title = f"{texts.BRAND}: подарок «{plan.title if plan else 'подписка'}»"
        try:
                invoice = await provider.create_invoice(
                        order.id,
                        order.amount_rub,
                        title,
                        price_override=None,
                        exact_kopecks=order.pay_amount_kopecks,
                )
        except PaymentError as exc:
                logger.warning("Ошибка счёта на подарок: %s", exc)
                await call.message.edit_text(texts.ERROR_GENERIC, reply_markup=keyboards.back_to_menu_kb())
                await call.answer("Не удалось создать счёт", show_alert=True)
                return

        # Связываем заказ со счётом провайдера (см. тот же разбор в buy.py):
        # ярлык заказа — ``ord-<hex>``, поэтому проверка «ord-<id>» не срабатывала.
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
        elif provider.code in {"crypto", "wata"} or provider.code.startswith("platega"):
                text = (
                        f"🎁 <b>Подарок: заказ #{order.id}</b>\n\n"
                        f"К оплате: <b>{order.amount_rub} ₽</b>\n"
                        "Нажми кнопку ниже — откроется страница оплаты."
                )
                markup = keyboards.crypto_order_kb(order.id, invoice.pay_url or "")
        else:
                text = texts.ORDER_CREATED_STARS.format(order_id=order.id, amount=order.amount_rub)
                markup = keyboards.stars_order_kb(
                        order.id,
                        invoice.pay_url or "",
                        reseller_url=settings.stars_reseller_url,
                        stars=order.stars_amount,
                )

        await call.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        await call.answer()
