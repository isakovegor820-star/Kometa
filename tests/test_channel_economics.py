"""Тесты отчёта о доходности каналов оплаты.

Смысл: владелец должен видеть, сколько денег реально доходит до него
по каждому каналу, а не только оборот.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.services import orders, stats, subscriptions

settings = get_settings()


async def make_paid_order(session, tg_id: int, provider: str, plan_code: str = "m1"):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=tg_id, username=f"p{tg_id}")
    plans = await orders.list_plans(session)
    plan = next(p for p in plans if p.code == plan_code)
    order = await orders.create_order(session, user, plan, provider=provider)
    order.status = "paid"
    order.paid_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    await session.flush()
    return order, plan


async def test_manual_channel_keeps_full_amount(session):
    _, plan = await make_paid_order(session, 9901, "manual")
    await session.flush()

    channels = await stats.channel_economics(session, days=30)
    manual = next(c for c in channels if c.provider == "manual")

    assert manual.orders == 1
    assert manual.gross_rub == plan.price_rub
    assert manual.net_rub == plan.price_rub
    assert manual.fee_percent == 0


async def test_wata_channel_subtracts_acquiring_fee(session, monkeypatch):
    monkeypatch.setattr(settings, "fee_percent_wata", 3.5)
    _, plan = await make_paid_order(session, 9902, "wata")
    await session.flush()

    channels = await stats.channel_economics(session, days=30)
    wata = next(c for c in channels if c.provider == "wata")

    assert wata.gross_rub == plan.price_rub
    assert wata.net_rub == round(plan.price_rub * (1 - 0.035))
    assert 3.0 < wata.fee_percent < 4.0


async def test_sbp_partner_channel_subtracts_eight_percent(session, monkeypatch):
    """СБП через банк-партнёра: 8 % с оборота (условия партнёра от 06.10.2026)."""
    monkeypatch.setattr(settings, "fee_percent_sbp", 8.0)
    _, plan = await make_paid_order(session, 9940, "platega_sbp")
    await session.flush()

    channels = await stats.channel_economics(session, days=30)
    sbp = next(c for c in channels if c.provider == "platega_sbp")

    assert sbp.gross_rub == plan.price_rub
    assert sbp.net_rub == round(plan.price_rub * 0.92)
    assert 7.5 < sbp.fee_percent < 8.5
    # Прямой перевод на карту комиссии не платит — это разные каналы.
    assert sbp.net_rub < plan.price_rub


async def test_crypto_channel_subtracts_five_percent(session, monkeypatch):
    """Криптоплатежи: 5 % по условиям партнёра."""
    monkeypatch.setattr(settings, "fee_percent_crypto", 5.0)
    _, plan = await make_paid_order(session, 9941, "crypto")
    await session.flush()

    channels = await stats.channel_economics(session, days=30)
    crypto = next(c for c in channels if c.provider == "crypto")

    assert crypto.net_rub == round(plan.price_rub * 0.95)
    assert 4.5 < crypto.fee_percent < 5.5


async def test_stars_channel_counts_telegram_payout(session, monkeypatch):
    """У звёзд комиссия структурная: получаем $0.013 за звезду, а не рубли счёта."""
    monkeypatch.setattr(settings, "stars_payout_usd", 0.013)
    monkeypatch.setattr(settings, "usd_rub_rate", 92.0)
    monkeypatch.setattr(settings, "fragment_withdrawal_percent", 5.0)

    _, plan = await make_paid_order(session, 9903, "stars")
    await session.flush()

    channels = await stats.channel_economics(session, days=30)
    stars = next(c for c in channels if c.provider == "stars")

    expected = round(plan.price_stars * 0.013 * 92.0 * 0.95)
    assert stars.gross_rub == plan.price_rub
    assert stars.net_rub == expected
    # и это не меньше рублёвой цены — иначе канал убыточен
    assert stars.net_rub >= plan.price_rub


async def test_channel_report_sorted_by_net(session):
    await make_paid_order(session, 9904, "manual")
    await make_paid_order(session, 9905, "manual")
    await make_paid_order(session, 9906, "stars")
    await session.flush()

    channels = await stats.channel_economics(session, days=30)

    assert channels[0].provider == "manual"  # два заказа против одного
    assert channels[0].net_rub > channels[1].net_rub


async def test_profit_summary_subtracts_monthly_costs(session, monkeypatch):
    monkeypatch.setattr(settings, "monthly_costs_rub", 750.0)
    # Заказов должно хватить, чтобы перекрыть постоянные расходы (при чеке
    # 120 ₽ пяти мало) — иначе маржа отрицательная и смысл теста теряется.
    orders_count = 8
    price = 0
    for index in range(orders_count):
        _, plan = await make_paid_order(session, 9910 + index, "manual")
        price = plan.price_rub
    await session.flush()

    summary = await stats.profit_summary(session, days=30)

    assert summary["gross"] == orders_count * price
    assert summary["net"] == orders_count * price
    assert summary["costs"] == 750
    assert summary["profit"] == orders_count * price - 750
    assert summary["margin_percent"] > 0


async def test_profit_summary_warns_when_loss(session, monkeypatch):
    monkeypatch.setattr(settings, "monthly_costs_rub", 5000.0)
    await make_paid_order(session, 9920, "manual")
    await session.flush()

    summary = await stats.profit_summary(session, days=30)

    assert summary["profit"] < 0
    assert summary["margin_percent"] < 0


async def test_orders_older_than_window_are_ignored(session):
    from datetime import datetime, timedelta, timezone

    order, _ = await make_paid_order(session, 9930, "manual")
    order.paid_at = datetime.now(timezone.utc) - timedelta(days=40)
    await session.flush()

    channels = await stats.channel_economics(session, days=30)

    assert channels == []
