"""Сквозной сценарий реферальной ссылки: пригласил → друг оплатил → дни пришли.

Что проверяем снаружи (через диспетчер, как это делает живой бот):

1. ссылка ведёт на правильного бота и содержит код человека;
2. на экране «Пригласить друга» есть готовое сообщение другу и кнопка
   «Поделиться» через ``t.me/share/url`` (права бота для неё не нужны);
3. новый человек по ссылке привязывается ровно один раз, оплачивает со скидкой,
   и пригласивший получает дни — в срок подписки или в накопительный баланс;
4. повторный ``/start`` по той же ссылке ничего не начисляет;
5. свою ссылку использовать нельзя, заблокированный пригласивший не привязывается;
6. гейт подписки на канал не съедает привязку: человек нажал ``/start`` по ссылке
   до подписки на канал — пригласивший уже записан.

Логика наград живёт в app/services/referral.py, скидка — в app/services/promo.py;
здесь мы проверяем, что она действительно доходит до клиента.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import quote

import pytest
from sqlalchemy import func, select

from app.bot import texts
from app.config import get_settings
from app.db.models import Order, Referral
from app.services import channel_gate, orders, promo, referral, subscriptions
from tests.fakes import make_pre_checkout_update, make_stars_payment_update, make_update

#: Тот же объект настроек, что держат модули приложения: они читают его один раз
#: при импорте, поэтому подменять значения нужно именно здесь.
settings = get_settings()

CHANNEL = "@kometa_test"
GATE_MARK = "Остался один шаг"


@pytest.fixture
def gate_on(monkeypatch):
    """Включённый гейт подписки на канал: в тестах он выключен (tests/conftest.py)."""
    monkeypatch.setattr(settings, "channel_gate_enabled", True)
    monkeypatch.setattr(settings, "channel_id", CHANNEL)
    monkeypatch.setattr(settings, "channel_url", f"https://t.me/{CHANNEL.lstrip('@')}")
    monkeypatch.setattr(settings, "channel_gate_cache_hours", 12)
    monkeypatch.setattr(settings, "channel_gate_fail_open", True)
    monkeypatch.setattr(channel_gate, "_alerted_at", 0.0)
    monkeypatch.setattr(channel_gate, "_not_subscribed_at", {})
    monkeypatch.setattr(channel_gate, "_unavailable_until", 0.0)
    return settings


async def make_user(session, tg_id: int):  # noqa: ANN001 - тестовый помощник
    user, _ = await subscriptions.get_or_create_user(
        session, tg_id=tg_id, username=f"user{tg_id}", first_name=f"User{tg_id}"
    )
    await session.flush()
    return user


def referral_url(username: str, code: str) -> str:
    return f"https://t.me/{username}?start=ref_{code}"


# ------------------------------------------------------------------ ссылка и экран
async def test_referral_link_points_to_the_right_bot_and_code(session, bot, dispatcher):
    await dispatcher.feed_update(bot, make_update("/start", user_id=9501))
    user = await subscriptions.get_user_by_tg(session, 9501)
    me = await bot.get_me()
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="ref:show", user_id=9501))

    link = referral_url(me.username, user.referral_code)
    assert link in bot.session.all_text()
    assert f"<code>{link}</code>" in " ".join(bot.session.texts())


async def test_share_button_sends_ready_message(session, bot, dispatcher):
    """Кнопка «Поделиться» отдаёт готовый текст и ссылку — сочинять не нужно."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=9502))
    user = await subscriptions.get_user_by_tg(session, 9502)
    me = await bot.get_me()
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="ref:show", user_id=9502))

    share_text = texts.REFERRAL_SHARE_TEXT.format(percent=settings.referral_discount_percent)
    link = referral_url(me.username, user.referral_code)
    share_urls = [
        url for url in bot.session.button_urls() if url.startswith("https://t.me/share/url?")
    ]

    assert share_urls, "нет кнопки «Поделиться с другом»"
    assert quote(link, safe="") in share_urls[0]
    assert quote(share_text, safe="") in share_urls[0]
    # Тот же готовый текст видно на экране — можно скопировать и отправить руками.
    shown = bot.session.all_text()
    assert share_text in shown
    assert "Готовое сообщение другу" in shown
    # Счётчики и условия на месте: человек видит, что и когда получит.
    assert "Перешли по ссылке" in shown
    assert f"скидку {settings.referral_discount_percent}%" in shown
    assert f"+{settings.referral_bonus_days_referrer} дней" in shown


