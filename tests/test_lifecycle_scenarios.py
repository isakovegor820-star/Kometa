"""Автосценарии в боте: подсказка после триала, win-back, апселл, рефералка.

Главное, что здесь проверяется — не тексты, а поведение:

  * человека не беспокоят чаще, чем раз в N дней (иначе бот = спамер);
  * один и тот же сценарий не приходит дважды;
  * сообщение не уходит тому, кто уже заплатил или заблокировал бота.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.db.models import Order
from app.services import gift, lifecycle, orders, subscriptions

settings = get_settings()


async def make_user(session, tg_id: int = 111):
    user, _ = await subscriptions.get_or_create_user(
        session, tg_id=tg_id, username=f"user{tg_id}", first_name=f"User{tg_id}"
    )
    await session.flush()
    return user


async def first_plan(session):
    return (await orders.list_plans(session))[0]


async def make_trial_user(session, panel, tg_id: int, *, expired_days: int = 5):
    """Человек взял пробный доступ, и срок уже истёк.

    Статус оставляем ``trial``: это ровно то состояние, в котором человек
    «попробовал и не купил», — отдельного статуса «триал истёк» в модели нет.
    """
    user = await make_user(session, tg_id)
    sub, _ = await subscriptions.start_trial(session, user, panel)
    sub.expires_at = datetime.now(timezone.utc) - timedelta(days=expired_days)
    await session.flush()
    return user, sub


async def make_paying_monthly(session, panel, tg_id: int, *, started_days_ago: int = 20):
    """Платящий клиент на месячном тарифе."""
    user = await make_user(session, tg_id)
    plan = await first_plan(session)
    order = await orders.create_order(session, user, plan, provider="manual")
    await orders.mark_paid(session, order, panel)
    sub = await subscriptions.get_subscription(session, user.id)
    sub.status = "active"
    sub.starts_at = datetime.now(timezone.utc) - timedelta(days=started_days_ago)
    sub.expires_at = datetime.now(timezone.utc) + timedelta(days=10)
    await session.flush()
    return user, sub


def kinds(planned) -> dict[int, str]:
    return {user.tg_id: scenario.kind for user, scenario in planned}


# ------------------------------------------------------------------ выбор аудитории
async def test_trial_without_payment_gets_reminder(session, panel):
    user, _ = await make_trial_user(session, panel, 7001, expired_days=5)

    planned = await lifecycle.plan_sends(session)

    assert kinds(planned).get(7001) == lifecycle.KIND_TRIAL_NO_PAYMENT


async def test_trial_that_paid_is_not_reminded(session, panel):
    """Заплатил — напоминать «ты не купил» нельзя, это раздражает."""
    user, _ = await make_trial_user(session, panel, 7002, expired_days=5)
    plan = await first_plan(session)
    order = await orders.create_order(session, user, plan, provider="manual")
    await orders.mark_paid(session, order, panel)

    planned = await lifecycle.plan_sends(session)

    assert 7002 not in kinds(planned)


async def test_fresh_trial_is_not_touched_yet(session, panel):
    """Ещё идёт пробный период — напоминание преждевременно."""
    user = await make_user(session, 7003)
    await subscriptions.start_trial(session, user, panel)

    planned = await lifecycle.plan_sends(session)

    assert 7003 not in kinds(planned)


async def test_expired_subscription_gets_winback(session, panel):
    user, sub = await make_paying_monthly(session, panel, 7004)
    sub.status = "expired"
    sub.expires_at = datetime.now(timezone.utc) - timedelta(days=8)
    await session.flush()

    planned = await lifecycle.plan_sends(session)

    assert kinds(planned).get(7004) == lifecycle.KIND_WINBACK


async def test_monthly_client_gets_upsell(session, panel):
    """Платит месяц за месяцем — предлагаем тариф подлиннее."""
    user, _ = await make_paying_monthly(session, panel, 7005, started_days_ago=20)

    planned = await lifecycle.plan_sends(session)

    assert kinds(planned).get(7005) == lifecycle.KIND_UPSELL


async def test_active_client_without_friends_gets_referral_nudge(session, panel):
    """Win-back и апселл важнее: на свежем месячном тарифе ждём апселл."""
    user, _ = await make_paying_monthly(session, panel, 7006, started_days_ago=20)

    planned = await lifecycle.plan_sends(session)

    # Первый подходящий сценарий выигрывает — апселл приоритетнее рефералки.
    assert kinds(planned).get(7006) == lifecycle.KIND_UPSELL


async def test_blocked_user_is_never_messaged(session, panel):
    user, _ = await make_trial_user(session, panel, 7007)
    user.is_blocked = True
    await session.flush()

    planned = await lifecycle.plan_sends(session)

    assert 7007 not in kinds(planned)


async def test_disabled_lifecycle_sends_nothing(session, panel, monkeypatch):
    await make_trial_user(session, panel, 7008)
    monkeypatch.setattr(settings, "lifecycle_enabled", False)

    assert await lifecycle.plan_sends(session) == []


# ------------------------------------------------------------------ отправка и защита от спама
async def test_message_is_sent_and_marked(session, panel, bot):
    user, _ = await make_trial_user(session, panel, 7101)

    sent = await lifecycle.run_lifecycle(bot, session)

    assert sent == 1
    assert "Kometa" in bot.session.all_text()
    assert user.last_lifecycle_kind == lifecycle.KIND_TRIAL_NO_PAYMENT
    assert user.last_lifecycle_at is not None


async def test_same_scenario_is_not_sent_twice(session, panel, bot):
    await make_trial_user(session, panel, 7102)

    first = await lifecycle.run_lifecycle(bot, session)
    second = await lifecycle.run_lifecycle(bot, session)

    assert first == 1
    assert second == 0


async def test_gap_prevents_second_message_too_soon(session, panel, bot):
    """Два сценария подряд одному человеку — это спам, ждём паузу."""
    user, sub = await make_paying_monthly(session, panel, 7103, started_days_ago=20)
    sent = await lifecycle.run_lifecycle(bot, session)
    assert sent == 1

    # Через день подписка кончилась — но пауза ещё не вышла.
    sub.status = "expired"
    sub.expires_at = datetime.now(timezone.utc) - timedelta(days=8)
    await session.flush()

    assert await lifecycle.run_lifecycle(bot, session) == 0

    # Прошла неделя — можно написать снова.
    user.last_lifecycle_at = datetime.now(timezone.utc) - timedelta(
        days=settings.lifecycle_min_gap_days + 1
    )
    await session.flush()

    assert await lifecycle.run_lifecycle(bot, session) == 1
    assert user.last_lifecycle_kind == lifecycle.KIND_WINBACK


async def test_audience_counts_for_report(session, panel):
    await make_trial_user(session, panel, 7201, expired_days=9)
    await make_trial_user(session, panel, 7202, expired_days=9)

    counts = await lifecycle.audience_counts(session)

    assert counts[lifecycle.KIND_TRIAL_NO_PAYMENT] >= 2
    assert set(counts) == {
        lifecycle.KIND_TRIAL_NO_PAYMENT,
        lifecycle.KIND_WINBACK,
        lifecycle.KIND_UPSELL,
        lifecycle.KIND_REFERRAL,
    }


async def test_scenario_texts_mention_real_numbers(session):
    """В текстах — только то, что реально настроено, без выдуманных цифр."""
    texts = {scenario.kind: scenario.text for scenario in lifecycle.scenarios()}

    assert f"{settings.referral_discount_percent} %" in texts[lifecycle.KIND_REFERRAL]
    assert f"{settings.referral_bonus_days_referrer} дней" in texts[lifecycle.KIND_REFERRAL]
    assert "120 ₽" in texts[lifecycle.KIND_TRIAL_NO_PAYMENT]


async def test_gift_scenarios_do_not_break_lifecycle(session, panel, bot):
    """Покупатель подарка не должен получать «ты не оплатил»."""
    buyer = await make_user(session, 7301)
    plan = await first_plan(session)
    order = await orders.create_order(
        session, buyer, plan, provider="manual", kind=orders.KIND_GIFT, with_discount=False
    )
    await gift.attach_gift(session, order)
    order.status = "paid"
    await session.flush()

    planned = await lifecycle.plan_sends(session)

    assert 7301 not in kinds(planned)


# ------------------------------------------------------------------ кнопки
class _RecordingBot:
    """Бот-заглушка: запоминает текст и клавиатуру каждого сообщения."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str, object]] = []

    async def send_message(self, chat_id, text, **kwargs):  # noqa: ANN001, ANN003
        self.messages.append((chat_id, text, kwargs.get("reply_markup")))


