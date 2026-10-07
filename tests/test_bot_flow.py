"""Интеграционные тесты сценария бота: /start → пробный доступ → меню.

Проверяем реальную сборку Dispatcher + middleware + хендлеров, но без сети:
запросы к Telegram API перехватывает FakeSession (см. tests/fakes.py).
Фикстуры bot/dispatcher/session живут в tests/conftest.py.
"""

from __future__ import annotations

from app.panels.registry import registry
from app.services import subscriptions
from tests.fakes import make_update


async def test_start_creates_user_and_shows_menu(bot, dispatcher, session):
    await dispatcher.feed_update(bot, make_update("/start", user_id=7001))

    user = await subscriptions.get_user_by_tg(session, 7001)
    assert user is not None
    assert user.referral_code

    texts_sent = bot.session.all_text()
    assert "Kometa" in texts_sent
    assert "пробн" in texts_sent.lower()


async def test_start_with_referral_payload_links_users(bot, dispatcher, session):
    inviter, _ = await subscriptions.get_or_create_user(session, tg_id=7100, username="inviter")
    await session.commit()

    await dispatcher.feed_update(bot, make_update(f"/start ref_{inviter.referral_code}", user_id=7101))

    invited = await subscriptions.get_user_by_tg(session, 7101)
    assert invited is not None
    assert invited.referred_by == inviter.id
    assert "пригласил" in bot.session.all_text().lower()


async def test_trial_button_creates_subscription_and_gives_link(bot, dispatcher, session):
    await dispatcher.feed_update(bot, make_update("/start", user_id=7201))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="trial:start", user_id=7201))

    user = await subscriptions.get_user_by_tg(session, 7201)
    assert user is not None
    sub = await subscriptions.get_subscription(session, user.id)
    assert sub is not None and sub.status == "trial"
    assert sub.panel_user_uuid

    panel = registry.primary()
    panel_user = await panel.get_user(sub.panel_user_uuid)
    assert panel_user is not None and panel_user.enabled

    sent = bot.session.all_text()
    assert "/sub/" in sent  # ссылка-подписка ушла пользователю


async def test_trial_is_not_given_twice(bot, dispatcher, session):
    await dispatcher.feed_update(bot, make_update("/start", user_id=7301))
    await dispatcher.feed_update(bot, make_update(callback_data="trial:start", user_id=7301))
    await dispatcher.feed_update(bot, make_update(callback_data="trial:start", user_id=7301))

    sent = bot.session.all_text().lower()
    assert "уже использован" in sent


async def test_plans_are_listed_with_prices(bot, dispatcher, session):
    await dispatcher.feed_update(bot, make_update("/start", user_id=7401))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="plans", user_id=7401))

    sent = bot.session.all_text()
    assert "199" in sent
    # Цены в кнопках с разделителем разрядов: «1 590 ₽» (неразрывный пробел).
    assert "1\u00a0590" in sent


async def test_unknown_text_gets_helpful_answer(bot, dispatcher, session):
    await dispatcher.feed_update(bot, make_update("/start", user_id=7501))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update("привет, а как это работает?", user_id=7501))

    assert bot.session.texts(), "пользователь не должен оставаться без ответа"


async def test_howto_and_help_are_available(bot, dispatcher, session):
    await dispatcher.feed_update(bot, make_update("/start", user_id=7601))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="sub:howto", user_id=7601))
    assert "подключ" in bot.session.all_text().lower()

    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data="help", user_id=7601))
    assert "устройств" in bot.session.all_text().lower()


async def test_referral_screen_shows_personal_link(bot, dispatcher, session):
    await dispatcher.feed_update(bot, make_update("/start", user_id=7701))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="ref:show", user_id=7701))

    user = await subscriptions.get_user_by_tg(session, 7701)
    assert user is not None
    assert f"ref_{user.referral_code}" in bot.session.all_text()


async def test_help_points_to_status_page(bot, dispatcher, session):
    """Клиент должен знать, как проверить сервис, не заходя в Telegram."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=7801))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="help", user_id=7801))

    sent = bot.session.all_text()
    assert "/status" in sent
    # В публичных текстах нет формулировок про ограничения доступа и обход:
    # их вычищает tests/test_public_texts_clean.py, здесь — точечная проверка.
    assert "Wi-Fi" not in sent
