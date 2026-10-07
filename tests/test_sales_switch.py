"""Тесты предохранителей: продажи и пробный доступ.

Смысл: пока оплата не подключена, бот не должен принимать деньги
(``SALES_ENABLED=false``), но при этом людей можно пускать на 3 дня бесплатно —
это отдельный флаг ``TRIAL_ENABLED``. Так устроен запуск Германии 06.10.2026.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import Order
from app.services import subscriptions
from tests.fakes import make_update

settings = get_settings()


@pytest.fixture
def sales_closed(monkeypatch):
    monkeypatch.setattr(settings, "sales_enabled", False)
    yield
    monkeypatch.setattr(settings, "sales_enabled", True)


async def _order_count(session) -> int:
    return await session.scalar(select(func.count()).select_from(Order))


async def test_plans_screen_shows_tariffs_even_when_closed(bot, dispatcher, session, sales_closed):
    """Тарифы видны всегда — банк и клиент должны понимать, сколько и за что платят."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=9901))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="plans", user_id=9901))

    sent = bot.session.all_text()
    assert "199" in sent  # цены на кнопках тарифов
    assert "ближайшие дни" in sent  # честно про сроки оплаты
    buttons = bot.session.buttons()
    assert any("1 месяц" in label for label in buttons)
    assert any("12 месяцев" in label for label in buttons)


async def test_closed_sales_full_path_plan_to_sbp_to_stub(bot, dispatcher, session, sales_closed):
    """Путь клиента при закрытых продажах: тариф → СБП → заглушка «скоро»."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=9910))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="plans", user_id=9910))
    assert "199" in bot.session.all_text()

    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data="plan:1", user_id=9910))
    card = bot.session.all_text()
    assert "1 месяц" in card
    assert "СБП" in card  # способ оплаты виден сразу после выбора тарифа

    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data="sbp:soon:1", user_id=9910))
    stub = bot.session.all_text()
    assert "Оплата по СБП — скоро" in stub
    assert "платить сейчас ничего не нужно" in stub
    assert await _order_count(session) == 0


async def test_pay_callback_does_not_create_order(bot, dispatcher, session, sales_closed):
    """Даже по старой кнопке деньги не принимаем: показываем заглушку, заказ не создаём."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=9902))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="pay:1:stars", user_id=9902))

    assert "Оплата по СБП — скоро" in bot.session.all_text()
    assert await _order_count(session) == 0


async def test_trial_works_while_payments_are_closed(bot, dispatcher, session, sales_closed, panel):
    """Пробный доступ и продажи — разные флаги.

    Боевой случай 06.10.2026: оплату ещё не подключили (SALES_ENABLED=false),
    но людей пускаем на 3 дня бесплатно (TRIAL_ENABLED=true). Пробный доступ
    обязан выдаваться, а платёжный путь — по-прежнему показывать заглушку.
    """
    await dispatcher.feed_update(bot, make_update("/start", user_id=9905))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="trial:start", user_id=9905))

    user = await subscriptions.get_user_by_tg(session, 9905)
    sub = await subscriptions.get_subscription(session, user.id)
    assert sub is not None, f"пробный доступ не выдан: {bot.session.all_text()}"
    assert sub.status == "trial"
    assert "/sub/" in bot.session.all_text()

    # А оплата при этом всё ещё закрыта: заказ не создаётся.
    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data="pay:1:stars", user_id=9905))
    assert "Оплата по СБП — скоро" in bot.session.all_text()
    assert await _order_count(session) == 0


async def test_trial_is_not_granted_when_trial_disabled(bot, dispatcher, session, monkeypatch):
    """Закрытый пробный доступ (TRIAL_ENABLED=false) — отдельная заглушка."""
    monkeypatch.setattr(settings, "trial_enabled", False)
    await dispatcher.feed_update(bot, make_update("/start", user_id=9906))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="trial:start", user_id=9906))

    sent = bot.session.all_text()
    assert "Пробный доступ ещё не открыт" in sent
    user = await subscriptions.get_user_by_tg(session, 9906)
    assert await subscriptions.get_subscription(session, user.id) is None


async def test_plans_visible_when_sales_open(bot, dispatcher, session, monkeypatch):
    monkeypatch.setattr(settings, "sales_enabled", True)
    await dispatcher.feed_update(bot, make_update("/start", user_id=9904))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="plans", user_id=9904))

    sent = bot.session.all_text()
    assert "199" in sent
    assert "Продажи ещё не открыты" not in sent