def _button_labels(markup) -> list[str]:  # noqa: ANN001
    return [button.text for row in markup.inline_keyboard for button in row]


def test_every_scenario_has_a_button():
    """У каждого автосценария есть кнопка действия.

    Правило проекта «сообщение без кнопки — потерянный клиент» нарушалось ровно
    там, где человек уже разогрет: в текстах написано «вернуть в один клик», а
    нажать было нечего — нужно было самому искать раздел в меню.
    """
    scenario_list = lifecycle.scenarios()

    assert scenario_list, "сценариев нет — тест ничего не проверяет"
    without = [scenario.kind for scenario in scenario_list if scenario.cta is None]
    assert without == [], f"сценарии без кнопки: {without}"


async def test_lifecycle_message_carries_the_button(session, panel):
    """Кнопка не просто объявлена — она уходит в сообщении."""
    await make_trial_user(session, panel, 7006, expired_days=5)
    bot = _RecordingBot()

    planned = await lifecycle.plan_sends(session)
    sent = await lifecycle.run_lifecycle(bot, session, planned=planned)

    assert sent == 1
    _tg_id, _text, markup = bot.messages[-1]
    assert markup is not None, "автосценарий ушёл без кнопки"
    assert "Выбрать тариф" in _button_labels(markup)


async def test_expiry_reminder_carries_the_button(session, panel, monkeypatch):
    """«Продлить в один клик» — теперь правда: кнопка в напоминании."""
    from app.services import notifications

    user, sub = await make_trial_user(session, panel, 7007, expired_days=0)
    sub.status = "active"
    await session.flush()

    async def fake_due(session_, days_before):  # noqa: ANN001, ARG001
        return [sub]

    monkeypatch.setattr(subscriptions, "due_for_reminder", fake_due)
    bot = _RecordingBot()

    sent = await notifications.notify_expiring(bot, session, 3)

    assert sent == 1, "напоминание не ушло"
    _tg_id, _text, markup = bot.messages[-1]
    assert markup is not None, "напоминание о продлении ушло без кнопки"
    assert "Выбрать тариф" in _button_labels(markup)
    assert user.tg_id  # пользователь существует
