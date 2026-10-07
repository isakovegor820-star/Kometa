"""Компенсация простоя: периоды, полные сутки, идемпотентность, уведомления.

Компенсация — это деньги (дни подписки), поэтому проверяем не только «работает»,
но и «не начислит дважды»: повторная команда не должна продлевать подписку ещё раз.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import Downtime
from app.services import downtime, subscriptions
from tests.fakes import make_update
from tests.test_subscriptions import make_user


async def _trial(session, panel, tg_id: int):
    """Пользователь с активной пробной подпиской."""
    user = await make_user(session, tg_id)
    sub, _ = await subscriptions.start_trial(session, user, panel)
    await session.commit()
    return user, sub


def test_days_for_counts_full_days_and_caps():
    now = datetime.now(timezone.utc)

    assert downtime._days_for(now - timedelta(days=3, hours=2), now, None) == 3
    assert downtime._days_for(now - timedelta(hours=23), now, None) == 0
    assert downtime._days_for(now - timedelta(days=90), now, None) == downtime.MAX_GRANT_DAYS
    assert downtime._days_for(now, now, 5) == 5
    assert downtime._days_for(now, now, 999) == downtime.MAX_GRANT_DAYS
    assert downtime._days_for(None, now, None) == 0


async def test_start_is_single_open_period(session):
    period, created = await downtime.start(session, note="перебои", actor="admin:1")
    await session.commit()

    again, created_again = await downtime.start(session, note="ещё раз", actor="admin:1")
    await session.commit()

    assert created is True
    assert created_again is False
    assert again.id == period.id

    opened = list((await session.scalars(select(Downtime).where(Downtime.ended_at.is_(None)))).all())
    assert len(opened) == 1


async def test_finish_without_open_period_explains(session):
    result = await downtime.finish(session, actor="admin:1")

    assert result.ok is False
    assert "период" in result.reason


async def test_finish_extends_once_and_second_call_does_nothing(session, panel):
    _user, sub = await _trial(session, panel, 6901)
    period, _ = await downtime.start(session, note="тест", actor="admin:1")
    period.started_at = datetime.now(timezone.utc) - timedelta(days=3, hours=2)
    await session.commit()

    panels = await subscriptions.all_user_panels(session)
    first = await downtime.finish(session, actor="admin:1", panels=panels)
    await session.commit()

    assert first.ok is True
    assert first.days == 3
    assert first.users >= 1
    await session.refresh(sub)
    after_first = sub.expires_at
    assert first.tg_ids

    second = await downtime.finish(session, actor="admin:1", panels=panels)
    await session.commit()

    assert second.ok is False
    await session.refresh(sub)
    assert sub.expires_at == after_first, "повторный finish не должен продлевать ещё раз"


async def test_finish_under_one_day_grants_nothing(session, panel):
    _user, sub = await _trial(session, panel, 6902)
    before = sub.expires_at
    period, _ = await downtime.start(session, note="короткий", actor="admin:1")
    period.started_at = datetime.now(timezone.utc) - timedelta(hours=5)
    await session.commit()

    result = await downtime.finish(session, actor="admin:1", panels=await subscriptions.all_user_panels(session))
    await session.commit()

    assert result.ok is True
    assert result.days == 0
    assert result.users == 0
    await session.refresh(sub)
    assert sub.expires_at == before
    await session.refresh(period)
    assert period.granted_at is not None, "период закрыт, повторно считать нечего"


async def test_expired_subscription_is_not_extended(session, panel):
    _user, sub = await _trial(session, panel, 6903)
    sub.status = "expired"
    await session.commit()
    before = sub.expires_at

    result = await downtime.grant(session, 5, actor="admin:1", panels=await subscriptions.all_user_panels(session))
    await session.commit()

    assert result.days == 5
    await session.refresh(sub)
    assert sub.expires_at == before, "истёкшие подписки не продлеваем: это был бы бесплатный доступ"


async def test_manual_grant_creates_closed_history_entry(session, panel):
    _user, sub = await _trial(session, panel, 6904)
    before = sub.expires_at

    result = await downtime.grant(
        session, 4, note="разовая", actor="admin:7", panels=await subscriptions.all_user_panels(session)
    )
    await session.commit()

    assert result.ok is True
    assert result.days == 4
    await session.refresh(sub)
    assert sub.expires_at > before

    period = await session.scalar(select(Downtime).order_by(Downtime.id.desc()))
    assert period.ended_at is not None
    assert period.granted_at is not None
    assert period.granted_by == "admin:7"
    assert period.note == "разовая"


async def test_grant_zero_days_is_rejected(session):
    result = await downtime.grant(session, 0, actor="admin:1")
    assert result.ok is False


async def test_bot_downtime_flow_notifies_clients(session, panel, bot, dispatcher):
    user, sub = await _trial(session, panel, 6905)
    before = sub.expires_at

    await dispatcher.feed_update(bot, make_update("/downtime_start перебои связи", user_id=1))
    assert "Простой открыт" in bot.session.all_text()

    opened = await session.scalar(select(Downtime).where(Downtime.ended_at.is_(None)))
    assert opened is not None
    opened.started_at = datetime.now(timezone.utc) - timedelta(days=2, hours=3)
    await session.commit()

    bot.session.clear()
    await dispatcher.feed_update(bot, make_update("/downtime_end", user_id=1))

    assert "начислено 2 дн." in bot.session.all_text()
    assert "Связь работала с перебоями" in bot.session.all_text()

    await session.refresh(sub)
    assert sub.expires_at >= before + timedelta(days=1, hours=23)

    bot.session.clear()
    await dispatcher.feed_update(bot, make_update("/downtime", user_id=1))
    assert "Открытого периода простоя нет" in bot.session.all_text()

    assert user.tg_id == 6905
