"""Тесты предохранителя продаж.

Смысл: пока нода не готова, бот не должен принимать деньги и выдавать
нерабочие конфиги. Выключается одной строкой `SALES_ENABLED=false`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import Order
from tests.fakes import make_update

settings = get_settings()


@pytest.fixture
def sales_closed(monkeypatch):
    monkeypatch.setattr(settings, "sales_enabled", False)
    yield
    monkeypatch.setattr(settings, "sales_enabled", True)


async def _order_count(session) -> int:
    return await session.scalar(select(func.count()).select_from(Order))


async def test_plans_screen_shows_closed_notice(bot, dispatcher, session, sales_closed):
    await dispatcher.feed_update(bot, make_update("/start", user_id=9901))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="plans", user_id=9901))

    sent = bot.session.all_text()
    assert "Продажи ещё не открыты" in sent
    assert "199" not in sent  # тарифы не показываем


async def test_pay_callback_does_not_create_order(bot, dispatcher, session, sales_closed):
    """Даже по старой кнопке деньги не принимаем."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=9902))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="pay:1:stars", user_id=9902))

    assert "Продажи ещё не открыты" in bot.session.all_text()
    assert await _order_count(session) == 0


async def test_trial_is_not_granted_when_closed(bot, dispatcher, session, sales_closed):
    await dispatcher.feed_update(bot, make_update("/start", user_id=9903))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="trial:start", user_id=9903))

    assert "Продажи ещё не открыты" in bot.session.all_text()


async def test_plans_visible_when_sales_open(bot, dispatcher, session, monkeypatch):
    monkeypatch.setattr(settings, "sales_enabled", True)
    await dispatcher.feed_update(bot, make_update("/start", user_id=9904))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="plans", user_id=9904))

    sent = bot.session.all_text()
    assert "199" in sent
    assert "Продажи ещё не открыты" not in sent
