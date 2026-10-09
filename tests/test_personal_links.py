"""Персональные ссылки: свой процент под конкретного человека.

Что проверяем — обещания, которые владелец даёт адресату:

  * «ссылка с 40 %» действительно даёт 40 % именно этой ссылке, не меняя общие правила;
  * скидка применяется сама: вводить код не нужно;
  * у ссылки есть срок и лимит активаций;
  * по каждой ссылке видно, сколько пришло, сколько заплатило и во что обошлась скидка;
  * если ссылка выдана под партнёра, ему ещё и начисляется выплата.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import PromoCode, User
from app.services import orders, partners, personal_links, promo, subscriptions
from tests.fakes import make_update


async def make_user(session, tg_id: int = 111) -> User:
    user, _ = await subscriptions.get_or_create_user(
        session, tg_id=tg_id, username=f"user{tg_id}", first_name=f"User{tg_id}"
    )
    await session.flush()
    return user


async def first_plan(session):
    return (await orders.list_plans(session))[0]


# ------------------------------------------------------------------ создание
async def test_link_gets_generated_code_and_promo_twin(session):
    """Код можно не придумывать: сгенерируем и выдадим код-двойник для ввода руками."""
    link = await personal_links.create_link(session, title="Сергей, коллега", discount_percent=40)

    assert len(link.code) == 7
    assert link.discount_percent == 40
    assert link.is_usable is True

    twin = await personal_links.promo_for_link(session, link.id)
    assert twin is not None
    assert twin.code == f"KOMETA-{link.code.upper()}"
    assert twin.kind == "personal"
    assert twin.percent == 40


async def test_manual_code_is_normalized(session):
    """Код задают как удобно: «Сергей 12.10» превращается в рабочую ссылку."""
    link = await personal_links.create_link(session, title="Пост", code="Sergey 12.10")
    assert link.code == "sergey-12-10"


async def test_duplicate_code_is_rejected(session):
    await personal_links.create_link(session, title="Первая", code="serega")
    with pytest.raises(personal_links.PersonalLinkError) as exc:
        await personal_links.create_link(session, title="Вторая", code="serega")
    assert "уже есть" in str(exc.value)


async def test_blank_title_is_rejected(session):
    with pytest.raises(personal_links.PersonalLinkError) as exc:
        await personal_links.create_link(session, title="   ")
    assert "название ссылки" in str(exc.value)


async def test_discount_bounds_are_checked(session):
    with pytest.raises(personal_links.PersonalLinkError):
        await personal_links.create_link(session, title="Много", discount_percent=150)
    with pytest.raises(personal_links.PersonalLinkError):
        await personal_links.create_link(session, title="Минус", discount_percent=-5)


async def test_expiry_is_set_from_days(session):
    link = await personal_links.create_link(session, title="Акция выходных", days=3)
    assert link.expires_at is not None
    delta = link.expires_at - datetime.now(timezone.utc)
    assert 2 <= delta.days <= 3
    assert link.is_expired is False
    assert link.is_usable is True


async def test_expired_link_is_not_usable(session):
    link = await personal_links.create_link(session, title="Старая", code="old")
    link.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    await session.flush()

    assert link.is_expired is True
    assert await personal_links.get_by_code(session, "old") is None


# ------------------------------------------------------------------ активация
async def test_start_with_personal_link_attaches_and_counts(session, bot, dispatcher):
    """Человек открыл именную ссылку: закрепился, активация засчиталась, он видит скидку."""
    link = await personal_links.create_link(
        session, title="Сергей, коллега", code="serega", discount_percent=40
    )
    await make_user(session, 8001)
    await session.commit()
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update("/start p_serega", user_id=8001))

    user = await session.scalar(__import__("sqlalchemy").select(User).where(User.tg_id == 8001))
    await session.refresh(user)
    await session.refresh(link)

    assert user.personal_link_id == link.id
    assert user.source == "personal"
    assert user.source_detail == "serega"
    assert link.uses_count == 1
    # Человек сразу видит, что условия особые.
    text = bot.session.all_text()
    assert "Сергей, коллега" in text
    assert "скидка 40%" in text


async def test_unknown_or_disabled_link_changes_nothing(session, bot, dispatcher):
    link = await personal_links.create_link(session, title="Выключенная", code="off-link")
    link.is_active = False
    await session.flush()
    await make_user(session, 8002)
    await session.commit()

    await dispatcher.feed_update(bot, make_update("/start p_off-link", user_id=8002))
    user = await session.scalar(__import__("sqlalchemy").select(User).where(User.tg_id == 8002))

    assert user.personal_link_id is None


async def test_link_is_attached_once(session):
    """Повторные заходы не съедают лимит активаций."""
    link = await personal_links.create_link(session, title="Лимит", code="limit20", uses_limit=20)
    user = await make_user(session, 8003)

    assert await personal_links.attach_link(session, user, link.code) is not None
    assert await personal_links.attach_link(session, user, link.code) is None
    await session.refresh(link)
    assert link.uses_count == 1


async def test_uses_limit_is_respected(session):
    link = await personal_links.create_link(session, title="Первым 2", code="first2", uses_limit=2)
    for tg in (8011, 8012):
        user = await make_user(session, tg)
        assert await personal_links.attach_link(session, user, link.code) is not None
    third = await make_user(session, 8013)

    assert await personal_links.attach_link(session, third, link.code) is None
    assert await personal_links.get_by_code(session, "first2") is None


async def test_link_under_partner_credits_partner(session, panel):
    """Ссылка выдана под партнёра — ему идёт и заслуга, и выплата."""
    partner = await partners.create_partner(
        session, name="Иван", slug="ivan-yt", discount_percent=0, reward_kind="percent", reward_value=30
    )
    link = await personal_links.create_link(
        session, title="Пост у Ивана 12.10", code="ivan-post1", partner_id=partner.id, discount_percent=40
    )
    user = await make_user(session, 8021)
    await personal_links.attach_link(session, user, link.code)
    plan = await first_plan(session)

    # Скидка — от ссылки (40 %), а не от партнёра (0 %).
    order = await orders.create_order(session, user, plan, provider="manual")
    assert order.discount_rub == promo.calc_discount_rub(plan.price_rub, 40, 0)
    await orders.mark_paid(session, order, panel)

    assert user.partner_id == partner.id
    stats = await partners.partner_stats(session, partner)
    assert stats.payers == 1
    assert stats.reward_rub == pytest.approx(order.amount_rub * 0.30)


# ------------------------------------------------------------------ скидка
async def test_discount_applies_without_entering_code(session, panel):
    """Главное обещание именной ссылки: скидка считается сама."""
    link = await personal_links.create_link(session, title="Особые условия", code="vip40", discount_percent=40)
    user = await make_user(session, 8101)
    await personal_links.attach_link(session, user, link.code)
    plan = await first_plan(session)

    order = await orders.create_order(session, user, plan, provider="manual")

    assert order.discount_rub == promo.calc_discount_rub(plan.price_rub, 40, 0)
    assert order.promo_code == f"KOMETA-{link.code.upper()}"


async def test_discount_survives_coming_back_without_link(session, panel):
    """Вернулся в бота без параметров — условия всё равно его."""
    link = await personal_links.create_link(session, title="Постоянные условия", code="keep30", discount_percent=30)
    user = await make_user(session, 8102)
    await personal_links.attach_link(session, user, link.code)
    plan = await first_plan(session)
    await session.flush()

    chosen = await promo.available(session, user)
    assert chosen is not None
    assert chosen.personal_link_id == link.id
    assert chosen.percent == 30


async def test_personal_discount_wins_over_partner_discount(session, panel):
    """Личная договорённость важнее общих условий канала."""
    partner = await partners.create_partner(session, name="К", slug="kanal", discount_percent=5)
    link = await personal_links.create_link(
        session, title="Личная", code="lucky50", partner_id=partner.id, discount_percent=50
    )
    user = await make_user(session, 8103)
    await personal_links.attach_link(session, user, link.code)
    plan = await first_plan(session)

    order = await orders.create_order(session, user, plan, provider="manual")

    assert order.discount_rub == promo.calc_discount_rub(plan.price_rub, 50, 0)


async def test_discount_cap_protects_long_plans(session, panel):
    """Потолок скидки не даёт отдать годовой тариф за полцены."""
    link = await personal_links.create_link(
        session, title="С потолком", code="cap240", discount_percent=50, discount_max_rub=240
    )
    user = await make_user(session, 8104)
    await personal_links.attach_link(session, user, link.code)
    plan = (await orders.list_plans(session))[-1]  # 12 месяцев

    order = await orders.create_order(session, user, plan, provider="manual")

    assert order.discount_rub == 240


async def test_manual_code_gives_same_conditions(session, panel):
    """Код-двойник даёт ту же скидку, что и ссылка."""
    link = await personal_links.create_link(session, title="Диктуют голосом", code="golos", discount_percent=25)
    user = await make_user(session, 8105)
    user.promo_code = f"KOMETA-{link.code.upper()}"
    await session.flush()
    plan = await first_plan(session)

    order = await orders.create_order(session, user, plan, provider="manual")

    assert order.discount_rub == promo.calc_discount_rub(plan.price_rub, 25, 0)


async def test_zero_discount_link_works(session, panel):
    """Ссылка без скидки — просто именной вход: видно, откуда пришёл человек."""
    link = await personal_links.create_link(session, title="Без скидки", code="nodisc", discount_percent=0)
    user = await make_user(session, 8106)
    await personal_links.attach_link(session, user, link.code)
    plan = await first_plan(session)

    order = await orders.create_order(session, user, plan, provider="manual")

    assert order.discount_rub == 0
    assert order.amount_rub == plan.price_rub


# ------------------------------------------------------------------ статистика
async def test_link_stats_count_people_payers_and_revenue(session, panel):
    link = await personal_links.create_link(session, title="Статистика", code="stat20", discount_percent=20)
    plan = await first_plan(session)
    discount_each = promo.calc_discount_rub(plan.price_rub, 20, 0)

    for tg_id in (8201, 8202):
        user = await make_user(session, tg_id)
        await personal_links.attach_link(session, user, link.code)
        order = await orders.create_order(session, user, plan, provider="manual")
        await orders.mark_paid(session, order, panel)

    # Третий только зашёл, но не оплатил.
    lurker = await make_user(session, 8203)
    await personal_links.attach_link(session, lurker, link.code)

    stats = await personal_links.link_stats(session, link)

    assert stats.joined == 3
    assert stats.payers == 2
    assert stats.orders == 2
    assert stats.conversion == pytest.approx(2 / 3)
    assert stats.discount_rub == discount_each * 2
    assert stats.cac_rub == pytest.approx(discount_each * 2 / 2)
    assert stats.roi == pytest.approx(stats.revenue_rub / stats.cost_rub)


async def test_empty_link_stats_are_safe(session):
    link = await personal_links.create_link(session, title="Пустая", code="empty1")
    stats = await personal_links.link_stats(session, link)

    assert stats.joined == 0
    assert stats.payers == 0
    assert stats.cac_rub == float("inf")
    assert stats.roi == 0.0


async def test_totals_across_links(session, panel):
    first = await personal_links.create_link(session, title="Первая", code="one", discount_percent=10)
    await personal_links.create_link(session, title="Вторая", code="two", discount_percent=10)
    user = await make_user(session, 8301)
    await personal_links.attach_link(session, user, first.code)
    plan = await first_plan(session)
    order = await orders.create_order(session, user, plan, provider="manual")
    await orders.mark_paid(session, order, panel)

    stats = await personal_links.all_link_stats(session)
    totals = await personal_links.link_totals(stats)

    assert totals["links"] == 2
    assert totals["joined"] == 1
    assert totals["payers"] == 1
    assert totals["revenue_rub"] > 0
    assert totals["discount_rub"] == pytest.approx(sum(row.discount_rub for row in stats))


async def test_users_of_link_lists_people(session, panel):
    link = await personal_links.create_link(session, title="Люди", code="people1")
    user = await make_user(session, 8302)
    await personal_links.attach_link(session, user, link.code)
    plan = await first_plan(session)
    order = await orders.create_order(session, user, plan, provider="manual")
    await orders.mark_paid(session, order, panel)

    people = await personal_links.users_of_link(session, link)

    assert len(people) == 1
    assert people[0]["user"].id == user.id
    assert people[0]["orders"] == 1


# ------------------------------------------------------------------ изменение
async def test_update_syncs_discount_to_twin_code(session):
    link = await personal_links.create_link(session, title="Меняем", code="change1", discount_percent=20)

    await personal_links.update_link(session, link, discount_percent=45, uses_limit=10)
    twin = await personal_links.promo_for_link(session, link.id)

    assert link.discount_percent == 45
    assert link.uses_limit == 10
    assert twin.percent == 45
    assert twin.uses_limit == 10


async def test_deactivating_link_disables_twin_code(session):
    link = await personal_links.create_link(session, title="Выключаем", code="turnoff")
    await personal_links.update_link(session, link, is_active=False)

    twin = await personal_links.promo_for_link(session, link.id)
    assert twin.is_active is False
    assert await personal_links.get_by_code(session, link.code) is None


async def test_code_cannot_be_changed(session):
    """Код не меняется: ссылку уже отправили человеку."""
    link = await personal_links.create_link(session, title="Постоянный код", code="fixed1")
    await personal_links.update_link(session, link, title="Другое название")
    assert link.code == "fixed1"
    assert link.title == "Другое название"


# ------------------------------------------------------------------ сквозной путь
async def test_full_journey_from_personal_link_to_payment(session, panel, bot, dispatcher):
    """Ссылка → скидка в боте → оплата → видно в статистике ссылки."""
    link = await personal_links.create_link(
        session, title="Сергей, коллега", code="serega40", discount_percent=40
    )
    await make_user(session, 8401)
    plan = await first_plan(session)
    await session.commit()
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update("/start p_serega40", user_id=8401))
    user = await session.scalar(__import__("sqlalchemy").select(User).where(User.tg_id == 8401))

    # 2. В тарифах цена уже со скидкой 40 %.
    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data="plans", user_id=8401))
    discounted = plan.price_rub - promo.calc_discount_rub(plan.price_rub, 40, 0)
    assert f"{discounted} ₽" in bot.session.all_text()

    # 3. Оплата — и в статистике ссылки есть человек, выручка и скидка.
    order = await orders.create_order(session, user, plan, provider="manual")
    await orders.mark_paid(session, order, panel)
    stats = await personal_links.link_stats(session, link)

    assert stats.joined == 1
    assert stats.payers == 1
    assert stats.revenue_rub == order.amount_rub
    assert stats.discount_rub == promo.calc_discount_rub(plan.price_rub, 40, 0)
    assert stats.cac_rub == float(stats.discount_rub)