# ------------------------------------------------------------------ сквозной путь
async def test_new_person_by_link_pays_and_referrer_gets_days(session, panel, bot, dispatcher):
    """Главный сценарий: пришёл по ссылке → оплатил → пригласивший получил дни."""
    referrer = await make_user(session, 9301)
    sub, granted = await subscriptions.start_trial(session, referrer, panel)
    assert granted is True
    before = sub.expires_at
    await session.commit()

    # 1. Друг приходит по ссылке: привязка и приветствие со скидкой.
    await dispatcher.feed_update(bot, make_update(f"/start ref_{referrer.referral_code}", user_id=9302))
    invited = await subscriptions.get_user_by_tg(session, 9302)
    assert invited is not None
    assert invited.referred_by == referrer.id
    assert "пригласил" in bot.session.all_text().lower()

    # 2. Выбирает тариф и платит звёздами: цена уже со скидкой за приглашение.
    plan = (await orders.list_plans(session))[0]
    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data=f"plan:{plan.id}", user_id=9302))
    await dispatcher.feed_update(bot, make_update(callback_data=f"pay:{plan.id}:stars", user_id=9302))

    order = await session.scalar(
        select(Order).where(Order.user_id == invited.id).order_by(Order.id.desc()).limit(1)
    )
    assert order is not None
    # Скидку сверяем с кодом пригласившего в БД: это то, что реально применилось.
    code_row = await promo.ensure_referral_code(session, referrer)
    assert order.discount_rub == promo.calc_discount_rub(
        plan.price_rub, code_row.percent, code_row.max_discount_rub
    )
    assert order.stars_amount > 0

    await dispatcher.feed_update(
        bot, make_pre_checkout_update(order.id, order.stars_amount, user_id=9302)
    )
    await dispatcher.feed_update(
        bot, make_stars_payment_update(order.id, order.stars_amount, user_id=9302)
    )
    await session.refresh(order)
    assert order.status == "paid"

    # 3. Пригласивший получил дни — прямо в срок своей подписки.
    ref = await session.scalar(select(Referral).where(Referral.invited_id == invited.id))
    assert ref is not None and ref.paid_order_id == order.id
    rewarded_days = int(ref.bonus_days_referrer)
    assert rewarded_days > 0

    await session.refresh(sub)
    gained = sub.expires_at - before
    assert gained >= timedelta(days=rewarded_days - 1)
    assert gained < timedelta(days=rewarded_days + 1)

    stats = await referral.overview(session, referrer)
    assert (stats["invited"], stats["paid"]) == (1, 1)
    assert stats["earned_days"] == rewarded_days
    assert await referral.rewards_this_month(session, referrer.id) == 1

    # 4. Друг видит свой бонус за то, что пришёл по ссылке.
    assert f"+{ref.bonus_days_invited} дня" in " ".join(bot.session.texts())


async def test_reward_goes_to_balance_when_referrer_has_no_subscription(session, panel, bot, dispatcher):
    """У пригласившего нет подписки: дни не теряются, а копятся в запасе."""
    referrer = await make_user(session, 9311)
    await session.commit()

    await dispatcher.feed_update(bot, make_update(f"/start ref_{referrer.referral_code}", user_id=9312))
    invited = await subscriptions.get_user_by_tg(session, 9312)
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, invited, plan, provider="manual")
    await orders.mark_paid(session, order, panel)

    ref = await session.scalar(select(Referral).where(Referral.invited_id == invited.id))
    assert ref is not None and ref.bonus_days_referrer > 0

    await session.refresh(referrer)
    assert referrer.bonus_days_balance == ref.bonus_days_referrer
    stats = await referral.overview(session, referrer)
    assert stats["balance"] == ref.bonus_days_referrer


