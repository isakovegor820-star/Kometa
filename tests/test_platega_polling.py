"""Тесты подтверждения платежей Platega опросом (``job_check_platega``).

Зачем это отдельно от вебхука: у части установок публичного адреса для
вебхука нет вовсе (бот живёт на домашнем компьютере), и тогда опрос —
единственный автоматический путь выдачи доступа. Плюс он страхует случай
«вебхук не дошёл»: деньги пришли, а доступ надо выдать.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.main import job_check_platega, job_check_platega_late
from app.payments.base import PaymentCheck, PaymentStatus
from app.payments.registry import payments
from app.services import orders, subscriptions

MERCHANT = "34c1b38c-6068-4196-bdd4-8fea587e8d75"
SECRET = "test-secret"
settings = get_settings()


@pytest.fixture(autouse=True)
def platega_settings(monkeypatch):
    """Настроенный провайдер Platega в реестре; сеть подменяют сами тесты."""
    monkeypatch.setattr(settings, "platega_merchant_id", MERCHANT)
    monkeypatch.setattr(settings, "platega_secret", SECRET)
    monkeypatch.setattr(settings, "platega_methods", "2,13")
    payments.reload()
    yield
    payments.reload()


def stub_check(monkeypatch, provider, status: PaymentStatus):  # noqa: ANN001
    """Подменить обращение к API: считаем, сколько раз спросили статус."""
    calls: list[str] = []

    async def fake_check(external_id: str) -> PaymentCheck:
        calls.append(external_id)
        return PaymentCheck(status=status, amount=199, raw={"id": external_id})

    monkeypatch.setattr(provider, "check_payment", fake_check)
    return calls


async def make_platega_order(session, tg_id: int, external_id: str = "tx-1"):  # noqa: ANN001
    """Заказ, как его создаёт бот: со ссылкой на счёт провайдера."""
    user, _ = await subscriptions.get_or_create_user(session, tg_id=tg_id, username=f"pl{tg_id}")
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="platega_sbp")
    order.external_id = external_id
    await session.commit()
    return user, order


async def test_polling_confirms_paid_order(bot, session, monkeypatch):
    """Оплата подтверждается опросом — доступ выдаётся без вебхука."""
    user, order = await make_platega_order(session, 9701)
    provider = payments.get("platega_sbp")
    assert provider is not None, "провайдер Platega не собран из настроек"
    calls = stub_check(monkeypatch, provider, PaymentStatus.PAID)

    await job_check_platega(bot)

    assert calls == ["tx-1"]
    await session.refresh(order)
    assert order.status == "paid"
    sub = await subscriptions.get_subscription(session, user.id)
    assert sub is not None and sub.status == "active"


async def test_polling_skips_unpaid_order(bot, session, monkeypatch):
    """Неоплаченный счёт доступ не выдаёт."""
    user, order = await make_platega_order(session, 9702)
    provider = payments.get("platega_sbp")
    stub_check(monkeypatch, provider, PaymentStatus.PENDING)

    await job_check_platega(bot)

    await session.refresh(order)
    assert order.status == "pending"


async def test_polling_skips_order_without_invoice(bot, session, monkeypatch):
    """Заказ без счёта у провайдера не опрашиваем: спрашивать нечего."""
    user, order = await make_platega_order(session, 9703, external_id="ord-abcdef0123456789")
    provider = payments.get("platega_sbp")
    calls = stub_check(monkeypatch, provider, PaymentStatus.PAID)

    await job_check_platega(bot)

    assert calls == []
    await session.refresh(order)
    assert order.status == "pending"


async def test_polling_is_idempotent(bot, session, monkeypatch):
    """Повторный прогон не продлевает подписку второй раз."""
    user, order = await make_platega_order(session, 9704)
    provider = payments.get("platega_sbp")
    stub_check(monkeypatch, provider, PaymentStatus.PAID)

    await job_check_platega(bot)
    sub = await subscriptions.get_subscription(session, user.id)
    expires_first = sub.expires_at

    await job_check_platega(bot)

    await session.refresh(sub)
    assert sub.expires_at == expires_first


async def test_polling_survives_api_errors(bot, session, monkeypatch):
    """Ошибка API по одному заказу не роняет фоновую задачу."""
    from app.payments.base import PaymentError

    user, order = await make_platega_order(session, 9705)
    provider = payments.get("platega_sbp")

    async def broken(external_id: str) -> PaymentCheck:
        raise PaymentError("Platega недоступна: таймаут")

    monkeypatch.setattr(provider, "check_payment", broken)

    await job_check_platega(bot)  # не должно бросить

    await session.refresh(order)
    assert order.status == "pending"


async def test_polling_uses_any_platega_provider_when_method_removed(bot, session, monkeypatch):
    """Метод убрали из настроек — оплату всё равно доводим до конца.

    Статус транзакции не зависит от номера метода, а учётные данные у всех
    методов Platega одни.
    """
    user, order = await make_platega_order(session, 9706, external_id="tx-card")
    order.provider = "platega_card"  # такого провайдера в настройках нет
    await session.commit()

    provider = payments.get("platega_sbp")
    calls = stub_check(monkeypatch, provider, PaymentStatus.PAID)

    await job_check_platega(bot)

    assert calls == ["tx-card"]
    await session.refresh(order)
    assert order.status == "paid"


async def test_check_button_works_when_method_removed(bot, dispatcher, session, monkeypatch):
    """Кнопка «Проверить оплату» находит провайдера, даже если метод убрали.

    Иначе оплата зависает: деньги пришли, а заказ не подтверждается ничем.
    """
    from tests.fakes import make_update

    user, order = await make_platega_order(session, 9707, external_id="tx-removed")
    order.provider = "platega_card"  # такого метода в настройках нет
    await session.commit()

    provider = payments.get("platega_sbp")
    calls = stub_check(monkeypatch, provider, PaymentStatus.PAID)

    await dispatcher.feed_update(bot, make_update(callback_data=f"order:check:{order.id}", user_id=9707))

    assert calls == ["tx-removed"]
    await session.refresh(order)
    assert order.status == "paid"


async def test_paid_closed_order_is_granted_without_admin(bot, session, monkeypatch):
    """Оплата по закрытому заказу: доступ выдаётся сам, админ не нужен.

    Так выглядит «бот спал»: платёжная ссылка живёт 15 минут, заказ закрывается
    через 30 — машина проснулась позже, деньги на счёте есть, а заказ отменён.
    Раньше такой платёж ждал владельца («проверь поступление и выдай вручную»):
    человек заплатил и зависел от того, когда админ посмотрит телефон. Теперь
    заказ открывается заново и доступ выдаётся автоматически.
    """
    user, order = await make_platega_order(session, 9708)
    order.status = "canceled"
    await session.commit()

    provider = payments.get("platega_sbp")
    stub_check(monkeypatch, provider, PaymentStatus.PAID)

    bot.session.clear()
    await job_check_platega_late(bot)

    await session.refresh(order)
    assert order.status == "paid", "оплаченный заказ должен быть закрыт как оплаченный"
    sub = await subscriptions.get_subscription(session, user.id)
    assert sub is not None and sub.is_active, "доступ выдан без участия админа"

    sent = bot.session.all_text()
    assert "подтвержд" in sent.lower() or "Оплата" in sent, "клиент получил доступ"
    assert "выдай доступ вручную" not in sent, "админа не просим подтверждать"

    # Второй прогон ничего не ломает: заказ уже оплачен.
    bot.session.clear()
    await job_check_platega_late(bot)
    assert bot.session.all_text() == ""


async def test_paid_expired_order_is_granted_without_admin(bot, session, monkeypatch):
    """Истёкший заказ с пришедшими деньгами — та же автовыдача."""
    user, order = await make_platega_order(session, 9709)
    order.status = "expired"
    await session.commit()

    provider = payments.get("platega_sbp")
    stub_check(monkeypatch, provider, PaymentStatus.PAID)

    bot.session.clear()
    await job_check_platega_late(bot)

    await session.refresh(order)
    assert order.status == "paid"
    sub = await subscriptions.get_subscription(session, user.id)
    assert sub is not None and sub.is_active


async def test_fast_polling_does_not_touch_closed_orders(bot, session, monkeypatch):
    """Быстрый опрос (раз в 30 секунд) спрашивает только открытые счета.

    Иначе по каждому истёкшему счёту мы бы дёргали платёжную систему каждые
    полминуты сутки напролёт.
    """
    user, order = await make_platega_order(session, 9711, external_id="tx-closed")
    order.status = "expired"
    await session.commit()

    provider = payments.get("platega_sbp")
    calls = stub_check(monkeypatch, provider, PaymentStatus.PAID)

    await job_check_platega(bot)

    assert calls == [], "закрытые заказы быстрый опрос не трогает"
    await session.refresh(order)
    assert order.status == "expired"


async def test_closed_orders_are_not_polled_forever(session):
    """Старые закрытые заказы в выборку не попадают — API не дёргаем зря."""
    from datetime import datetime, timedelta, timezone

    from app.services import orders as orders_service

    user, old = await make_platega_order(session, 9710, external_id="tx-old")
    old.status = "canceled"
    old.created_at = datetime.now(timezone.utc) - timedelta(hours=48)
    await session.commit()

    found = await orders_service.awaiting_payment(session, provider_prefix="platega")

    assert old.id not in [o.id for o in found]
