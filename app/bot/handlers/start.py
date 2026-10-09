"""Хендлеры: /start, главное меню, навигация."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import gate, keyboards, texts, view
from app.config import get_settings
from app.db.models import User
from app.panels.registry import registry
from app.services import (
    attribution,
    channel_gate,
    orders,
    partners,
    personal_links,
    promo,
    referral,
    subscriptions,
)

router = Router(name="start")
settings = get_settings()


async def main_menu_view(
    session: AsyncSession, user: User, *, hero: bool = False
) -> view.MenuView:
    """Что показать в главном меню — по состоянию подписки.

    :param hero: первый вход после /start. Тогда экран показываем картинкой
        (фото + подпись + кнопки, одним сообщением) и обращаемся по имени.
        Дальше — обычный текст: картинка работает один раз, при знакомстве.
    """
    sub = await subscriptions.get_subscription(session, user.id)
    markup = keyboards.main_menu(
        has_subscription=sub is not None,
        is_active=bool(sub and sub.is_active),
        min_price=await _min_price(session),
    )
    if sub is None:
        # Новый человек: оффер в заголовке, риск снят пробным доступом.
        return view.MenuView(
            text=texts.welcome(user.display_name if hero else None),
            markup=markup,
            photo=view.START_HERO if hero else None,
        )
    if sub.is_active:
        text = texts.MENU_ACTIVE
    else:
        text = texts.MENU_EXPIRED
    # Скидку показываем там, где человек точно её увидит.
    promo_row = await promo.available(session, user)
    if promo_row is not None:
        text += texts.MENU_DISCOUNT_HINT.format(percent=promo_row.percent)
    return view.MenuView(text=text, markup=markup)


async def _min_price(session: AsyncSession) -> int:
    """Цена самого дешёвого тарифа — её видно прямо в кнопке.

    Клиент понимает порядок цен, не открывая раздел «Тарифы».
    """
    plans = await orders.list_plans(session)
    return min((plan.price_rub for plan in plans), default=0)


async def send_referral_greeting(
    message: Message, session: AsyncSession, user: User, referrer: User
) -> None:
    """Поздороваться с приглашённым и сразу показать его скидку."""
    plans = await orders.list_plans(session)
    percent = settings.referral_discount_percent
    examples = "\n".join(
        texts.REFERRAL_GREETING_EXAMPLE.format(
            title=plan.title,
            base=plan.price_rub,
            price=plan.price_rub
            - promo.calc_discount_rub(plan.price_rub, percent, settings.referral_discount_max_rub),
        )
        for plan in plans[:3]
    )
    await message.answer(
        texts.REFERRAL_GREETING.format(
            referrer=referrer.display_name,
            percent=percent,
            examples=examples,
            invited_days=settings.referral_bonus_days_invited,
            code=promo.code_for_referral(referrer.referral_code),
        ),
        reply_markup=keyboards.plans_button_kb(),
        disable_web_page_preview=True,
    )


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, user: User, bot: Bot) -> None:
    payload = ""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        payload = parts[1].strip()

    # Метка источника — до всего остального: она нужна, даже если человек не
    # пройдёт гейт подписки на канал. Без неё нельзя посчитать стоимость
    # привлечения по каналам (docs/МАРКЕТИНГ-ЭКОНОМИКА.md).
    await attribution.record_source(session, user, payload)

    # Партнёрская ссылка ``?start=src_<код>``: закрепляем человека за партнёром,
    # чтобы считать его переходы, оплаты и нашу выплату (app/services/partners.py).
    if payload.startswith("src_"):
        await partners.attach_partner(session, user, payload[4:])

    # Персональная ссылка ``?start=p_<код>``: своя скидка под конкретного
    # человека. Показываем её сразу — иначе он не поймёт, что условия особые.
    if payload.startswith("p_"):
        link = await personal_links.attach_link(session, user, payload[2:])
        if link is not None:
            await message.answer(
                texts.PERSONAL_LINK_GREETING.format(
                    title=link.title,
                    percent=link.discount_percent,
                    referrer_days=settings.referral_bonus_days_referrer,
                ),
                reply_markup=keyboards.plans_button_kb(),
                disable_web_page_preview=True,
            )

    referrer = None
    if payload.startswith("ref_"):
        referrer = await referral.attach_referrer(session, user, payload[4:])
        if referrer is not None and referrer.id == user.id:
            referrer = None

    # Подарок активируем сразу и тоже до гейта: человек пришёл за подарком,
    # а не за проверкой подписки на канал. Дни при этом уже его.
    if payload.startswith("gift_"):
        from app.bot.handlers import gift as gift_handlers

        await gift_handlers.activate_from_payload(message, session, user, bot, payload)

    # Приветствие со скидкой показываем ДО гейта: ``attach_referrer`` отдаёт
    # пригласившего только в момент привязки, то есть ровно на этом /start. Если
    # отложить его до проверки подписки, друг не увидит обещанный подарок
    # никогда: повторный /start вернёт None, потому что referred_by уже стоит.
    if referrer is not None:
        await send_referral_greeting(message, session, user, referrer)

    # Обязательная подписка на канал: пока её нет (и нет активной подписки на
    # сервис), дальше экрана подписки человек не пройдёт. Пригласившего при
    # этом уже записали — бонус не теряется.
    if not (await channel_gate.verdict(session, bot, user)).allowed:
        await gate.show(message)
        return

    # Hero: фото + подпись + кнопки одним сообщением, следом — постоянное меню.
    # Отдельного сообщения «Быстрое меню » больше нет: клавиатура ставится
    # сама, а лишнее сообщение только раздувало первый экран.
    menu = await main_menu_view(session, user, hero=True)
    await view.send_view(message, menu, reply_markup=keyboards.reply_menu())


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    """Вернуться в меню. Картинку не пересылаем — правим текст на месте."""
    menu = await main_menu_view(session, user)
    await view.edit_view(call, menu.text, menu.markup)
    await call.answer()


@router.message(Command("menu"))
async def cmd_menu(message: Message, session: AsyncSession, user: User) -> None:
    menu = await main_menu_view(session, user)
    await view.send_view(message, menu)


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Command("support"))
async def cmd_support(message: Message) -> None:
    await message.answer(texts.HELP, reply_markup=keyboards.support_kb())
