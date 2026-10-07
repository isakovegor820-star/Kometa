"""Клавиатуры бота: одно место для всей навигации."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.config import get_settings
from app.db.models import Plan

settings = get_settings()

BTN_TRIAL = "🎁 Попробовать бесплатно"
BTN_PLANS = "💎 Тарифы"
BTN_MY_SUB = "📡 Моя подписка"
BTN_HELP = "❓ Помощь"
BTN_REFERRAL = "🎁 Пригласи друга — месяц бесплатно"
BTN_HOWTO = "📱 Как подключить"
BTN_RESERVE = "🛟 Если не открывается"
BTN_LEGAL = "📄 Документы и цены"


def main_menu(has_subscription: bool, is_active: bool, min_price: int = 0) -> InlineKeyboardMarkup:
    """Главное меню.

    :param min_price: цена самого дешёвого тарифа. Показываем её в кнопке —
        клиент видит стоимость, не открывая раздел с тарифами.
    """
    plans_label = f"{BTN_PLANS} — от {min_price} ₽" if min_price else BTN_PLANS
    kb = InlineKeyboardBuilder()
    if is_active:
        kb.button(text=BTN_MY_SUB, callback_data="sub:show")
        kb.button(text="💎 Продлить", callback_data="plans")
    elif has_subscription:
        kb.button(text="💎 Продлить доступ", callback_data="plans")
    else:
        kb.button(text=BTN_TRIAL, callback_data="trial:start")
        kb.button(text=plans_label, callback_data="plans")
    kb.button(text=BTN_HOWTO, callback_data="sub:howto")
    kb.button(text=BTN_RESERVE, callback_data="sub:reserve")
    kb.button(text=BTN_REFERRAL, callback_data="ref:show")
    kb.button(text=BTN_LEGAL, callback_data="legal:show")
    kb.button(text=BTN_HELP, callback_data="help")
    kb.adjust(2, 1, 1, 2, 1)
    return kb.as_markup()


def reply_menu() -> ReplyKeyboardMarkup:
    """Постоянное меню снизу — быстрый доступ к главному."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MY_SUB), KeyboardButton(text=BTN_PLANS)],
            [KeyboardButton(text=BTN_HOWTO), KeyboardButton(text=BTN_RESERVE)],
            [KeyboardButton(text=BTN_HELP), KeyboardButton(text=BTN_LEGAL)],
        ],
        resize_keyboard=True,
        is_persistent=False,
    )


