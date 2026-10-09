"""Тесты финансовой модели: средний чек, безубыточность, ёмкость, проекция роста."""

from __future__ import annotations

import pytest

from app.services.finmodel import (
    DEFAULT_PLANS,
    Costs,
    Plan,
    PlanMix,
    average_monthly_revenue,
    break_even_users,
    capacity_users,
    nodes_needed,
    project,
    users_for_revenue,
)


def test_plan_price_per_month():
    monthly = next(p for p in DEFAULT_PLANS if p.code == "m1")
    yearly = next(p for p in DEFAULT_PLANS if p.code == "m12")

    assert monthly.per_month == 120  # базовая точка сетки от 08.10.2026
    # годовой тариф заметно дешевле в пересчёте на месяц (79 ₽ против 120 ₽)
    assert yearly.per_month < monthly.per_month * 0.7
    assert yearly.per_month > monthly.per_month * 0.6


def test_average_revenue_with_default_mix():
    net = average_monthly_revenue(DEFAULT_PLANS, PlanMix())

    # 0.5*120 + 0.25*100 + 0.15*90 + 0.10*79 ≈ 106, плюс поправка на звёзды
    assert 104 < net < 112


def test_average_revenue_without_stars_bonus():
    net = average_monthly_revenue(DEFAULT_PLANS, PlanMix(), stars_net_factor=1.0)
    assert 101 < net < 109


def test_mix_must_sum_to_one():
    with pytest.raises(ValueError, match="сумма долей"):
        average_monthly_revenue(DEFAULT_PLANS, PlanMix(monthly=0.9, quarterly=0.9, half_year=0.9, yearly=0.9))


def test_mix_size_must_match_plans():
    with pytest.raises(ValueError, match="количество долей"):
        average_monthly_revenue((Plan("only", "Один", 100, 30),), PlanMix())


def test_all_monthly_plan_is_most_expensive():
    """Чем больше доля месячных подписок, тем выше средний чек."""
    cheap = average_monthly_revenue(DEFAULT_PLANS, PlanMix(monthly=0, quarterly=0, half_year=0, yearly=1))
    pricey = average_monthly_revenue(DEFAULT_PLANS, PlanMix(monthly=1, quarterly=0, half_year=0, yearly=0))

    assert pricey > cheap


def test_break_even_is_about_five_clients():
    """При чеке ~110 ₽ ноду за 459 ₽ окупают ~4,2 платящих (было 2,8)."""
    net = average_monthly_revenue(DEFAULT_PLANS, PlanMix())
    users = break_even_users(net, Costs(nodes=1))

    assert 4 < users < 5.5


def test_capacity_and_nodes():
    assert capacity_users(1) == (30, 60)
    assert nodes_needed(30) == 1
    assert nodes_needed(46) == 2
    assert nodes_needed(100) == 3
    assert nodes_needed(0) == 1


def test_projection_grows_and_reaches_plateau():
    net = average_monthly_revenue(DEFAULT_PLANS, PlanMix())
    scenario = project(
        "тест", months=24, new_per_month=15, churn=0.15, net_per_user=net, costs=Costs(nodes=1)
    )

    assert scenario.rows[0].users == 15
    assert scenario.rows[-1].users > scenario.rows[0].users
    # к 24-му месяцу выходим на плато new/churn = 100
    assert 90 < scenario.rows[-1].users <= 100
    assert scenario.total_revenue > 0
    assert scenario.steady_users == pytest.approx(100)


def test_projection_adds_nodes_when_growing():
    net = average_monthly_revenue(DEFAULT_PLANS, PlanMix())
    scenario = project(
        "тест", months=12, new_per_month=30, churn=0.12, net_per_user=net, costs=Costs(nodes=1)
    )

    last = scenario.rows[-1]
    assert last.users > 45
    assert last.costs_rub > Costs(nodes=1).monthly  # докупили ноды
    assert last.profit_rub > 0


def test_zero_churn_grows_linearly():
    scenario = project(
        "без оттока", months=3, new_per_month=10, churn=0.0, net_per_user=182, costs=Costs(nodes=1)
    )

    assert [row.users for row in scenario.rows] == [10, 20, 30]


def test_users_for_revenue():
    assert users_for_revenue(1820, 182) == 10
    assert users_for_revenue(10000, 182) == 55
    assert users_for_revenue(1000, 182) == 6  # округление вверх


def test_break_even_rejects_zero_revenue():
    with pytest.raises(ValueError, match="больше нуля"):
        break_even_users(0, Costs())
