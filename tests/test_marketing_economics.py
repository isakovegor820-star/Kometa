"""Тесты экономики привлечения: акции считаются в рублях, а не на глаз.

Проверяем не «красиво ли выглядит скидка», а три вещи:
  1. база (LTV, потолки) считается по формулам из финмодели;
  2. скидка и бонусные дни правильно переводятся в деньги;
  3. калькулятор предупреждает, когда акция дороже, чем приносит клиент.
"""

from __future__ import annotations

import pytest

from app.services.finmodel import DEFAULT_PLANS, Plan
from app.services.marketing import (
    GrowthChannel,
    GrowthPlan,
    ReferralTerms,
    UnitEconomics,
    referral_cost,
    referral_verdict,
    required_trials,
    viral_coefficient,
    viral_reach,
)


def test_net_revenue_matches_finmodel_assumption() -> None:
    """С чека 109 ₽ и комиссии 10 % на руки остаётся 98,1 ₽ — как в доке."""
    econ = UnitEconomics()
    assert econ.net_rub == pytest.approx(98.1, abs=0.05)


def test_ltv_and_ceilings() -> None:
    econ = UnitEconomics()
    assert econ.lifetime_months == pytest.approx(1 / 0.15)
    assert econ.ltv_rub == pytest.approx(econ.net_rub / 0.15)
    assert econ.cac_ceiling_rub(1 / 3) == pytest.approx(econ.ltv_rub / 3)
    assert econ.cac_ceiling_rub() < econ.cac_ceiling_rub(1 / 2)


def test_marginal_cost_is_share_of_node() -> None:
    """Бесплатные дни стоят почти ноль: нода делится на всех клиентов."""
    econ = UnitEconomics()
    assert econ.marginal_cost_rub == pytest.approx(459 / 45)
    assert econ.marginal_cost_rub < econ.net_rub / 5


def test_discount_is_capped_by_plan_price() -> None:
    """Скидка не может быть больше цены тарифа, даже при 100 %."""
    terms = ReferralTerms(invited_discount_percent=100, plan=DEFAULT_PLANS[0])
    assert terms.discount_rub() == DEFAULT_PLANS[0].price_rub


def test_discount_cap_limits_expensive_plans() -> None:
    """Планка скидки защищает длинные тарифы от раздачи вдвое дешевле."""
    year = DEFAULT_PLANS[3]
    without_cap = ReferralTerms(plan=year).discount_rub()
    with_cap = ReferralTerms(plan=year, discount_cap_rub=240).discount_rub()
    assert without_cap == 480
    assert with_cap == 240


def test_free_month_costs_more_than_discounted_month() -> None:
    """Разница между «месяц в подарок» и «скидка 50 %» — в деньгах кассы."""
    econ = UnitEconomics()
    free = referral_cost(ReferralTerms(invited_discount_percent=100, referrer_days=0), econ)
    half = referral_cost(ReferralTerms(invited_discount_percent=50, referrer_days=0), econ)
    assert free.effective_cac_rub > half.effective_cac_rub
    assert half.first_payment_net_rub > 0


def test_longer_plan_costs_us_more_but_pays_back_faster() -> None:
    """Длинный тариф = больше скидка в рублях, но и больше оплата друга."""
    econ = UnitEconomics()
    monthly = referral_cost(ReferralTerms(plan=DEFAULT_PLANS[0]), econ)
    yearly = referral_cost(ReferralTerms(plan=DEFAULT_PLANS[3]), econ)
    assert yearly.discount_rub > monthly.discount_rub
    assert yearly.first_payment_rub > monthly.first_payment_rub
    assert yearly.payback_months(econ) > monthly.payback_months(econ)
    assert yearly.months_paid_for > monthly.months_paid_for


