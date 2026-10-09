"""Тесты копеечной подписи заказа (H3).

Зачем это вообще: клиент платит переводом на уникальную сумму — 199 ₽ **13
копеек**. По этим копейкам автоплатёж понимает, чей это перевод. Если два
открытых заказа получат одинаковую пару «сумма + копейки», деньги одного
человека могут подтвердить заказ другого.

Что проверяем:

* два параллельных заказа на одну сумму не получают одинаковые копейки;
* уникальность держит БД (частичный UNIQUE-индекс), а не только код;
* при конфликте вставки копейки подбираются заново, а не падает заказ;
* «сумма без копеек» подтверждает заказ только если кандидат один, иначе —
  «неопознанный» платёж и алерт;
* после оплаты подпись освобождается, а у не-ручных способов оплаты копеек нет
  и мешать друг другу они не должны.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db.models import Alert, Order, Subscription, User
from app.payments.statements import IncomingPayment
from app.services import autopay, orders, subscriptions

settings = get_settings()


@pytest.fixture
def autopay_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "autopay_enabled", True)
    monkeypatch.setattr(settings, "statement_state_file", str(tmp_path / "state.json"))
    monkeypatch.setattr(settings, "autopay_tolerance_kopecks", 0)
    yield


async def _user(session, tg_id: int) -> User:  # noqa: ANN001
    user, _ = await subscriptions.get_or_create_user(session, tg_id=tg_id, username=f"kop{tg_id}")
    return user


async def _plan(session):  # noqa: ANN001
    return (await orders.list_plans(session))[0]


async def test_two_pending_orders_get_different_kopecks(session):
    """Два открытых заказа на одну сумму — разные копейки и разные суммы к оплате."""
    first_user, second_user = await _user(session, 9201), await _user(session, 9202)
    plan = await _plan(session)

    first = await orders.create_order(session, first_user, plan, provider="manual")
    second = await orders.create_order(session, second_user, plan, provider="manual")
    await session.commit()

    assert first.status == "pending" and second.status == "pending"
    assert first.amount_rub == second.amount_rub
    assert first.pay_kopecks != second.pay_kopecks
    assert first.pay_amount_kopecks != second.pay_amount_kopecks
    assert first.pay_kopecks > 0 and second.pay_kopecks > 0


async def test_database_rejects_duplicate_pending_signature(session):
    """Уникальность держит БД: без неё гонка двух процессов не ловится кодом."""
    user = await _user(session, 9203)
    plan = await _plan(session)
    first = await orders.create_order(session, user, plan, provider="manual")
    await session.commit()

    duplicate = Order(
        user_id=user.id,
        plan_id=plan.id,
        amount_rub=first.amount_rub,
        provider="manual",
        status="pending",
        pay_kopecks=first.pay_kopecks,
        external_id="dup-kopeck-1",
        expires_at=first.expires_at,
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_paid_order_frees_the_signature(session):
    """Оплаченный заказ подпись не держит: новые продажи не упираются в лимит."""
    user = await _user(session, 9204)
    plan = await _plan(session)
    first = await orders.create_order(session, user, plan, provider="manual")
    signature = first.pay_kopecks
    first.status = "paid"
    await session.commit()

    second_user = await _user(session, 9205)
    second = await orders.create_order(session, second_user, plan, provider="manual")
    await session.commit()

    assert second.pay_kopecks == signature  # подпись освободилась


async def test_non_manual_orders_do_not_collide(session):
    """У Stars/Platega копеек нет: два счёта на одну сумму — норма, не конфликт."""
    first_user, second_user = await _user(session, 9206), await _user(session, 9207)
    plan = await _plan(session)

    first = await orders.create_order(session, first_user, plan, provider="stars")
    second = await orders.create_order(session, second_user, plan, provider="stars")
    await session.commit()

    assert first.pay_kopecks == 0 and second.pay_kopecks == 0
    assert first.amount_rub == second.amount_rub


async def test_conflict_repicks_kopecks_instead_of_failing(session, monkeypatch):
    """Гонка за подписью: занятая пара не роняет заказ, копейки подбираются снова."""
    occupier, buyer = await _user(session, 9208), await _user(session, 9209)
    plan = await _plan(session)
    taken = await orders.create_order(session, occupier, plan, provider="manual")
    await session.commit()
    taken_kopecks = taken.pay_kopecks

    real_allocate = orders._allocate_pay_kopecks
    calls = {"n": 0}

    async def racing_allocate(session_, base_rub):  # noqa: ANN001
        # Первый вызов возвращает уже занятую подпись (как будто два процесса
        # одновременно увидели одно и то же свободное значение), дальше — честный подбор.
        calls["n"] += 1
        if calls["n"] == 1:
            return taken_kopecks
        return await real_allocate(session_, base_rub)

    monkeypatch.setattr(orders, "_allocate_pay_kopecks", racing_allocate)

    order = await orders.create_order(session, buyer, plan, provider="manual")
    await session.commit()

    assert calls["n"] >= 2  # конфликт был и его разобрали
    assert order.pay_kopecks != taken_kopecks
    assert order.pay_kopecks > 0
    assert order.status == "pending"


async def test_amount_without_kopecks_needs_a_single_candidate(session, autopay_env):
    """«Сумма без копеек» при двух кандидатах не выдаёт доступ никому — только алерт."""
    first_user, second_user = await _user(session, 9210), await _user(session, 9211)
    plan = await _plan(session)
    first = await orders.create_order(session, first_user, plan, provider="manual")
    second = await orders.create_order(session, second_user, plan, provider="manual")
    await session.commit()

    payment = IncomingPayment(
        amount_kopecks=first.amount_rub * 100,  # банк не передал копейки
        received_at=datetime.now(timezone.utc),
        comment="перевод",
        source="csv",
        external_id="kop-amb-1",
    )

    found, reason = await autopay.find_order_for_payment(session, payment)
    assert found is None and reason == autopay.AMBIGUOUS_MATCH

    class _Source:
        name = "csv"

        async def fetch(self, since):  # noqa: ANN001, ANN202
            return [payment]

        async def close(self) -> None:  # pragma: no cover
            return None

    result = await autopay.reconcile(session, None, None, sources=[_Source()])
    await session.commit()

    assert result.confirmed == []
    assert await session.scalar(
        select(Subscription).where(Subscription.user_id.in_([first_user.id, second_user.id]))
    ) is None
    assert await session.scalar(select(Alert).where(Alert.kind == "payment_unmatched")) is not None
    # Оба заказа по-прежнему открыты — деньги не «пристроили» наугад.
    for order in (first, second):
        await session.refresh(order)
        assert order.status == "pending"
