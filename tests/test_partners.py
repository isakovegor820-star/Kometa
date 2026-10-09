"""Партнёры: своя ссылка, свой промокод, свой процент и учёт выплат.

Проверяем то, что владелец видит в админке и на что полагается при расчётах:
  * ссылка ``?start=src_<код>`` закрепляет человека за партнёром один раз;
  * аудитория партнёра получает его скидку — и по ссылке, и по промокоду;
  * выплата начисляется на КАЖДУЮ оплату, а не только на первую;
  * долг = начислено минус выплачено, частичная выплата считается верно;
  * CAC и ROI канала считаются по выручке, выплате и скидкам.
"""

from __future__ import annotations

import pytest

from app.db.models import Partner, PromoCode, User
from app.services import orders, partners, promo, subscriptions
from tests.fakes import make_update

# ------------------------------------------------------------------ фикстуры
async def make_user(session, tg_id: int = 111) -> User:
    user, _ = await subscriptions.get_or_create_user(
        session, tg_id=tg_id, username=f"user{tg_id}", first_name=f"User{tg_id}"
    )
    await session.flush()
    return user


async def first_plan(session):
    return (await orders.list_plans(session))[0]


async def make_partner(session, **kwargs) -> Partner:
    params = {
        "name": "Иван, канал про удалёнку",
        "slug": "ivan-yt",
        "discount_percent": 20,
        "reward_kind": partners.REWARD_PERCENT,
        "reward_value": 30.0,
    }
    params.update(kwargs)
    return await partners.create_partner(session, **params)


async def pay_for(session, panel, user, *, times: int = 1):
    """Оплатить подписку N раз (первая оплата + продления)."""
    plan = await first_plan(session)
    result = []
    for _ in range(times):
        order = await orders.create_order(session, user, plan, provider="manual")
        await orders.mark_paid(session, order, panel)
        result.append(order)
    return result


# ------------------------------------------------------------------ создание
async def test_create_partner_gives_link_code_and_reward(session):
    partner = await make_partner(session)

    assert partner.slug == "ivan-yt"
    assert partner.reward_text == "30 % с платежей"
    assert partners.partner_link("kometa_bot", partner.slug) == "https://t.me/kometa_bot?start=src_ivan-yt"

    code = await partners.promo_for_partner(session, partner.id)
    assert code is not None
    assert code.code == "KOMETA-IVAN-YT"
    assert code.kind == "partner"
    assert code.percent == 20
    assert code.partner_id == partner.id


async def test_slug_and_code_are_normalized(session):
    """Человек вводит как удобно — ссылка всё равно получается рабочая."""
    partner = await make_partner(session, name="Петя", slug="Petя Channel!!", promo_code="pet")

    assert partner.slug == "pet-channel"
    code = await partners.promo_for_partner(session, partner.id)
    assert code.code == "KOMETA-PET"


async def test_duplicate_slug_is_rejected(session):
    await make_partner(session)
    with pytest.raises(partners.PartnerError) as exc:
        await make_partner(session, name="Другой", slug="ivan-yt")
    assert "уже есть" in str(exc.value)


async def test_duplicate_promo_code_is_rejected(session):
    await make_partner(session)
    with pytest.raises(partners.PartnerError) as exc:
        await make_partner(session, name="Второй", slug="second", promo_code="IVAN-YT")
    assert "занят" in str(exc.value)


@pytest.mark.parametrize(
    "kwargs, ожидание",
    (
        ({"name": "  "}, "имя партнёра"),
        ({"slug": "!"}, "Код ссылки"),
        ({"discount_percent": 150}, "от 0 до 100"),
        ({"reward_kind": "percent", "reward_value": 120}, "больше 100"),
        ({"reward_kind": "казна"}, "Выплата"),
    ),
)
async def test_bad_input_is_explained(session, kwargs, ожидание):
    """Ошибки в форме читает владелец — они должны быть понятными."""
    with pytest.raises(partners.PartnerError) as exc:
        await make_partner(session, **kwargs)
    assert ожидание in str(exc.value)


# ------------------------------------------------------------------ привязка
async def test_start_with_partner_link_attaches_user(session, bot, dispatcher):
    partner = await make_partner(session)
    await make_user(session, 7001)
    await session.commit()

    await dispatcher.feed_update(bot, make_update("/start src_ivan-yt", user_id=7001))

    user = await session.scalar(
        __import__("sqlalchemy").select(User).where(User.tg_id == 7001)
    )
    await session.refresh(user)
    assert user.partner_id == partner.id
    assert user.source == "src"
    assert user.source_detail == "ivan-yt"


