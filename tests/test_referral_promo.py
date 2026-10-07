"""Рефералка и промокоды: скидка на первую оплату и награда пригласившему.

Проверяем ровно то, что обещано клиенту в интерфейсе:
  * друг по ссылке платит половину;
  * скидка одна на аккаунт и только на первую оплату;
  * пригласивший получает +30 дней, даже если подписки у него ещё нет;
  * в звёздах цена тоже со скидкой, и Telegram подтверждает именно её;
  * накрутка ограничена: свой код не работает, лимит наград в месяц.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import Order, PromoCode, Referral, User
from app.services import orders, promo, referral, subscriptions
from tests.fakes import make_pre_checkout_update, make_stars_payment_update, make_update

settings = get_settings()


async def make_user(session, tg_id: int = 111) -> User:
    user, _ = await subscriptions.get_or_create_user(
        session, tg_id=tg_id, username=f"user{tg_id}", first_name=f"User{tg_id}"
    )
    await session.flush()
    return user


async def make_friend(session, referrer: User, tg_id: int) -> User:
    """Новый человек, пришедший по реферальной ссылке."""
    friend = await make_user(session, tg_id)
    attached = await referral.attach_referrer(session, friend, referrer.referral_code)
    assert attached is not None and attached.id == referrer.id
    return friend


async def first_plan(session):
    return (await orders.list_plans(session))[0]


def last_order_of(tg_id: int):
    return (
        select(Order)
        .join(User, User.id == Order.user_id)
        .where(User.tg_id == tg_id)
        .order_by(Order.id.desc())
        .limit(1)
    )


# ------------------------------------------------------------------ скидка
async def test_discount_math_is_whole_rubles_and_rounds_down():
    assert promo.calc_discount_rub(199, 50) == 99
    assert promo.calc_discount_rub(499, 50) == 249
    assert promo.calc_discount_rub(890, 50) == 445
    assert promo.calc_discount_rub(1590, 50) == 795
    assert promo.calc_discount_rub(1590, 50, max_rub=300) == 300
    assert promo.calc_discount_rub(199, 0) == 0


async def test_referral_link_gives_half_price_on_first_order(session):
    referrer = await make_user(session, 5001)
    friend = await make_friend(session, referrer, 5002)
    plan = await first_plan(session)

    order = await orders.create_order(session, friend, plan, provider="manual")

    assert order.base_amount_rub == plan.price_rub
    assert order.discount_rub == 99
    assert order.amount_rub == 100  # 199 ₽ − 99 ₽
    assert order.promo_code == promo.code_for_referral(referrer.referral_code)


async def test_admin_promo_code_applies_and_counts_uses(session, panel):
    user = await make_user(session, 5101)
    plan = await first_plan(session)

    code = PromoCode(code="LAUNCH30", kind="admin", percent=30, first_only=True, uses_limit=1)
    session.add(code)
    await session.flush()

    user.promo_code = "launch30"  # человек вводит код как угодно
    order = await orders.create_order(session, user, plan, provider="crypto")

    assert order.discount_rub == promo.calc_discount_rub(plan.price_rub, 30)
    assert order.promo_code == "LAUNCH30"

    await orders.mark_paid(session, order, panel)

    await session.refresh(code)
    assert code.uses_count == 1
    assert await promo.has_used_discount(session, user) is True


async def test_discount_is_given_only_once_per_account(session, panel):
    referrer = await make_user(session, 5201)
    friend = await make_friend(session, referrer, 5202)
    plan = await first_plan(session)

    first = await orders.create_order(session, friend, plan, provider="manual")
    await orders.mark_paid(session, first, panel)

    second = await orders.create_order(session, friend, plan, provider="manual")

    assert first.discount_rub == 99
    assert second.discount_rub == 0
    assert second.amount_rub == plan.price_rub


async def test_only_one_discounted_pending_order_at_a_time(session):
    referrer = await make_user(session, 5301)
    friend = await make_friend(session, referrer, 5302)
    plan = await first_plan(session)

    first = await orders.create_order(session, friend, plan, provider="manual")
    second = await orders.create_order(session, friend, plan, provider="crypto")

    assert first.status == "canceled"
    assert second.status == "pending" and second.discount_rub > 0


async def test_own_referral_code_is_rejected(session):
    user = await make_user(session, 5401)
    plan = await first_plan(session)

    code, reason = await promo.check_code(session, user, promo.code_for_referral(user.referral_code))

    assert code is None
    assert reason == promo.REASON_SELF
    order = await orders.create_order(session, user, plan, provider="manual")
    assert order.discount_rub == 0


async def test_manual_code_links_buyer_to_code_owner(session, panel):
    """Человек ввёл код руками, по ссылке не шёл — владелец всё равно в плюсе."""
    owner = await make_user(session, 5501)
    await subscriptions.start_trial(session, owner, panel)
    buyer = await make_user(session, 5502)
    plan = await first_plan(session)

    buyer.promo_code = promo.code_for_referral(owner.referral_code)
    order = await orders.create_order(session, buyer, plan, provider="manual")
    assert order.discount_rub == 99

    await orders.mark_paid(session, order, panel)

    assert buyer.referred_by == owner.id
    ref = await session.scalar(select(Referral).where(Referral.invited_id == buyer.id))
    assert ref is not None and ref.paid_order_id == order.id
    invited, paid = await referral.referral_stats(session, owner)
    assert (invited, paid) == (1, 1)


# ------------------------------------------------------------------ награда
async def test_referrer_gets_configured_days(session, panel):
    referrer = await make_user(session, 6001)
    referrer_sub, _ = await subscriptions.start_trial(session, referrer, panel)
    friend = await make_friend(session, referrer, 6002)
    plan = await first_plan(session)
    before = referrer_sub.expires_at

    order = await orders.create_order(session, friend, plan, provider="manual")
    await orders.mark_paid(session, order, panel)

    await session.refresh(referrer_sub)
    assert (referrer_sub.expires_at - before) >= timedelta(days=settings.referral_bonus_days_referrer - 1)


async def test_reward_is_not_lost_without_subscription(session, panel):
    """У пригласившего ещё нет подписки: дни копятся, а не пропадают."""
    referrer = await make_user(session, 6101)
    friend = await make_friend(session, referrer, 6102)
    plan = await first_plan(session)

    order = await orders.create_order(session, friend, plan, provider="manual")
    await orders.mark_paid(session, order, panel)

    assert referrer.bonus_days_balance == settings.referral_bonus_days_referrer

    # Подключается пробным доступом — накопленные дни добавляются к сроку
    sub, granted = await subscriptions.start_trial(session, referrer, panel)
    assert granted is True
    assert referrer.bonus_days_balance == 0
    assert sub.days_left >= settings.referral_bonus_days_referrer


async def test_reward_is_paid_once_per_friend(session, panel):
    referrer = await make_user(session, 6201)
    await subscriptions.start_trial(session, referrer, panel)
    friend = await make_friend(session, referrer, 6202)
    plan = await first_plan(session)

    first = await orders.create_order(session, friend, plan, provider="manual")
    await orders.mark_paid(session, first, panel)
    second = await orders.create_order(session, friend, plan, provider="manual")
    await orders.mark_paid(session, second, panel)

    invited, paid = await referral.referral_stats(session, referrer)
    assert (invited, paid) == (1, 1)


async def test_monthly_reward_limit_stops_accrual(session, panel):
    limit = settings.referral_max_rewards_per_month
    referrer = await make_user(session, 6301)
    await subscriptions.start_trial(session, referrer, panel)
    plan = await first_plan(session)

    for index in range(limit + 1):
        friend = await make_friend(session, referrer, 6310 + index)
        order = await orders.create_order(session, friend, plan, provider="manual")
        await orders.mark_paid(session, order, panel)

    rewarded = await session.scalar(
        select(func.count(Referral.id)).where(
            Referral.referrer_id == referrer.id, Referral.rewarded_at.is_not(None)
        )
    )
    assert rewarded == limit + 1  # разобрали все оплаты
    assert await referral.rewards_this_month(session, referrer.id) == limit  # а наградили — по лимиту


async def test_stats_show_friends_and_earned_days(session, panel):
    referrer = await make_user(session, 6401)
    await subscriptions.start_trial(session, referrer, panel)
    friend = await make_friend(session, referrer, 6402)
    silent = await make_friend(session, referrer, 6403)
    plan = await first_plan(session)

    order = await orders.create_order(session, friend, plan, provider="manual")
    await orders.mark_paid(session, order, panel)

    stats = await referral.overview(session, referrer)
    assert stats["invited"] == 2
    assert stats["paid"] == 1
    assert stats["earned_days"] == settings.referral_bonus_days_referrer

    friends = await referral.list_invited(session, referrer)
    by_id = {item.user.id: item for item in friends}
    assert by_id[friend.id].paid is True
    assert by_id[silent.id].paid is False


# ------------------------------------------------------------------ звёзды
async def test_stars_invoice_uses_discounted_price(session, panel, bot, dispatcher):
    referrer = await make_user(session, 6501)
    await make_friend(session, referrer, 6502)
    plan = await first_plan(session)

    await session.commit()  # отпускаем блокировку SQLite перед работой бота
    await dispatcher.feed_update(bot, make_update("/start", user_id=6502))
    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data=f"plan:{plan.id}", user_id=6502))
    assert "100 ₽" in bot.session.all_text()

    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data=f"pay:{plan.id}:stars", user_id=6502))

    order = await session.scalar(last_order_of(6502))
    assert order.discount_rub == 99
    assert order.stars_amount == plan.price_stars - round(plan.price_stars * 99 / plan.price_rub)

    bot.session.clear()
    await dispatcher.feed_update(bot, make_pre_checkout_update(order.id, order.stars_amount, user_id=6502))
    answers = [r for r in bot.session.requests if type(r).__name__ == "AnswerPreCheckoutQuery"]
    assert answers and answers[-1].ok is True

    await dispatcher.feed_update(bot, make_stars_payment_update(order.id, order.stars_amount, user_id=6502))
    await session.refresh(order)
    assert order.status == "paid"


async def test_pre_checkout_rejects_full_price_for_discounted_order(session, panel, bot, dispatcher):
    referrer = await make_user(session, 6601)
    friend = await make_friend(session, referrer, 6602)
    plan = await first_plan(session)

    order = await orders.create_order(session, friend, plan, provider="stars")
    await session.commit()
    bot.session.clear()
    # Прислали полную цену тарифа, хотя заказ со скидкой
    await dispatcher.feed_update(bot, make_pre_checkout_update(order.id, plan.price_stars, user_id=6602))
    answers = [r for r in bot.session.requests if type(r).__name__ == "AnswerPreCheckoutQuery"]
    assert answers and answers[-1].ok is False


# ------------------------------------------------------------------ экраны
async def test_referral_screen_explains_mechanics(session, bot, dispatcher):
    await session.commit()
    await dispatcher.feed_update(bot, make_update("/start", user_id=6701))
    user = await subscriptions.get_user_by_tg(session, 6701)
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="ref:show", user_id=6701))
    text = bot.session.all_text()

    assert f"ref_{user.referral_code}" in text
    assert promo.code_for_referral(user.referral_code) in text
    assert "Как это работает" in text
    assert "скидку 50%" in text


async def test_referral_greeting_shows_prices(session, bot, dispatcher):
    referrer = await make_user(session, 6801)
    await session.commit()
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(f"/start ref_{referrer.referral_code}", user_id=6802))
    text = bot.session.all_text()

    assert "Скидка 50% на первую оплату" in text
    assert "199 ₽" in text and "100 ₽" in text
    assert promo.code_for_referral(referrer.referral_code) in text


async def test_referrer_is_notified_about_reward(session, panel, bot, dispatcher):
    referrer = await make_user(session, 6901)
    await subscriptions.start_trial(session, referrer, panel)
    friend = await make_friend(session, referrer, 6902)
    plan = await first_plan(session)
    order = await orders.create_order(session, friend, plan, provider="stars")
    await session.commit()
    bot.session.clear()

    await dispatcher.feed_update(bot, make_stars_payment_update(order.id, order.stars_amount, user_id=6902))
    sent = " ".join(bot.session.texts())

    assert "Твой друг оплатил" in sent
    assert f"+{settings.referral_bonus_days_referrer} дней" in sent


async def test_promo_entry_by_button_and_by_text(session, bot, dispatcher):
    owner = await make_user(session, 7001)
    code = promo.code_for_referral(owner.referral_code)
    await session.commit()
    await dispatcher.feed_update(bot, make_update("/start", user_id=7002))

    # Кнопкой: бот просит прислать код
    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data="promo:enter", user_id=7002))
    assert "Промокод" in bot.session.all_text()

    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(code, user_id=7002))
    assert "принят" in bot.session.all_text()

    user = await subscriptions.get_user_by_tg(session, 7002)
    assert user.promo_code == code

    # Свободным текстом — тоже работает (второй человек)
    await dispatcher.feed_update(bot, make_update("/start", user_id=7003))
    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(code.lower(), user_id=7003))
    assert "принят" in bot.session.all_text()


async def test_unknown_text_is_not_treated_as_promo(session, bot, dispatcher):
    await session.commit()
    await dispatcher.feed_update(bot, make_update("/start", user_id=7101))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update("Happ", user_id=7101))

    assert "Не понял сообщение" in bot.session.all_text()


async def test_second_discount_is_explained(session, panel, bot, dispatcher):
    owner = await make_user(session, 7201)
    buyer = await make_user(session, 7202)
    plan = await first_plan(session)
    buyer.promo_code = promo.code_for_referral(owner.referral_code)
    order = await orders.create_order(session, buyer, plan, provider="manual")
    await orders.mark_paid(session, order, panel)
    await session.commit()

    await dispatcher.feed_update(bot, make_update("/start", user_id=7202))
    bot.session.clear()
    await dispatcher.feed_update(
        bot, make_update(promo.code_for_referral(owner.referral_code).lower(), user_id=7202)
    )

    assert "уже пользовался скидкой" in bot.session.all_text()


# ------------------------------------------------------------------ админка
async def test_admin_creates_and_lists_promo_codes(session, bot, dispatcher):
    await session.commit()

    await dispatcher.feed_update(bot, make_update("/promo NEWYEAR 40 5 30", user_id=1))
    created = await promo.get_by_code(session, "newyear")
    assert created is not None
    assert (created.kind, created.percent, created.uses_limit) == ("admin", 40, 5)
    assert created.expires_at is not None

    bot.session.clear()
    await dispatcher.feed_update(bot, make_update("/promos", user_id=1))
    assert "NEWYEAR" in bot.session.all_text()

    await dispatcher.feed_update(bot, make_update("/promo_off NEWYEAR", user_id=1))
    await session.refresh(created)
    assert created.is_active is False


async def test_admin_referral_dashboard_command(session, panel, bot, dispatcher):
    referrer = await make_user(session, 7401)
    await subscriptions.start_trial(session, referrer, panel)
    friend = await make_friend(session, referrer, 7402)
    plan = await first_plan(session)
    order = await orders.create_order(session, friend, plan, provider="manual")
    await orders.mark_paid(session, order, panel)
    await session.commit()

    bot.session.clear()
    await dispatcher.feed_update(bot, make_update("/referrals", user_id=1))
    text = bot.session.all_text()

    assert "Пришли по ссылкам" in text
    assert "Скидок активировано" in text
    assert "User7401" in text  # топ пригласивших


async def test_non_admin_cannot_create_promo(session, bot, dispatcher):
    await session.commit()

    await dispatcher.feed_update(bot, make_update("/promo HACK 90", user_id=999999))

    assert await promo.get_by_code(session, "HACK") is None


async def test_friend_message_mentions_bonus(session, panel, bot, dispatcher):
    """Друг должен видеть, что +3 дня — это подарок за приглашение."""
    referrer = await make_user(session, 7501)
    friend = await make_friend(session, referrer, 7502)
    plan = await first_plan(session)
    order = await orders.create_order(session, friend, plan, provider="stars")
    await session.commit()
    bot.session.clear()

    await dispatcher.feed_update(bot, make_stars_payment_update(order.id, order.stars_amount, user_id=7502))
    sent = " ".join(bot.session.texts())

    assert "Оплата получена" in sent
    assert f"+{settings.referral_bonus_days_invited} дня" in sent
