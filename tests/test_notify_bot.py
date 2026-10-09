"""Бот уведомлений: куда уходят сообщения команде и что в них написано.

Главная проверка — та, ради которой всё делалось: уведомление об оплате
приходит **новым** ботом, а основной бот (бот продаж) таких сообщений
админам больше не отправляет. Клиентские письма при этом остаются у
основного бота: клиент должен получать письма от знакомого ему бота.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from app.bot import keyboards
from app.config import get_settings
from app.db.models import Order, User
from app.services import digest, notifications, notify_bot, orders, subscriptions
from tests.fakes import BOT_TOKEN, FakeSession, make_update

settings = get_settings()
ADMIN_ID = 1


@pytest.fixture(autouse=True)
def clean_notify_state(monkeypatch):  # noqa: ANN001
    """Бот уведомлений — процессный синглтон: между тестами его надо чистить."""
    monkeypatch.setattr(settings, "notify_bot_token", "")
    monkeypatch.setattr(settings, "notify_chat_ids", "")
    monkeypatch.setattr(settings, "notify_fallback_to_main", True)
    monkeypatch.setattr(settings, "notify_errors", True)
    notify_bot.set_bot(None)
    notifications._error_seen.clear()
    yield
    notify_bot.set_bot(None)
    notify_bot.set_customer_bot(None)
    notifications._error_seen.clear()


def dedicated_bot() -> Bot:
    """Второй бот с заглушкой Telegram API — «новый бот уведомлений»."""
    return Bot(token=BOT_TOKEN, session=FakeSession())


async def make_paid_order(session, *, tg_id: int, amount: int):  # noqa: ANN001
    """Пользователь + оплаченный заказ: минимальный контекст для уведомления."""
    user, _ = await subscriptions.get_or_create_user(
        session, tg_id=tg_id, username="zakon", first_name="zakon"
    )
    plans = await orders.list_plans(session)
    plan = next(p for p in plans if p.code == "m1")
    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        amount_rub=amount,
        base_amount_rub=amount + 119,
        discount_rub=119,
        provider="stars",
        status="paid",
        paid_at=datetime.now(timezone.utc),
    )
    session.add(order)
    await session.flush()
    return user, order, plan


# --------------------------------------------------------------- куда уходит
async def test_notify_admins_goes_to_dedicated_bot(bot, session):  # noqa: ANN001
    """Настроен отдельный бот — сообщение уходит им, а не ботом продаж."""
    settings.notify_bot_token = "111111:NOTIFY_TOKEN"
    settings.notify_chat_ids = "926194553"
    dedicated = dedicated_bot()
    notify_bot.set_bot(dedicated)

    sent = await notifications.notify_admins(bot, "💰 Оплата: заказ #3, 1 ₽")

    assert sent == 1
    assert "Оплата: заказ #3" in dedicated.session.all_text()
    assert bot.session.all_text() == "", "основной бот не должен слать админские уведомления"
    assert dedicated.session.requests[0].chat_id == 926194553
    await dedicated.session.close()


async def test_fallback_to_main_bot_when_notify_bot_is_down(bot, session):  # noqa: ANN001
    """Токен есть, но бот не поднялся: молчать нельзя, шлём основным."""
    settings.notify_bot_token = "111111:NOTIFY_TOKEN"
    notify_bot.set_bot(None)
    notify_bot._problem = "Telegram не принял токен"

    sent = await notifications.notify_admins(bot, "🚀 Kometa запущена")

    assert sent == 1
    assert "Kometa запущена" in bot.session.all_text()


async def test_fallback_can_be_disabled(bot, session):  # noqa: ANN001
    """NOTIFY_FALLBACK_TO_MAIN=false: лучше не отправить, чем слать не тем ботом."""
    settings.notify_bot_token = "111111:NOTIFY_TOKEN"
    settings.notify_fallback_to_main = False
    notify_bot.set_bot(None)

    sent = await notifications.notify_admins(bot, "текст")

    assert sent == 0
    assert bot.session.all_text() == ""


async def test_without_separate_bot_everything_works_as_before(bot, session):  # noqa: ANN001
    """Старые настройки (только BOT_TOKEN) продолжают работать."""
    assert notify_bot.configured() is False

    sent = await notifications.notify_admins(bot, "тестовое уведомление")

    assert sent == 1
    assert "тестовое уведомление" in bot.session.all_text()


async def test_several_recipients_all_get_the_message(bot, session):  # noqa: ANN001
    """Уведомления можно увести в группу команды: получателей несколько."""
    settings.notify_bot_token = "111111:NOTIFY_TOKEN"
    settings.notify_chat_ids = "-1001234567890, 926194553"
    dedicated = dedicated_bot()
    notify_bot.set_bot(dedicated)

    sent = await notifications.notify_admins(bot, "оплата")

    assert sent == 2
    chats = [r.chat_id for r in dedicated.session.requests]
    assert chats == [-1001234567890, 926194553]
    await dedicated.session.close()


# ------------------------------------------------------------ разбор настроек
def test_notify_chat_ids_accept_groups(monkeypatch):  # noqa: ANN001
    settings.notify_chat_ids = "-1001234567890, 926194553; 926194553"
    assert settings.notify_chat_id_list == [-1001234567890, 926194553]

    settings.notify_chat_ids = ""
    settings.admin_ids = "1, 2"
    assert settings.notify_chat_id_list == [1, 2]
    settings.admin_ids = "1"


def test_buttons_namespace_follows_the_bot_that_sends():
    """Кнопки должны попасть к тому боту, который отправил сообщение."""
    assert notify_bot.namespace() == "admin"
    notify_bot.set_bot(dedicated_bot())
    assert notify_bot.namespace() == "adm"
    notify_bot.set_bot(None)


def test_order_keyboard_uses_namespace():
    rows = keyboards.admin_order_kb(7, namespace="adm").inline_keyboard
    assert [button.callback_data for row in rows for button in row] == [
        "adm:confirm:7",
        "adm:reject:7",
    ]


# --------------------------------------------------------------- содержимое
async def test_payment_notice_names_amount_client_and_day_total(bot, session):  # noqa: ANN001
    """В уведомлении об оплате видно, кто, сколько и сколько уже за день."""
    user, order, plan = await make_paid_order(session, tg_id=2114099310, amount=120)

    text = await notifications.payment_notice(
        session, order, user, provider="⭐ Звёзды", plan_title=plan.title
    )

    assert "120 ₽" in text
    assert "скидка 119 ₽" in text
    assert "zakon" in text and "2114099310" in text
    assert plan.title in text
    assert "Сегодня: 1 оплата на 120 ₽" in text


async def test_payment_notice_counts_stars_when_rubles_are_zero(session):  # noqa: ANN001
    """Звёздный заказ без рублёвой цены показываем в звёздах, а не «0 ₽»."""
    user, order, _ = await make_paid_order(session, tg_id=555, amount=0)
    order.stars_amount = 150
    await session.flush()

    text = await notifications.payment_notice(session, order, user)
    order_line = text.splitlines()[1]

    assert "150 ⭐" in order_line
    assert "0 ₽" not in order_line


async def test_payment_notice_shows_rubles_and_stars_together(session):  # noqa: ANN001
    """У звёздного заказа видно и рубли (сравнить с тарифом), и звёзды."""
    user, order, _ = await make_paid_order(session, tg_id=556, amount=120)
    order.stars_amount = 150
    await session.flush()

    text = await notifications.payment_notice(session, order, user)

    assert "120 ₽" in text and "150 ⭐" in text


async def test_daily_digest_has_real_numbers(session):  # noqa: ANN001
    """Сводка считается по базе: деньги, люди и инфраструктура на месте."""
    await make_paid_order(session, tg_id=101, amount=199)
    await session.commit()

    text = await digest.build_daily(session)

    assert "Kometa" in text
    assert "199 ₽" in text
    assert "Деньги" in text and "Люди" in text and "Инфраструктура" in text


async def test_digest_does_not_ask_admin_to_confirm_unpaid_invoices(session):  # noqa: ANN001
    """Неоплаченный счёт — не заявка админу: доступ выдастся сам.

    Раньше сводка писала «ждут подтверждения» про любой pending-заказ, и
    владелец шёл подтверждать то, что клиент ещё не оплатил.
    """
    user, _ = await subscriptions.get_or_create_user(session, tg_id=88077, username="waiting")
    plan = (await orders.list_plans(session))[0]
    await orders.create_order(session, user, plan, provider="platega_sbp")
    await session.commit()

    money = await digest.build_money(session)
    hint = await digest.build_hint(session)

    assert "Счета ждут оплаты клиентом: 1" in money
    assert "Ждут подтверждения" not in money
    assert "Требует внимания" not in hint, "неоплаченный счёт не требует рук админа"


async def test_digest_asks_admin_only_for_manual_transfers(session):  # noqa: ANN001
    """Ручной перевод по реквизитам — единственный случай, где нужен человек."""
    user, _ = await subscriptions.get_or_create_user(session, tg_id=88078, username="waiting")
    plan = (await orders.list_plans(session))[0]
    manual_order = await orders.create_order(session, user, plan, provider="manual")
    await orders.create_order(session, user, plan, provider="platega_sbp")
    await session.commit()

    money = await digest.build_money(session)
    hint = await digest.build_hint(session)

    assert f"Ждут подтверждения (перевод по реквизитам): <b>1</b>" in money
    assert "Счета ждут оплаты клиентом: 1" in money
    assert "подтвердить переводов: 1" in hint
    assert manual_order.id  # заказ существует, счётчик считает именно его


async def test_startup_notice_names_both_bots(session):  # noqa: ANN001
    """В стартовом сообщении видно, какой бот продаёт и какой уведомляет."""
    text = await digest.build_startup(
        session,
        sales_bot="kometavpnservise_bot",
        notify_username="@kometadcjcd99_bot",
    )

    assert "@kometavpnservise_bot" in text
    assert "@kometadcjcd99_bot" in text


async def test_error_notice_is_rate_limited(bot, session):  # noqa: ANN001
    """Одна и та же ошибка не должна превращаться в поток сообщений."""
    first = await notifications.notify_error(
        bot, where="обработка апдейта", exc=ValueError("boom"), user_id=42
    )
    second = await notifications.notify_error(
        bot, where="обработка апдейта", exc=ValueError("boom снова"), user_id=42
    )
    other = await notifications.notify_error(bot, where="фоновая задача", exc=ValueError("boom"))

    assert first is True
    assert second is False, "повтор в пределах паузы глушим"
    assert other is True, "другое место — другое сообщение"
    assert "Ошибка в боте" in bot.session.all_text()


# --------------------------------------- имена клиентов и разметка сообщений
class HtmlStrictSession(FakeSession):
    """Telegram, который бракует неразобранную разметку — как настоящий."""

    async def make_request(self, bot, method, timeout=None):  # noqa: ANN001
        if type(method).__name__ == "SendMessage" and getattr(method, "parse_mode", None) is not None:
            text = getattr(method, "text", "") or ""
            if "<oc" in text:  # имя клиента из живой базы: «^l<oc₽»
                raise TelegramBadRequest(
                    method=method,
                    message='Bad Request: can\'t parse entities: Unsupported start tag "oc₽"',
                )
        return await super().make_request(bot, method, timeout)


async def test_client_name_with_angle_brackets_is_escaped(session):  # noqa: ANN001
    """Имя вида «^l<oc₽» не должно ломать разметку уведомления."""
    user, order, _ = await make_paid_order(session, tg_id=777, amount=120)
    user.first_name = "^l<oc₽"
    await session.flush()

    text = await notifications.payment_notice(session, order, user)

    assert "&lt;oc₽" in text, "имя экранировано"
    assert "<oc₽" not in text, "сырого «<» в тексте быть не должно"


async def test_notification_survives_broken_markup(session):  # noqa: ANN001
    """Даже если разметка не разобралась, уведомление обязано дойти.

    Терять сообщение об оплате из-за чужого ника нельзя: вторым запросом
    уходит тот же текст без HTML.
    """
    bot = Bot(token=BOT_TOKEN, session=HtmlStrictSession())

    sent = await notifications.notify_admins(bot, "💰 Оплата от ^l<oc₽")

    assert sent == 1
    assert "^l<oc₽" in bot.session.all_text()
    await bot.session.close()


def test_plain_strips_our_tags():
    assert notifications.plain("💰 <b>Оплата</b> · <code>5</code>") == "💰 Оплата · 5"


# ---------------------------------------------------- команды служебного бота
async def test_notify_bot_status_command(bot, session):  # noqa: ANN001
    """Команда /status отвечает цифрами, а не молчит."""
    settings.admin_ids = "1"

    await notify_bot.build_dispatcher().feed_update(bot, make_update("/status", user_id=ADMIN_ID))

    assert "Выручка сегодня" in bot.session.all_text()


async def test_notify_bot_ignores_strangers(bot, session):  # noqa: ANN001
    """В сводке — выручка: посторонний в группе команды не должен её видеть."""
    settings.admin_ids = "1"

    await notify_bot.build_dispatcher().feed_update(bot, make_update("/status", user_id=999999))

    assert bot.session.all_text() == ""


async def test_notify_bot_confirm_button_issues_access(bot, session):  # noqa: ANN001
    """Кнопка «Подтвердить» из чата уведомлений выдаёт доступ, письмо — от бота продаж."""
    settings.admin_ids = "1"
    settings.notify_bot_token = "111111:NOTIFY_TOKEN"
    notify_bot.set_bot(dedicated_bot())
    notify_bot.set_customer_bot(bot)  # клиенту пишем основным ботом

    user, _ = await subscriptions.get_or_create_user(session, tg_id=88001, username="payer")
    plans = await orders.list_plans(session)
    plan = next(p for p in plans if p.code == "m1")
    order = Order(user_id=user.id, plan_id=plan.id, amount_rub=plan.price_rub, status="pending")
    session.add(order)
    await session.commit()

    await notify_bot.build_dispatcher().feed_update(
        bot, make_update(callback_data=f"adm:confirm:{order.id}", user_id=ADMIN_ID)
    )

    await session.refresh(order)
    assert order.status == "paid"
    assert order.confirmed_by == ADMIN_ID
    assert "подтвержд" in bot.session.all_text().lower(), "клиенту ушло письмо о выдаче доступа"
