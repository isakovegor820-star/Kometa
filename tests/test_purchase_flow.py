"""Сквозной тест денежного пути без сети:

/start → тарифы → выбор тарифа → счёт (ручная оплата) → «Я оплатил»
→ подтверждение админом → активная подписка + ссылка пользователю.
"""

from __future__ import annotations

import pytest

from app.panels.registry import registry
from app.services import orders, subscriptions
from tests.fakes import make_update

USER_ID = 8101
ADMIN_ID = 1


@pytest.fixture
async def started_user(bot, dispatcher, session):  # noqa: ANN001
    await dispatcher.feed_update(bot, make_update("/start", user_id=USER_ID))
    await dispatcher.feed_update(bot, make_update(callback_data="trial:start", user_id=USER_ID))
    bot.session.clear()
    user = await subscriptions.get_user_by_tg(session, USER_ID)
    assert user is not None
    return user


async def test_full_purchase_flow(bot, dispatcher, session, started_user):  # noqa: ANN001
    plans = await orders.list_plans(session)
    plan = next(p for p in plans if p.code == "m1")

    # 1. Тарифы показываются с ценами
    await dispatcher.feed_update(bot, make_update(callback_data="plans", user_id=USER_ID))
    plans_text = bot.session.all_text()
    assert str(plan.price_rub) in plans_text

    # 2. Карточка тарифа — предлагает способы оплаты
    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data=f"plan:{plan.id}", user_id=USER_ID))
    assert "оплат" in bot.session.all_text().lower()

    # 3. Счёт на ручную оплату
    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data=f"pay:{plan.id}:manual", user_id=USER_ID))
    invoice_text = bot.session.all_text()
    assert "Заказ" in invoice_text or "заказ" in invoice_text

    pending = await orders.pending_orders(session)
    assert len(pending) == 1
    order = pending[0]
    assert order.amount_rub == plan.price_rub
    assert order.status == "pending"

    # 4. «Я оплатил» — заявка уходит админам
    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data=f"order:manual:{order.id}", user_id=USER_ID))
    assert "проверк" in bot.session.all_text().lower()

    # 5. Админ подтверждает платёж
    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data=f"admin:confirm:{order.id}", user_id=ADMIN_ID))
    assert "подтвержд" in bot.session.all_text().lower()

    # 6. Проверяем результат в БД и в панели
    await session.refresh(order)
    assert order.status == "paid"
    assert order.confirmed_by == ADMIN_ID

    sub = await subscriptions.get_subscription(session, started_user.id)
    assert sub is not None
    assert sub.status == "active"
    assert sub.days_left >= 30

    panel = __import__("app.panels.registry", fromlist=["registry"]).registry.primary()
    panel_user = await panel.get_user(sub.panel_user_uuid)
    assert panel_user is not None and panel_user.enabled is True

    # 7. Пользователь получил ссылку-подписку
    assert f"/sub/{sub.subscription_token}" in bot.session.all_text()


async def test_admin_reject_cancels_order(bot, dispatcher, session, started_user):  # noqa: ANN001
    plans = await orders.list_plans(session)
    plan = next(p for p in plans if p.code == "m3")

    await dispatcher.feed_update(bot, make_update(callback_data=f"pay:{plan.id}:manual", user_id=USER_ID))
    order = (await orders.pending_orders(session))[0]

    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data=f"admin:reject:{order.id}", user_id=ADMIN_ID))

    await session.refresh(order)
    assert order.status == "canceled"

    sub = await subscriptions.get_subscription(session, started_user.id)
    assert sub is not None
    assert sub.status != "active"  # доступ не выдан


async def test_duplicate_confirmation_does_not_extend_twice(bot, dispatcher, session, started_user):  # noqa: ANN001
    plans = await orders.list_plans(session)
    plan = next(p for p in plans if p.code == "m1")

    await dispatcher.feed_update(bot, make_update(callback_data=f"pay:{plan.id}:manual", user_id=USER_ID))
    order = (await orders.pending_orders(session))[0]

    await dispatcher.feed_update(bot, make_update(callback_data=f"admin:confirm:{order.id}", user_id=ADMIN_ID))
    sub = await subscriptions.get_subscription(session, started_user.id)
    expires_after_first = sub.expires_at

    # повторное нажатие «Подтвердить» по тому же заказу
    await dispatcher.feed_update(bot, make_update(callback_data=f"admin:confirm:{order.id}", user_id=ADMIN_ID))

    await session.refresh(sub)
    assert sub.expires_at == expires_after_first


async def test_stats_command_shows_revenue(bot, dispatcher, session, started_user):  # noqa: ANN001
    plans = await orders.list_plans(session)
    plan = next(p for p in plans if p.code == "m1")
    await dispatcher.feed_update(bot, make_update(callback_data=f"pay:{plan.id}:manual", user_id=USER_ID))
    order = (await orders.pending_orders(session))[0]
    await dispatcher.feed_update(bot, make_update(callback_data=f"admin:confirm:{order.id}", user_id=ADMIN_ID))

    bot.session.clear()
    await dispatcher.feed_update(bot, make_update("/stats", user_id=ADMIN_ID))

    stats_text = bot.session.all_text()
    assert "Статистика" in stats_text
    assert str(plan.price_rub) in stats_text  # выручка за сегодня
