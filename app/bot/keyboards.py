"""Клавиатуры бота: одно место для всей навигации."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import get_settings
from app.db.models import Plan

settings = get_settings()

BTN_TRIAL = "🎁 Попробовать бесплатно"
BTN_PLANS = "💎 Тарифы"
BTN_MY_SUB = "📡 Моя подписка"
BTN_HELP = "❓ Помощь"
BTN_REFERRAL = "👥 Пригласить друга"
BTN_HOWTO = "📱 Как подключить"


def main_menu(has_subscription: bool, is_active: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if is_active:
        kb.button(text=BTN_MY_SUB, callback_data="sub:show")
        kb.button(text="💎 Продлить", callback_data="plans")
    elif has_subscription:
        kb.button(text="💎 Продлить доступ", callback_data="plans")
    else:
        kb.button(text=BTN_TRIAL, callback_data="trial:start")
        kb.button(text=BTN_PLANS, callback_data="plans")
    kb.button(text=BTN_HOWTO, callback_data="sub:howto")
    kb.button(text=BTN_REFERRAL, callback_data="ref:show")
    kb.button(text=BTN_HELP, callback_data="help")
    kb.adjust(1, 1 if not is_active else 2, 1)
    return kb.as_markup()


def reply_menu() -> ReplyKeyboardMarkup:
    """Постоянное меню снизу — быстрый доступ к главному."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MY_SUB), KeyboardButton(text=BTN_PLANS)],
            [KeyboardButton(text=BTN_HOWTO), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=False,
    )


def plans_kb(plans: list[Plan], show_stars: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for plan in plans:
        per_month = round(plan.price_rub / max(1, plan.days) * 30)
        label = f"{plan.title} — {plan.price_rub} ₽ ({per_month} ₽/мес)"
        if show_stars and plan.price_stars:
            label = f"{plan.title} — {plan.price_rub} ₽ или {plan.price_stars} ⭐"
        kb.button(text=label, callback_data=f"plan:{plan.id}")
    kb.button(text="⬅️ Назад", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def providers_kb(plan_id: int, providers: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code, title in providers:
        kb.button(text=title, callback_data=f"pay:{plan_id}:{code}")
    kb.button(text="⬅️ Назад к тарифам", callback_data="plans")
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
    """Кнопки быстрого подключения: одно нажатие — профиль уже в приложении.

    Диплинки взяты из документации клиентов (Happ, v2rayNG, Hiddify).
    Если система не открывает схему приложения, остаётся кнопка «Скопировать».
    """
    from urllib.parse import quote

    kb = InlineKeyboardBuilder()
    if sub_url:
        kb.button(text="🟢 Подключить в Happ", url=f"happ://add/{sub_url}")
        kb.button(
            text="🔵 Подключить в v2rayNG",
            url=f"v2rayng://install-sub/?url={quote(sub_url, safe='')}%23Kometa",
        )
        kb.button(text="🟣 Подключить в Hiddify", url=f"hiddify://install-config/?url={sub_url}")
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