def test_verdict_flags_too_generous_referral() -> None:
    """Скидка 100 % без оплаты друга — дорогая акция, калькулятор это ловит."""
    econ = UnitEconomics()
    terms = ReferralTerms(invited_discount_percent=100, referrer_days=60, plan=DEFAULT_PLANS[0])
    cost = referral_cost(terms, econ)
    ok, why = referral_verdict(cost, econ, terms)
    assert not ok
    assert "не окупается" in why


def test_verdict_accepts_current_program() -> None:
    """Текущие условия (50 % другу, 30 дней пригласившему) проходят проверку."""
    econ = UnitEconomics()
    terms = ReferralTerms(plan=DEFAULT_PLANS[1])
    cost = referral_cost(terms, econ)
    ok, _ = referral_verdict(cost, econ, terms)
    assert ok
    assert cost.effective_cac_rub < econ.cac_ceiling_rub(1 / 3)


def test_viral_coefficient_and_reach() -> None:
    assert viral_coefficient(1.0, 0.35) == pytest.approx(0.35)
    rows = viral_reach(100, 0.5, generations=3)
    assert rows == [50, 25, 12]
    assert viral_reach(100, 0.0) == [0]


def test_channel_cac_and_verdict() -> None:
    econ = UnitEconomics()
    good = GrowthChannel("размещение", 1000, 25, 7)
    bad = GrowthChannel("дорогое размещение", 8000, 20, 3)
    assert good.cac_rub == pytest.approx(1000 / 7)
    assert good.verdict(econ) == "отлично"
    assert bad.verdict(econ) == "убыточно"
    empty = GrowthChannel("пустое", 500, 10, 0)
    assert empty.verdict(econ).startswith("нет оплат")


def test_required_trials_scales_with_price_and_conversion() -> None:
    """Чем дороже размещение и ниже конверсия, тем больше нужно пробных."""
    econ = UnitEconomics()
    cheap = required_trials(1000, econ, 0.3)
    pricey = required_trials(3000, econ, 0.3)
    worse_conv = required_trials(1000, econ, 0.15)
    assert pricey == pytest.approx(cheap * 3)
    assert worse_conv == pytest.approx(cheap * 2)


def test_growth_plan_reaches_plateau() -> None:
    """При постоянном притоке и оттоке рост выходит на плато, а не в бесконечность."""
    econ = UnitEconomics()
    plan = GrowthPlan(organic_per_month=6, referral_per_month=4)
    rows = plan.projection(econ, months=60)
    users = [row[1] for row in rows]
    assert users[-1] == pytest.approx(plan.new_per_month / econ.churn, rel=0.05)
    assert max(users) - min(users[-12:]) <= 2


def test_growth_plan_without_attraction_shrinks() -> None:
    econ = UnitEconomics()
    empty = GrowthPlan(organic_per_month=0, referral_per_month=0)
    rows = empty.projection(econ, months=6, start_users=10)
    assert rows[-1][1] < 10


def test_paid_growth_costs_money_but_adds_users() -> None:
    """Платный канал должен окупаться: он добавляет больше, чем стоит."""
    econ = UnitEconomics()
    free = GrowthPlan(organic_per_month=8, referral_per_month=4)
    paid = GrowthPlan(organic_per_month=8, referral_per_month=4, paid_per_month=8, paid_spend_rub=2000)
    assert paid.cac_rub == pytest.approx(250)
    assert paid.projection(econ, months=12)[-1][1] > free.projection(econ, months=12)[-1][1]


def test_zero_churn_means_infinite_lifetime() -> None:
    econ = UnitEconomics(churn=0.0)
    assert econ.lifetime_months == float("inf")
    assert econ.ltv_rub == float("inf")


def test_plan_price_validation_for_cost() -> None:
    """Нулевая цена тарифа не должна ломать расчёт (акция «первый месяц за 0 ₽»)."""
    econ = UnitEconomics()
    free_plan = Plan("free", "акция", 0, 30)
    cost = referral_cost(ReferralTerms(plan=free_plan, invited_discount_percent=100), econ)
    assert cost.first_payment_rub == 0
    assert cost.effective_cac_rub > 0
