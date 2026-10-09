"""Тесты оплаты Telegram Stars: счёт → подтверждение → выдача доступа.

Ключевая деталь: без ответа на pre_checkout_query Telegram отменяет платёж,
поэтому этот путь проверяем отдельно.
"""

from __future__ import annotations

import pytest

from app.payments.registry import payments
from app.services import orders, subscriptions
from tests.fakes import make_pre_checkout_update, make_stars_payment_update, make_update

USER_ID = 9101


@pytest.fixture
async def stars_order(bot, dispatcher, session):  # noqa: ANN001
    """Пользователь дошёл до счёта в звёздах; возвращаем заказ и тариф."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=USER_ID))
    plans = await orders.list_plans(session)
    plan = next(p for p in plans if p.code == "m1")
    await dispatcher.feed_update(bot, make_update(callback_data=f"pay:{plan.id}:stars", user_id=USER_ID))
    pending = await orders.pending_orders(session)
    assert pending, "заказ не создан"
    return pending[0], plan


async def test_stars_provider_is_registered(bot):
    provider = payments.get("stars")
    assert provider is not None and provider.code == "stars"


async def test_plans_screen_shows_stars_price(bot, dispatcher, session):
    await dispatcher.feed_update(bot, make_update("/start", user_id=USER_ID))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="plans", user_id=USER_ID))

    plans = await orders.list_plans(session)
    monthly = next(p for p in plans if p.code == "m1")
    assert f"{monthly.price_stars} ⭐" in bot.session.all_text()


async def test_stars_invoice_created_for_order(bot, dispatcher, session, stars_order):
    order, plan = stars_order

    assert order.provider == "stars"
    assert order.status == "pending"

    # бот запросил у Telegram ссылку на счёт именно в звёздах
    links = bot.session.by_name("CreateInvoiceLink")
    assert links, "ссылка на счёт не создавалась"
    assert links[0].currency == "XTR"
    assert links[0].prices[0].amount == plan.price_stars

    assert "звёзд" in bot.session.all_text().lower() or "⭐" in bot.session.all_text()


async def test_pre_checkout_accepts_matching_amount(bot, dispatcher, session, stars_order):
    order, plan = stars_order
    bot.session.clear()

    await dispatcher.feed_update(bot, make_pre_checkout_update(order.id, plan.price_stars, user_id=USER_ID))

    answers = bot.session.by_name("AnswerPreCheckoutQuery")
    assert answers, "pre_checkout_query остался без ответа — Telegram отменил бы платёж"
    assert answers[0].ok is True


async def test_pre_checkout_rejects_wrong_amount(bot, dispatcher, session, stars_order):
    order, plan = stars_order
    bot.session.clear()

    await dispatcher.feed_update(bot, make_pre_checkout_update(order.id, 1, user_id=USER_ID))

    answers = bot.session.by_name("AnswerPreCheckoutQuery")
    assert answers and answers[0].ok is False


async def test_pre_checkout_rejects_unknown_order(bot, dispatcher, session):
    bot.session.clear()

    await dispatcher.feed_update(bot, make_pre_checkout_update(999_999, 180, user_id=USER_ID))

    answers = bot.session.by_name("AnswerPreCheckoutQuery")
    assert answers and answers[0].ok is False


async def test_successful_payment_activates_subscription(bot, dispatcher, session, stars_order):
    order, plan = stars_order
    user = await subscriptions.get_user_by_tg(session, USER_ID)
    assert user is not None

    await dispatcher.feed_update(bot, make_stars_payment_update(order.id, plan.price_stars, user_id=USER_ID))

    await session.refresh(order)
    assert order.status == "paid"
    assert order.paid_at is not None

    sub = await subscriptions.get_subscription(session, user.id)
    assert sub is not None
    assert sub.status == "active"
    assert sub.days_left >= 30

    # пользователь получил подтверждение и ссылку-подписку
    sent = bot.session.all_text()
    assert f"/sub/{sub.subscription_token}" in sent
    assert "Оплата получена" in sent


async def test_repeated_successful_payment_does_not_extend_twice(bot, dispatcher, session, stars_order):
    order, plan = stars_order
    user = await subscriptions.get_user_by_tg(session, USER_ID)

    await dispatcher.feed_update(bot, make_stars_payment_update(order.id, plan.price_stars, user_id=USER_ID))
    sub = await subscriptions.get_subscription(session, user.id)
    expires_after_first = sub.expires_at

    # Telegram может доставить апдейт повторно — второй раз дни не начисляем
    await dispatcher.feed_update(bot, make_stars_payment_update(order.id, plan.price_stars, user_id=USER_ID))

    await session.refresh(sub)
    assert sub.expires_at == expires_after_first


async def test_stars_prices_are_profitable(bot, dispatcher, session):
    """Цена в звёздах не должна быть меньше точки окупаемости.

    Telegram платит разработчику ~$0.013 за звезду (≈1.2 ₽ при 92 ₽/$),
    поэтому при цене в звёздах ниже ~0.84 от рублёвой мы работаем в убыток.
    """
    plans = await orders.list_plans(session)
    for plan in plans:
        assert plan.price_stars > 0, f"у тарифа {plan.code} не задана цена в звёздах"
        # 0.84 — минимально допустимое отношение (без запаса на вывод средств)
        assert plan.price_stars / plan.price_rub >= 0.84, (
            f"{plan.code}: {plan.price_stars} ⭐ за {plan.price_rub} ₽ — слишком дёшево"
        )


async def test_large_stars_payment_alerts_admin(bot, dispatcher, session):
    """Крупная покупка звёздами — сигнал админу: дешёвые звёзды бывают крадеными."""
    user_id = 9201
    await dispatcher.feed_update(bot, make_update("/start", user_id=user_id))
    plans = await orders.list_plans(session)
    annual = next(p for p in plans if p.code == "m12")

    await dispatcher.feed_update(bot, make_update(callback_data=f"pay:{annual.id}:stars", user_id=user_id))
    order = (await orders.pending_orders(session))[0]

    await dispatcher.feed_update(
        bot, make_stars_payment_update(order.id, annual.price_stars, user_id=user_id)
    )

    admin_alerts = [
        request.text
        for request in bot.session.by_name("SendMessage")
        if getattr(request, "chat_id", None) == 1 and "Крупная оплата звёздами" in (request.text or "")
    ]
    assert admin_alerts, "админ не получил предупреждение о крупной оплате звёздами"
    assert str(annual.price_stars) in admin_alerts[0]


async def test_small_stars_payment_does_not_alert_admin(bot, dispatcher, session):
    """Обычная покупка на месяц не должна спамить админа предупреждениями."""
    user_id = 9202
    await dispatcher.feed_update(bot, make_update("/start", user_id=user_id))
    plans = await orders.list_plans(session)
    monthly = next(p for p in plans if p.code == "m1")

    await dispatcher.feed_update(bot, make_update(callback_data=f"pay:{monthly.id}:stars", user_id=user_id))
    order = (await orders.pending_orders(session))[0]
    bot.session.clear()

    await dispatcher.feed_update(
        bot, make_stars_payment_update(order.id, monthly.price_stars, user_id=user_id)
    )

    warnings = [
        request.text
        for request in bot.session.by_name("SendMessage")
        if "Крупная оплата звёздами" in (request.text or "")
    ]
    assert not warnings


async def test_reseller_button_appears_when_configured(bot, dispatcher, session, monkeypatch):
    """Если задан бот-посредник, клиент видит кнопку «Купить N звёзд» прямо в счёте."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "stars_reseller_url", "https://t.me/kupits_zvezdyy_bot")

    user_id = 9301
    await dispatcher.feed_update(bot, make_update("/start", user_id=user_id))
    plans = await orders.list_plans(session)
    monthly = next(p for p in plans if p.code == "m1")
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data=f"pay:{monthly.id}:stars", user_id=user_id))

    buttons = bot.session.buttons()
    assert any("Купить" in button and "звёзд" in button for button in buttons)
    assert "Не хватает звёзд" in bot.session.all_text()
    assert "https://t.me/kupits_zvezdyy_bot" in bot.session.button_urls()


async def test_reseller_button_hidden_when_not_configured(bot, dispatcher, session, monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "stars_reseller_url", "")

    user_id = 9302
    await dispatcher.feed_update(bot, make_update("/start", user_id=user_id))
    plans = await orders.list_plans(session)
    monthly = next(p for p in plans if p.code == "m1")
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data=f"pay:{monthly.id}:stars", user_id=user_id))

    assert not any("Купить" in button for button in bot.session.buttons())
    assert "Не хватает звёзд" not in bot.session.all_text()
