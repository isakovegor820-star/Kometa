"""Источник привлечения и награда за продление друга.

Две вещи, без которых маркетинг нельзя измерить:

  * **метка источника** — откуда пришёл человек; ставится один раз и не
    перезаписывается, иначе каналы «перетягивают» заслугу друг у друга;
  * **награда за продление** — пригласивший получает дни не только за первую
    оплату друга, но и за каждое продление: рефералка работает на удержание,
    а не только на привлечение.
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import Order, Referral, User
from app.services import attribution, orders, referral, subscriptions
from tests.fakes import make_update

settings = get_settings()


async def make_user(session, tg_id: int = 111) -> User:
    user, _ = await subscriptions.get_or_create_user(
        session, tg_id=tg_id, username=f"user{tg_id}", first_name=f"User{tg_id}"
    )
    await session.flush()
    return user


async def first_plan(session):
    return (await orders.list_plans(session))[0]


# ------------------------------------------------------------------ разбор ссылки
def test_parse_payload_knows_all_link_types():
    assert attribution.parse_payload("ref_abc123") == ("ref", "abc123")
    assert attribution.parse_payload("src_yt_ivanov") == ("src", "yt_ivanov")
    assert attribution.parse_payload("gift_G7K2") == ("gift", "G7K2")
    assert attribution.parse_payload("promo_newyear") == ("promo", "newyear")
    assert attribution.parse_payload("sub_tok") == ("sub", "tok")


def test_parse_payload_without_marker_is_direct():
    """Пустой параметр — человек пришёл сам: это тоже источник, его видно в отчёте."""
    assert attribution.parse_payload("") == ("direct", "")
    assert attribution.parse_payload("   ") == ("direct", "")


def test_parse_payload_free_marker_counts_as_campaign():
    """Свободная метка без префикса — кампания: так удобнее ссылаться извне."""
    assert attribution.parse_payload("habr_post") == ("src", "habr_post")


def test_parse_payload_truncates_long_detail():
    """Длинная метка не должна ломать запись в колонку."""
    source, detail = attribution.parse_payload("src_" + "x" * 200)
    assert source == "src"
    assert len(detail) <= 64


# ------------------------------------------------------------------ запись источника
async def test_source_is_recorded_once_and_keeps_first_touch(session):
    user = await make_user(session, 3001)

    assert await attribution.record_source(session, user, "src_habr") == "src"
    assert user.source == "src"
    assert user.source_detail == "habr"
    assert user.source_at is not None

    # Второй заход по другой ссылке ничего не меняет: заслуга у первого канала.
    assert await attribution.record_source(session, user, "ref_xyz") == "src"
    assert user.source == "src"
    assert user.source_detail == "habr"


async def test_direct_source_is_upgraded_to_referral(session):
    """Сначала зашёл сам, потом по ссылке друга — засчитываем друга."""
    user = await make_user(session, 3002)
    await attribution.record_source(session, user, "")
    assert user.source == "direct"

    assert await attribution.record_source(session, user, "ref_friend01") == "ref"
    assert user.source == "ref"
    assert user.source_detail == "friend01"


async def test_detail_is_normalized(session):
    user = await make_user(session, 3003)
    await attribution.record_source(session, user, "src_  YouTube   Ivanov  ")
    assert user.source_detail == "youtube ivanov"


async def test_start_with_link_sets_source(session, bot, dispatcher):
    """Метка источника ставится прямо в /start — до гейта подписки на канал."""
    user = await make_user(session, 3004)
    await session.commit()

    await dispatcher.feed_update(bot, make_update("/start src_habr_article", user_id=3004))
    await session.refresh(user)

    assert user.source == "src"
    assert user.source_detail == "habr_article"


# ------------------------------------------------------------------ отчёт по каналам
async def test_source_stats_count_people_payers_and_revenue(session, panel):
    """Отчёт «канал → люди → оплаты → деньги»: основа расчёта CAC."""
    plan = await first_plan(session)

    paying = await make_user(session, 3101)
    await attribution.record_source(session, paying, "src_habr")
    order = await orders.create_order(session, paying, plan, provider="manual")
    await orders.mark_paid(session, order, panel)

    silent = await make_user(session, 3102)
    await attribution.record_source(session, silent, "src_habr")

    stats = {row.detail: row for row in await attribution.source_stats(session) if row.source == "src"}

    assert stats["habr"].joined == 2
    assert stats["habr"].payers == 1
    assert stats["habr"].revenue_rub == float(plan.price_rub)
    assert stats["habr"].conversion == 0.5
    assert stats["habr"].title.startswith("Размещение")


async def test_source_stats_use_spend_for_cac(session, panel):
    """Расходы привязываются к метке размещения — так считается CAC канала."""
    plan = await first_plan(session)
    for tg_id in (3201, 3202):
        user = await make_user(session, tg_id)
        await attribution.record_source(session, user, "src_yt_ivanov")
        order = await orders.create_order(session, user, plan, provider="manual")
        await orders.mark_paid(session, order, panel)

    rows = await attribution.source_stats(session, spend={"yt_ivanov": 1000})
    row = next(r for r in rows if r.detail == "yt_ivanov")

    assert row.payers == 2
    assert row.cac_rub == 500


async def test_source_summary_counts_unmarked_users(session):
    marked = await make_user(session, 3301)
    await attribution.record_source(session, marked, "ref_abc")
    await make_user(session, 3302)  # без метки

    summary = await attribution.source_summary(session)

    assert summary["total"] >= 2
    assert summary["unknown"] >= 1
    assert summary["referral"] >= 1


# ------------------------------------------------------------------ награда за продление
async def test_renewal_gives_referrer_days_again(session, panel):
    """Друг продлил подписку — пригласивший снова получает дни."""
    referrer = await make_user(session, 4001)
    await subscriptions.start_trial(session, referrer, panel)
    friend = await make_user(session, 4002)
    await referral.attach_referrer(session, friend, referrer.referral_code)
    plan = await first_plan(session)

    first = await orders.create_order(session, friend, plan, provider="manual")
    await orders.mark_paid(session, first, panel)
    sub = await subscriptions.get_subscription(session, referrer.id)
    await session.refresh(sub)
    after_first = sub.expires_at

    second = await orders.create_order(session, friend, plan, provider="manual")
    await orders.mark_paid(session, second, panel)
    await session.refresh(sub)

    assert sub.expires_at > after_first
    assert (sub.expires_at - after_first).days >= settings.referral_bonus_days_renewal - 1

    ref = await session.scalar(select(Referral).where(Referral.invited_id == friend.id))
    assert ref.renewals_count == 1
    assert ref.renewal_bonus_days == settings.referral_bonus_days_renewal
    assert ref.renewal_rewarded_at is not None


async def test_renewal_bonus_accumulates_per_payment(session, panel):
    """Каждое продление добавляет дни: счётчик и сумма растут."""
    referrer = await make_user(session, 4101)
    await subscriptions.start_trial(session, referrer, panel)
    friend = await make_user(session, 4102)
    await referral.attach_referrer(session, friend, referrer.referral_code)
    plan = await first_plan(session)

    for _ in range(3):
        order = await orders.create_order(session, friend, plan, provider="manual")
        await orders.mark_paid(session, order, panel)

    ref = await session.scalar(select(Referral).where(Referral.invited_id == friend.id))
    assert ref.renewals_count == 2  # первая оплата + два продления
    assert ref.renewal_bonus_days == 2 * settings.referral_bonus_days_renewal


async def test_first_payment_still_rewards_full_bonus(session, panel):
    """Первая оплата остаётся «полной» наградой, продление — отдельной."""
    referrer = await make_user(session, 4201)
    await subscriptions.start_trial(session, referrer, panel)
    friend = await make_user(session, 4202)
    await referral.attach_referrer(session, friend, referrer.referral_code)
    plan = await first_plan(session)

    order = await orders.create_order(session, friend, plan, provider="manual")
    await orders.mark_paid(session, order, panel)

    ref = await session.scalar(select(Referral).where(Referral.invited_id == friend.id))
    assert ref.bonus_days_referrer == settings.referral_bonus_days_referrer
    assert ref.renewal_bonus_days == 0
    assert ref.renewals_count == 0


async def test_renewal_without_referral_does_nothing(session, panel):
    """Обычный клиент без пригласившего: продление никого не награждает."""
    user = await make_user(session, 4301)
    plan = await first_plan(session)

    for _ in range(2):
        order = await orders.create_order(session, user, plan, provider="manual")
        await orders.mark_paid(session, order, panel)

    assert await session.scalar(select(func.count(Referral.id))) == 0


async def test_renewals_do_not_multiply_monthly_reward_counter(session, panel):
    """Месячный счётчик наград считает друзей, а не платежи.

    Иначе один друг, продлевающий подписку каждый месяц, исчерпал бы лимит
    наград за всех остальных: смысл лимита — ограничить число НОВЫХ людей,
    за которых получена награда, а не наказать за долгого клиента.
    """
    limit = settings.referral_max_rewards_per_month
    referrer = await make_user(session, 4401)
    friend = await make_user(session, 4402)
    await referral.attach_referrer(session, friend, referrer.referral_code)
    plan = await first_plan(session)

    for _ in range(limit):
        order = await orders.create_order(session, friend, plan, provider="manual")
        await orders.mark_paid(session, order, panel)

    assert await referral.rewards_this_month(session, referrer.id) == 1

    ref = await session.scalar(select(Referral).where(Referral.invited_id == friend.id))
    assert ref.renewals_count == limit - 1
    assert ref.renewal_bonus_days == (limit - 1) * settings.referral_bonus_days_renewal


async def test_monthly_limit_still_applies_when_inviting_many_friends(session, panel):
    """Лимит продолжает работать по числу приглашённых друзей."""
    limit = settings.referral_max_rewards_per_month
    referrer = await make_user(session, 4501)
    plan = await first_plan(session)

    for index in range(limit + 1):
        friend = await make_user(session, 4510 + index)
        await referral.attach_referrer(session, friend, referrer.referral_code)
        order = await orders.create_order(session, friend, plan, provider="manual")
        await orders.mark_paid(session, order, panel)

    assert await referral.rewards_this_month(session, referrer.id) == limit