async def test_unknown_partner_slug_is_not_attached(session, bot, dispatcher):
    await make_user(session, 7002)
    await session.commit()

    await dispatcher.feed_update(bot, make_update("/start src_kto-to-nesushchestvuyushchiy", user_id=7002))

    user = await session.scalar(
        __import__("sqlalchemy").select(User).where(User.tg_id == 7002)
    )
    assert user.partner_id is None


async def test_first_partner_keeps_credit(session):
    """Первый канал сохраняет заслугу: второй партнёр человека не забирает."""
    first = await make_partner(session)
    second = await make_partner(session, name="Второй", slug="second")
    user = await make_user(session, 7003)

    assert await partners.attach_partner(session, user, "ivan-yt") is not None
    assert await partners.attach_partner(session, user, "second") is None
    assert user.partner_id == first.id
    assert second.id != user.partner_id


async def test_deactivated_partner_does_not_accept_new_people(session):
    partner = await make_partner(session)
    partner.is_active = False
    await session.flush()
    user = await make_user(session, 7004)

    assert await partners.attach_partner(session, user, partner.slug) is None


# ------------------------------------------------------------------ скидка
async def test_partner_discount_applies_to_his_audience(session, panel):
    """Пришёл по партнёрской ссылке — скидка считается сама, код вводить не нужно."""
    partner = await make_partner(session, discount_percent=25)
    user = await make_user(session, 7101)
    await partners.attach_partner(session, user, partner.slug)
    plan = await first_plan(session)

    order = await orders.create_order(session, user, plan, provider="manual")

    expected = promo.calc_discount_rub(plan.price_rub, 25, 0)
    assert order.discount_rub == expected
    assert order.amount_rub == plan.price_rub - expected


async def test_partner_promo_code_works_when_sent_manually(session, panel):
    """Код можно дать текстом: «введи KOMETA-IVAN-YT» — скидка та же."""
    partner = await make_partner(session, discount_percent=15)
    user = await make_user(session, 7102)
    user.promo_code = "KOMETA-IVAN-YT"
    await session.flush()
    plan = await first_plan(session)

    order = await orders.create_order(session, user, plan, provider="manual")

    assert order.promo_code == "KOMETA-IVAN-YT"
    assert order.discount_rub == promo.calc_discount_rub(plan.price_rub, 15, 0)
    assert order.partner_id is None  # заслуга партнёра появляется только по ссылке


async def test_referral_discount_wins_over_partner(session):
    """Пришёл по ссылке друга, а потом по партнёрской — заслуга у друга."""
    from app.services import referral

    friend = await make_user(session, 7103)
    user = await make_user(session, 7104)
    await referral.attach_referrer(session, user, friend.referral_code)
    partner = await make_partner(session)

    # attach_partner не должен перебивать рефералку: человек уже закреплён.
    await partners.attach_partner(session, user, partner.slug)

    chosen = await promo.available(session, user)
    assert chosen is not None
    assert chosen.kind == "referral"


async def test_partner_without_discount_has_no_effect_on_price(session, panel):
    partner = await make_partner(session, discount_percent=0, reward_kind=partners.REWARD_FIXED, reward_value=50)
    user = await make_user(session, 7105)
    await partners.attach_partner(session, user, partner.slug)
    plan = await first_plan(session)

    order = await orders.create_order(session, user, plan, provider="manual")

    assert order.discount_rub == 0
    assert order.amount_rub == plan.price_rub


# ------------------------------------------------------------------ выплата
async def test_reward_is_percent_of_every_payment(session, panel):
    """Партнёр получает с каждой оплаты: ему выгодно приводить тех, кто остаётся."""
    partner = await make_partner(session, discount_percent=0, reward_kind=partners.REWARD_PERCENT, reward_value=30)
    user = await make_user(session, 7201)
    await partners.attach_partner(session, user, partner.slug)
    plan = await first_plan(session)

    paid = await pay_for(session, panel, user, times=3)
    await session.refresh(partner)
    stats = await partners.partner_stats(session, partner)

    revenue = sum(order.amount_rub for order in paid)
    assert stats.revenue_rub == revenue
    assert stats.reward_rub == pytest.approx(revenue * 0.30)
    assert stats.orders == 3
    assert stats.payers == 1
    # Снимок в заказе: история платежей не пересчитается при смене условий.
    assert all(order.partner_id == partner.id for order in paid)
    assert all(order.partner_reward_rub > 0 for order in paid)