def plans_kb(
    plans: list[Plan],
    show_stars: bool = False,
    discount_percent: int = 0,
    max_discount_rub: int = 0,
    show_promo_button: bool = False,
) -> InlineKeyboardMarkup:
    """Тарифы. Если есть скидка — она уже в цене на кнопках."""
    from app.services.promo import calc_discount_rub

    kb = InlineKeyboardBuilder()
    for plan in plans:
        discount_rub = calc_discount_rub(plan.price_rub, discount_percent, max_discount_rub)
        price = plan.price_rub - discount_rub
        per_month = round(price / max(1, plan.days) * 30)
        if discount_rub:
            label = f"{plan.title} — {texts.format_rub(price)} ₽ вместо {texts.format_rub(plan.price_rub)} ₽ 🎉"
        elif show_stars and plan.price_stars:
            label = f"{plan.title} — {texts.format_rub(plan.price_rub)} ₽ или {plan.price_stars} ⭐"
        else:
            label = f"{plan.title} — {texts.format_rub(plan.price_rub)} ₽ ({texts.format_rub(per_month)} ₽/мес)"
        kb.button(text=label, callback_data=f"plan:{plan.id}")
    if show_promo_button:
        kb.button(text="🎟 У меня есть промокод", callback_data="promo:enter")
    kb.button(text="⬅️ Назад", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def plans_button_kb() -> InlineKeyboardMarkup:
    """Кнопка «выбрать тариф» для приветствия приглашённого."""
    kb = InlineKeyboardBuilder()
    kb.button(text="💎 Выбрать тариф со скидкой", callback_data="plans")
    kb.button(text="⬅️ В меню", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def providers_kb(
    plan_id: int,
    providers: list[tuple[str, str]],
    sbp_soon: bool = False,
) -> InlineKeyboardMarkup:
    """Способы оплаты для выбранного тарифа.

    :param sbp_soon: оплата ещё не подключена — показываем СБП и ведём на
        заглушку «скоро», а не на счёт.
    """
    kb = InlineKeyboardBuilder()
    for code, title in providers:
        kb.button(text=title, callback_data=f"pay:{plan_id}:{code}")
    if sbp_soon:
        kb.button(text=texts.SBP_SOON_BTN, callback_data=f"sbp:soon:{plan_id}")
    kb.button(text="🎟 У меня есть промокод", callback_data="promo:enter")
    kb.button(text="⬅️ Назад к тарифам", callback_data="plans")
    kb.adjust(1)
    return kb.as_markup()


def referral_kb(share_url: str) -> InlineKeyboardMarkup:
    """Кнопки экрана «Пригласи друга»: поделиться, показать код, список друзей."""
    from urllib.parse import quote

    kb = InlineKeyboardBuilder()
    if share_url:
        text = quote(
            f"Забирай скидку {settings.referral_discount_percent}% на первое подключение 👇",
            safe="",
        )
        kb.button(
            text="📤 Поделиться ссылкой",
            url=f"https://t.me/share/url?url={quote(share_url, safe='')}&text={text}",
        )
    kb.button(text="🎟 Мой промокод", callback_data="ref:code")
    kb.button(text="👥 Мои друзья", callback_data="ref:friends")
    kb.button(text="⬅️ В меню", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def manual_order_kb(order_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🆘 Оплатил, но доступа нет", callback_data=f"order:manual:{order_id}")
    kb.button(text="⬅️ Отменить заказ", callback_data=f"order:cancel:{order_id}")
    kb.adjust(1)
    return kb.as_markup()


def crypto_order_kb(order_id: int, pay_url: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🪙 Оплатить", url=pay_url)
    kb.button(text="🔄 Проверить оплату", callback_data=f"order:check:{order_id}")
    kb.button(text="⬅️ Отменить", callback_data=f"order:cancel:{order_id}")
    kb.adjust(1)
    return kb.as_markup()


def stars_order_kb(order_id: int, pay_url: str, reseller_url: str = "", stars: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⭐️ Оплатить звёздами", url=pay_url)
    if reseller_url:
        label = f"🛒 Купить {stars} ⭐" if stars else "🛒 Купить звёзды"
        kb.button(text=label, url=reseller_url)
    kb.button(text="⬅️ Отменить", callback_data=f"order:cancel:{order_id}")
    kb.adjust(1)
    return kb.as_markup()


def connect_kb(sub_url: str) -> InlineKeyboardMarkup:
    """Кнопки подключения: одно нажатие — профиль уже в приложении.

    ВАЖНО: Telegram **запрещает** нестандартные схемы (``happ://``,
    ``v2rayng://``, ``hiddify://``) в inline-кнопках — на такую клавиатуру он
    отвечает ``Bad Request: Unsupported URL protocol``, и сообщение с кнопками
    не уходит вовсе (проверено на живом боте 06.10.2026). Поэтому кнопки ведут
    на нашу https-страницу ``/connect/<token>``: она открывает приложение по
    схеме, а если приложение не установлено — показывает ссылку для копирования.

    Имя профиля клиенты берут из фрагмента ссылки (``#Kometa``) — страница
    подключения подставляет его сама.
    """
    from app.config import get_settings

    kb = InlineKeyboardBuilder()
    token = sub_url.rstrip("/").rsplit("/", 1)[-1] if sub_url else ""
    base = (get_settings().public_base_url or "").rstrip("/")

    if token and base:
        page = f"{base}/connect/{token}"
        kb.button(text="🟢 Подключить в Happ", url=f"{page}?app=happ")
        kb.button(text="🔵 Подключить в v2rayNG", url=f"{page}?app=v2rayng")
        kb.button(text="🟣 Подключить в Hiddify", url=f"{page}?app=hiddify")
    if sub_url:
        kb.button(text="📋 Скопировать ссылку", callback_data="sub:copy")
    kb.button(text="⬅️ В меню", callback_data="menu:main")
    kb.adjust(1, 1, 1, 1, 1)
    return kb.as_markup()


def subscription_kb(has_panel_user: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Ссылка-подписка", callback_data="sub:link")
    kb.button(text="💎 Продлить", callback_data="plans")
    kb.button(text="🔄 Обновить", callback_data="sub:refresh")
    kb.button(text="⬅️ В меню", callback_data="menu:main")
    kb.adjust(1 if not has_panel_user else 2, 1, 1)
    return kb.as_markup()


def back_to_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В меню", callback_data="menu:main")
    return kb.as_markup()


def support_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if settings.support_username:
        kb.button(text="💬 Написать в поддержку", url=f"https://t.me/{settings.support_username.lstrip('@')}")
    if settings.channel_url:
        kb.button(text="📣 Наш канал", url=settings.channel_url)
    kb.button(text=BTN_LEGAL, callback_data="legal:show")
    kb.button(text="⬅️ В меню", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def legal_kb(
    privacy_url: str = "",
    terms_url: str = "",
) -> InlineKeyboardMarkup:
    """Документы и цены: отдельные кнопки, доступные в любой момент.

    Банк-партнёр проверяет именно это: политика, соглашение, контакт поддержки
    и актуальные тарифы должны открываться у клиента в один тап. Если ссылка
    на документ ещё не опубликована, кнопка показывает текст внутри бота —
    доступность важнее ссылки.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.LEGAL_PRICING_BTN, callback_data="legal:pricing")
    kb.button(
        text=texts.LEGAL_PRIVACY_BTN,
        url=privacy_url or None,
        callback_data=None if privacy_url else "legal:privacy",
    )
    kb.button(
        text=texts.LEGAL_TERMS_BTN,
        url=terms_url or None,
        callback_data=None if terms_url else "legal:terms",
    )
    kb.button(text=texts.LEGAL_SUPPORT_BTN, callback_data="legal:support")
    kb.button(text="⬅️ В меню", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def docs_back_kb() -> InlineKeyboardMarkup:
    """Кнопки под прайсом и документами: тарифы, назад в раздел, в меню."""
    kb = InlineKeyboardBuilder()
    kb.button(text=BTN_PLANS, callback_data="plans")
    kb.button(text=BTN_LEGAL, callback_data="legal:show")
    kb.button(text="⬅️ В меню", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def admin_order_kb(order_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"admin:confirm:{order_id}")
    kb.button(text="🚫 Отклонить", callback_data=f"admin:reject:{order_id}")
    kb.adjust(2)
    return kb.as_markup()


def admin_panel_kb(pending_count: int = 0, nodes_ok: bool = True, panel_url: str | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🧾 Заявки ({pending_count})", callback_data="admin:orders")
    kb.button(text="📊 Статистика", callback_data="admin:stats")
    kb.button(text="🖥 Ноды", callback_data="admin:nodes")
    if panel_url:
        kb.button(text="🌐 Веб-панель", url=panel_url)
    kb.adjust(1)
    return kb.as_markup()