async def test_repeat_start_by_same_link_does_not_reward_twice(session, panel, bot, dispatcher):
    """Второй /start по той же ссылке ничего не добавляет: привязка одна."""
    referrer = await make_user(session, 9321)
    sub, _granted = await subscriptions.start_trial(session, referrer, panel)
    before = sub.expires_at
    await session.commit()

    await dispatcher.feed_update(bot, make_update(f"/start ref_{referrer.referral_code}", user_id=9322))
    await dispatcher.feed_update(bot, make_update(f"/start ref_{referrer.referral_code}", user_id=9322))

    invited = await subscriptions.get_user_by_tg(session, 9322)
    assert invited.referred_by == referrer.id
    attached = await session.scalar(
        select(func.count(Referral.id)).where(Referral.referrer_id == referrer.id)
    )
    assert attached == 1, "привязка должна случиться ровно один раз"
    assert bot.session.all_text().lower().count("пригласил") == 1, "приветствие шлём один раз"

    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, invited, plan, provider="manual")
    await orders.mark_paid(session, order, panel)

    ref = await session.scalar(select(Referral).where(Referral.invited_id == invited.id))
    assert ref is not None and ref.bonus_days_referrer > 0

    await session.refresh(sub)
    gained = sub.expires_at - before
    assert gained >= timedelta(days=ref.bonus_days_referrer - 1)
    assert gained < timedelta(days=ref.bonus_days_referrer + 1), "награда одна, не двойная"
    assert await referral.rewards_this_month(session, referrer.id) == 1


# ------------------------------------------------------------------ защита от накрутки
async def test_self_referral_is_ignored(session, bot, dispatcher):
    """Своя ссылка не работает: ни привязки, ни награды."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=9331))
    user = await subscriptions.get_user_by_tg(session, 9331)
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(f"/start ref_{user.referral_code}", user_id=9331))

    await session.refresh(user)
    assert user.referred_by is None
    assert await session.scalar(select(func.count(Referral.id))) == 0
    assert "пригласил" not in bot.session.all_text().lower()


async def test_blocked_referrer_is_not_attached(session, bot, dispatcher):
    """Заблокированный пригласивший не привязывается: награду ему не начисляем."""
    referrer = await make_user(session, 9341)
    referrer.is_blocked = True
    await session.commit()

    await dispatcher.feed_update(bot, make_update(f"/start ref_{referrer.referral_code}", user_id=9342))

    invited = await subscriptions.get_user_by_tg(session, 9342)
    assert invited is not None
    assert invited.referred_by is None
    assert await session.scalar(select(func.count(Referral.id))) == 0


async def test_unknown_referral_code_is_ignored(session, bot, dispatcher):
    """Битая ссылка не ломает /start: человек просто попадает в меню."""
    await dispatcher.feed_update(bot, make_update("/start ref_NOPE1234", user_id=9351))

    user = await subscriptions.get_user_by_tg(session, 9351)
    assert user is not None and user.referred_by is None
    assert "Kometa" in bot.session.all_text()


# ------------------------------------------------------------------ гейт канала
async def test_channel_gate_does_not_eat_the_attachment(session, bot, dispatcher, gate_on):
    """Человек нажал /start по ссылке, но ещё не подписан на канал.

    Привязка обязана случиться до гейта: иначе друг не увидит обещанную скидку,
    а пригласивший — награду, хотя ссылку он уже разослал.
    """
    referrer = await make_user(session, 9361)
    await session.commit()
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(f"/start ref_{referrer.referral_code}", user_id=9362))

    invited = await subscriptions.get_user_by_tg(session, 9362)
    assert invited is not None
    assert invited.referred_by == referrer.id, "гейт съел привязку"
    assert bot.session.by_name("GetChatMember"), "гейт даже не проверили"
    assert GATE_MARK in bot.session.all_text()

    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, invited, plan, provider="manual")
    assert order.discount_rub > 0, "скидка другу должна быть уже привязана"
