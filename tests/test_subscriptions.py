"""Тесты ядра: пробный доступ, покупка, продление, истечение, рефералка."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Subscription, User
from app.services import orders, referral, subscriptions


async def make_user(session, tg_id: int = 111) -> User:
    user, _ = await subscriptions.get_or_create_user(
        session, tg_id=tg_id, username=f"user{tg_id}", first_name=f"User{tg_id}"
    )
    await session.flush()
    return user


async def test_user_creation_is_idempotent_and_code_unique(session):
    first, created_first = await subscriptions.get_or_create_user(session, tg_id=1001, username="a")
    second, created_second = await subscriptions.get_or_create_user(session, tg_id=1001, username="b")
    await session.flush()

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert second.username == "b"  # актуальный username обновляется
    assert first.referral_code and len(first.referral_code) >= 6


async def test_trial_creates_panel_user_and_subscription(session, panel):
    user = await make_user(session, 2001)
    sub, granted = await subscriptions.start_trial(session, user, panel)

    assert granted is True
    assert sub.status == "trial"
    assert sub.panel_user_uuid
    assert sub.subscription_token
    assert 2 <= sub.days_left <= 3
    assert sub.devices_limit == 1

    panel_user = await panel.get_user(sub.panel_user_uuid)
    assert panel_user is not None
    assert panel_user.enabled is True
    assert panel_user.traffic_limit_bytes == 10 * 1024**3


async def test_trial_is_given_only_once(session, panel):
    user = await make_user(session, 2002)
    await subscriptions.start_trial(session, user, panel)
    sub, granted = await subscriptions.start_trial(session, user, panel)

    assert granted is False
    assert sub.status == "trial"


async def test_purchase_activates_subscription(session, panel):
    user = await make_user(session, 2003)
    await subscriptions.start_trial(session, user, panel)
    plan = (await orders.list_plans(session))[0]

    order = await orders.create_order(session, user, plan, provider="manual")
    sub, already_paid = await orders.mark_paid(session, order, panel)

    assert already_paid is False
    assert order.status == "paid"
    assert sub is not None and sub.status == "active"
    assert sub.plan_id == plan.id
    assert sub.days_left >= 30  # 30 дней тарифа + остаток триала
    assert order.kind == "renew"  # триал уже был — это продление


async def test_mark_paid_is_idempotent(session, panel):
    user = await make_user(session, 2004)
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="manual")

    sub, _ = await orders.mark_paid(session, order, panel)
    expires_after_first = sub.expires_at

    same_sub, already = await orders.mark_paid(session, order, panel)

    assert already is True
    assert same_sub.expires_at == expires_after_first  # второй раз дни не начисляются


async def test_expired_subscription_is_disabled_in_panel(session, panel):
    user = await make_user(session, 2005)
    sub, _ = await subscriptions.start_trial(session, user, panel)
    sub.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await session.flush()

    changed = await subscriptions.disable_expired(session, panel)

    assert len(changed) == 1
    assert changed[0].status == "expired"
    panel_user = await panel.get_user(sub.panel_user_uuid)
    assert panel_user is not None and panel_user.enabled is False


async def test_reminders_are_sent_once(session, panel):
    user = await make_user(session, 2006)
    sub, _ = await subscriptions.start_trial(session, user, panel)
    sub.expires_at = datetime.now(timezone.utc) + timedelta(days=2, hours=1)
    await session.flush()

    first = await subscriptions.due_for_reminder(session, 3)
    second = await subscriptions.due_for_reminder(session, 3)

    assert len(first) == 1
    assert second == []


async def test_referral_rewards_both_sides_after_first_payment(session, panel):
    referrer = await make_user(session, 3001)
    invited = await make_user(session, 3002)
    referrer_sub, _ = await subscriptions.start_trial(session, referrer, panel)
    invited_sub, _ = await subscriptions.start_trial(session, invited, panel)

    attached = await referral.attach_referrer(session, invited, referrer.referral_code)
    assert attached is not None and attached.id == referrer.id

    referrer_before = referrer_sub.expires_at
    invited_before = invited_sub.expires_at

    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, invited, plan, provider="manual")
    await orders.mark_paid(session, order, panel)

    assert (referrer_sub.expires_at - referrer_before) >= timedelta(days=6)
    assert (invited_sub.expires_at - invited_before) >= timedelta(days=32)  # 30 тарифа + 3 бонуса


async def test_referral_reward_is_paid_once(session, panel):
    referrer = await make_user(session, 3101)
    invited = await make_user(session, 3102)
    await subscriptions.start_trial(session, referrer, panel)
    await subscriptions.start_trial(session, invited, panel)
    await referral.attach_referrer(session, invited, referrer.referral_code)

    plan = (await orders.list_plans(session))[0]
    first_order = await orders.create_order(session, invited, plan, provider="manual")
    await orders.mark_paid(session, first_order, panel)
    first_reward = await session.get(User, referrer.id)

    second_order = await orders.create_order(session, invited, plan, provider="manual")
    await orders.mark_paid(session, second_order, panel)

    invited, paid = await referral.referral_stats(session, first_reward)
    assert (invited, paid) == (1, 1)


async def test_stale_orders_are_closed(session):
    user = await make_user(session, 4001)
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="crypto")
    order.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.flush()

    expired = await orders.expire_stale_orders(session)

    assert order in expired
    assert order.status == "expired"


async def test_panel_failure_does_not_break_trial(session):
    """Если панель недоступна — операция падает с PanelError, а не молча ломает данные."""
    from app.panels.base import PanelError

    class BrokenPanel:
        name = "broken"

        async def create_user(self, spec):
            raise PanelError("панель недоступна")

        async def find_user_by_email(self, email):
            # Панель недоступна целиком: и восстановление по email не работает,
            # поэтому ошибка обязана дойти до вызывающего кода.
            raise PanelError("панель недоступна")

    user = await make_user(session, 5001)
    with pytest.raises(PanelError):
        await subscriptions.start_trial(session, user, BrokenPanel())

    assert await subscriptions.get_subscription(session, user.id) is None