async def test_reward_is_fixed_per_order(session, panel):
    partner = await make_partner(
        session, discount_percent=0, reward_kind=partners.REWARD_FIXED, reward_value=50
    )
    user = await make_user(session, 7202)
    await partners.attach_partner(session, user, partner.slug)

    await pay_for(session, panel, user, times=2)
    stats = await partners.partner_stats(session, partner)

    assert stats.orders == 2
    assert stats.reward_rub == pytest.approx(100)


async def test_no_reward_means_zero_debt(session, panel):
    partner = await make_partner(session, reward_kind=partners.REWARD_NONE)
    user = await make_user(session, 7203)
    await partners.attach_partner(session, user, partner.slug)

    await pay_for(session, panel, user)
    stats = await partners.partner_stats(session, partner)

    assert stats.reward_rub == 0
    assert stats.debt_rub == 0


async def test_ordinary_client_pays_no_reward(session, panel):
    """Человек без партнёра: выплат никому не начисляется."""
    await make_user(session, 7204)
    plan = await first_plan(session)
    user = await session.scalar(__import__("sqlalchemy").select(User).where(User.tg_id == 7204))
    order = await orders.create_order(session, user, plan, provider="manual")
    await orders.mark_paid(session, order, panel)

    assert order.partner_id is None
    assert order.partner_reward_rub == 0


async def test_deactivated_partner_is_not_paid_but_people_stay(session, panel):
    """Выключили партнёра — новые выплаты не идут, приведённые люди остаются."""
    partner = await make_partner(session)
    user = await make_user(session, 7205)
    await partners.attach_partner(session, user, partner.slug)
    partner.is_active = False
    await session.flush()

    order = (await pay_for(session, panel, user))[0]

    assert order.partner_reward_rub == 0
    assert user.partner_id == partner.id


# ------------------------------------------------------------------ долг и выплаты
async def test_partial_payout_leaves_the_rest_as_debt(session, panel):
    """Заплатили часть — остаток остаётся в долге, а не списывается."""
    partner = await make_partner(session, discount_percent=0, reward_value=50)
    user = await make_user(session, 7301)
    await partners.attach_partner(session, user, partner.slug)
    await pay_for(session, panel, user)

    stats = await partners.partner_stats(session, partner)
    assert stats.debt_rub == pytest.approx(stats.reward_rub)
    assert stats.reward_rub > 20  # иначе частичная выплата бессмысленна

    await partners.register_payout(session, partner, 20)
    stats_after = await partners.partner_stats(session, partner)

    assert stats_after.paid_out_rub == pytest.approx(20)
    assert stats_after.debt_rub == pytest.approx(stats_after.reward_rub - 20)


async def test_partial_payouts_accumulate(session, panel):
    partner = await make_partner(session, discount_percent=0, reward_value=30)
    user = await make_user(session, 7302)
    await partners.attach_partner(session, user, partner.slug)
    await pay_for(session, panel, user, times=2)

    await partners.register_payout(session, partner, 10)
    await partners.register_payout(session, partner, 15)
    stats = await partners.partner_stats(session, partner)

    assert stats.paid_out_rub == pytest.approx(25)
    assert partner.paid_out_at is not None


async def test_negative_payout_is_rejected(session):
    partner = await make_partner(session)
    with pytest.raises(partners.PartnerError):
        await partners.register_payout(session, partner, -5)


async def test_reward_zero_when_nothing_paid(session):
    """Партнёр без клиентов: долг нулевой, деления на ноль нет."""
    partner = await make_partner(session)
    stats = await partners.partner_stats(session, partner)

    assert stats.clicked == 0
    assert stats.payers == 0
    assert stats.debt_rub == 0
    assert stats.cac_rub == float("inf")
    assert stats.roi == 0.0


# ------------------------------------------------------------------ экономика канала
async def test_cac_and_roi_include_discounts(session, panel):
    """CAC канала = (выплата + скидки) ÷ платящие: скидка тоже наша стоимость."""
    partner = await make_partner(session, discount_percent=30, reward_kind=partners.REWARD_FIXED, reward_value=50)
    plan = await first_plan(session)
    for tg_id in (7401, 7402):
        user = await make_user(session, tg_id)
        await partners.attach_partner(session, user, partner.slug)
        order = await orders.create_order(session, user, plan, provider="manual")
        await orders.mark_paid(session, order, panel)

    stats = await partners.partner_stats(session, partner)
    discount_each = promo.calc_discount_rub(plan.price_rub, 30, 0)

    assert stats.payers == 2
    assert stats.discount_rub == discount_each * 2
    assert stats.reward_rub == pytest.approx(100)
    assert stats.total_cost_rub == pytest.approx(discount_each * 2 + 100)
    assert stats.cac_rub == pytest.approx((discount_each * 2 + 100) / 2)
    assert stats.roi == pytest.approx(stats.revenue_rub / stats.total_cost_rub)


async def test_totals_across_partners(session, panel):
    first = await make_partner(session, slug="first", reward_value=20)
    await make_partner(session, name="Второй", slug="second", reward_value=10)
    user = await make_user(session, 7501)
    await partners.attach_partner(session, user, "first")
    await pay_for(session, panel, user)

    stats = await partners.all_stats(session)
    totals = await partners.totals(stats)

    assert totals["clicked"] >= 1
    assert totals["payers"] >= 1
    assert totals["revenue_rub"] > 0
    assert totals["debt_rub"] == pytest.approx(sum(row.debt_rub for row in stats))
    assert any(row.partner.id == first.id for row in stats)


async def test_users_of_partner_lists_people_with_revenue(session, panel):
    partner = await make_partner(session)
    user = await make_user(session, 7502)
    await partners.attach_partner(session, user, partner.slug)
    paid = await pay_for(session, panel, user, times=2)

    people = await partners.users_of_partner(session, partner)

    assert len(people) == 1
    assert people[0]["user"].id == user.id
    assert people[0]["orders"] == 2
    assert people[0]["revenue_rub"] == sum(order.amount_rub for order in paid)


# ------------------------------------------------------------------ изменение условий
async def test_update_keeps_slug_and_syncs_code(session):
    """Ссылку менять нельзя (она уже разошлась), а скидка в коде должна совпадать."""
    partner = await make_partner(session, discount_percent=20)

    await partners.update_partner(session, partner, discount_percent=35, reward_value=45)
    code = await partners.promo_for_partner(session, partner.id)

    assert partner.slug == "ivan-yt"
    assert partner.discount_percent == 35
    assert partner.reward_value == 45
    assert code.percent == 35


async def test_deactivating_partner_disables_code(session):
    partner = await make_partner(session)
    await partners.update_partner(session, partner, is_active=False)

    code = await partners.promo_for_partner(session, partner.id)
    assert code.is_active is False
    assert await partners.get_by_slug(session, partner.slug) is None


async def test_partner_code_is_rejected_for_ownerless_checks(session):
    """Партнёрский код не путается с реферальным: у него нет владельца-клиента."""
    partner = await make_partner(session)
    code = await partners.promo_for_partner(session, partner.id)

    assert code.owner_user_id is None
    assert code.kind == "partner"
    assert isinstance(code, PromoCode)
    assert isinstance(partner, Partner)


# ------------------------------------------------------------------ сквозной путь
async def test_full_journey_link_then_purchase_then_payout(session, panel, bot, dispatcher):
    """Полный путь: ссылка → скидка в боте → оплата → долг партнёру → выплата.

    Это главный сценарий владельца: он даёт партнёру ссылку и хочет видеть,
    что деньги считаются правильно от первого клика до выплаты.
    """
    partner = await make_partner(session, discount_percent=20, reward_kind=partners.REWARD_PERCENT, reward_value=30)
    await make_user(session, 7701)
    plan = await first_plan(session)
    await session.commit()
    bot.session.clear()

    # 1. Человек приходит по партнёрской ссылке.
    await dispatcher.feed_update(bot, make_update("/start src_ivan-yt", user_id=7701))
    user = await session.scalar(__import__("sqlalchemy").select(User).where(User.tg_id == 7701))
    await session.refresh(user)
    assert user.partner_id == partner.id

    # 2. В боте видит тарифы уже со скидкой партнёра.
    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data="plans", user_id=7701))
    discount = promo.calc_discount_rub(plan.price_rub, 20, 0)
    assert f"{plan.price_rub - discount} ₽" in bot.session.all_text()

    # 3. Оплачивает — заказ получает скидку и партнёра.
    order = await orders.create_order(session, user, plan, provider="manual")
    assert order.discount_rub == discount
    await orders.mark_paid(session, order, panel)

    # 4. В админке появляется долг, равный 30 % от оплаченного.
    stats = await partners.partner_stats(session, partner)
    assert stats.clicked == 1
    assert stats.payers == 1
    assert stats.revenue_rub == order.amount_rub
    assert stats.debt_rub == pytest.approx(order.amount_rub * 0.30)

    # 5. Выплата закрывает долг.
    await partners.register_payout(session, partner, stats.debt_rub)
    after = await partners.partner_stats(session, partner)
    assert after.debt_rub == pytest.approx(0)
    assert after.paid_out_rub == pytest.approx(order.amount_rub * 0.30)
