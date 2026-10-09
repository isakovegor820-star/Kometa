"""CLI: экономика привлечения и проверка акций.

Запуск:
    .venv/bin/python -m app.tools.marketing                 # база: LTV, потолки, план
    .venv/bin/python -m app.tools.marketing --referral      # условия рефералки и их цена
    .venv/bin/python -m app.tools.marketing --placements    # окупаемость размещений
    .venv/bin/python -m app.tools.marketing --plan          # план роста на 12 месяцев
    .venv/bin/python -m app.tools.marketing --churn 0.12 --arpu 120
"""

from __future__ import annotations

import argparse

from app.config import get_settings
from app.services.finmodel import DEFAULT_PLANS, Costs, average_monthly_revenue
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

settings = get_settings()


def money(value: float) -> str:
    if value == float("inf"):
        return "—"
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Экономика привлечения Kometa")
    parser.add_argument(
        "--arpu", type=float, default=average_monthly_revenue(), help="средний чек в месяц, ₽"
    )
    parser.add_argument("--fee", type=float, default=10.0, help="комиссия канала оплаты, %%")
    parser.add_argument("--churn", type=float, default=0.15, help="месячный отток, доля")
    parser.add_argument(
        "--node", type=float, default=Costs().node_rub, help="нода в месяц, ₽"
    )
    parser.add_argument("--spend", type=float, default=10000.0, help="бюджет на привлечение в месяц, ₽")
    parser.add_argument("--referral", action="store_true", help="показать условия рефералки")
    parser.add_argument("--placements", action="store_true", help="показать окупаемость размещений")
    parser.add_argument("--plan", action="store_true", help="показать план роста на 12 месяцев")
    parser.add_argument("--all", action="store_true", help="все разделы")
    return parser.parse_args()


#: Условия рефералки берём из настроек (.env), а не из значений по умолчанию:
#: калькулятор обязан показывать то, что реально работает в проде, иначе он
#: превращается в источник красивых, но неверных цифр.
def referral_terms(plan) -> ReferralTerms:  # noqa: ANN001 - Plan
    return ReferralTerms(
        invited_discount_percent=settings.referral_discount_percent,
        discount_cap_rub=settings.referral_discount_max_rub,
        referrer_days=settings.referral_bonus_days_referrer,
        invited_bonus_days=settings.referral_bonus_days_invited,
        plan=plan,
    )


def renewal_terms(plan) -> ReferralTerms:  # noqa: ANN001 - Plan
    """Условия продления: та же скидка не применяется, дни — свои."""
    terms = referral_terms(plan)
    return ReferralTerms(
        invited_discount_percent=0,
        discount_cap_rub=0,
        referrer_days=settings.referral_bonus_days_renewal,
        invited_bonus_days=0,
        plan=plan,
    )


def show_base(econ: UnitEconomics, spend: float) -> None:
    print("=" * 74)
    print("БАЗА: сколько приносит один клиент")
    print("=" * 74)
    print(f"  средний чек:                 {money(econ.arpu_rub)}/мес")
    print(f"  на руки после канала оплаты: {money(econ.net_rub)}/мес  (комиссия {econ.fee_percent:.1f} %)")
    print(f"  срок жизни при оттоке {econ.churn:.0%}:  {econ.lifetime_months:.1f} мес")
    print(f"  LTV (за всю жизнь):          {money(econ.ltv_rub)}")
    print(f"  себестоимость клиента:       {money(econ.marginal_cost_rub)}/мес  "
          f"(нода {money(econ.node_rub)} на {econ.users_per_node:.0f} человек)")
    print(f"  безубыточность:              {econ.break_even_users:.1f} платящих на одну ноду")
    print()
    print("  Потолок расходов на привлечение одного платящего:")
    print(f"    осторожно (1/3 LTV):       {money(econ.cac_ceiling_rub(1 / 3))}")
    print(f"    предел (1/2 LTV):          {money(econ.cac_ceiling_rub(1 / 2))}")
    print(f"    окупаемость за 3 месяца:   {money(econ.net_rub * 3)}")
    print()
    print(f"  Бюджет {money(spend)}/мес при разном потолке даёт новых платящих:")
    for ceiling in (150, 200, 250, 300, 400, 600, 800, 1000):
        print(f"    CAC {ceiling:>5} ₽  →  {spend / ceiling:5.1f} клиентов/мес   "
              f"{'ок' if ceiling <= econ.cac_ceiling_rub(1 / 3) else ('на грани' if ceiling <= econ.cac_ceiling_rub(1 / 2) else 'дорого')}")


def show_referral(econ: UnitEconomics) -> None:
    print()
    print("=" * 74)
    print("РЕФЕРАЛКА: цена одного приведённого друга")
    print("=" * 74)
    print(f"  {'тариф':<12} {'скидка':>8} {'друг платит':>12} {'отдаём всего':>13} {'CAC':>8} {'окупаемость':>12}  вердикт")
    for plan in DEFAULT_PLANS:
        terms = referral_terms(plan)
        cost = referral_cost(terms, econ)
        ok, why = referral_verdict(cost, econ, terms)
        mark = "✓" if ok else "✗"
        print(f"  {plan.title:<12} {money(cost.discount_rub):>8} {money(cost.first_payment_rub):>12} "
              f"{money(cost.contra_revenue_rub):>13} {money(cost.effective_cac_rub):>8} "
              f"{cost.payback_months(econ):>9.1f} мес  {mark} {why}")
    print()
    print("  То же, но с планкой скидки (чтобы годовой тариф не уходил вдвое дешевле):")
    for plan in DEFAULT_PLANS:
        for cap in (200, 240, 300):
            terms = referral_terms(plan)
            terms = ReferralTerms(
                invited_discount_percent=terms.invited_discount_percent,
                discount_cap_rub=cap,
                referrer_days=terms.referrer_days,
                invited_bonus_days=terms.invited_bonus_days,
                plan=plan,
            )
            cost = referral_cost(terms, econ)
            ok, why = referral_verdict(cost, econ, terms)
            mark = "✓" if ok else "✗"
            print(f"    {plan.title:<12} планка {cap:>4} ₽ → скидка {money(cost.discount_rub):>7}, "
                  f"CAC {money(cost.effective_cac_rub):>7}, окупаемость {cost.payback_months(econ):>4.1f} мес  {mark} {why}")
        print()
    print("  Награда за продление друга (вторая и следующие оплаты):")
    for plan in DEFAULT_PLANS:
        terms = renewal_terms(plan)
        cost = referral_cost(terms, econ)
        print(f"    {plan.title:<12} +{terms.referrer_days} дн. пригласившему за каждое продление "
              f"→ {money(cost.contra_revenue_rub)}")
    print()
    print("  Цена бонусных дней пригласившего (за сколько дней что отдаём):")
    for days in (7, 14, 21, 30, 60):
        print(f"    {days:>2} дн → {money(econ.months_of_service_rub(days / 30))}")
    print()
    print("  Виральность: сколько новых даёт каждый клиент (K)")
    for invites in (0.3, 0.5, 1.0, 1.5):
        line = "    приглашений на клиента " + f"{invites:.1f}: "
        line += "  ".join(f"оплат {c:.0%} → K={viral_coefficient(invites, c):.2f}" for c in (0.2, 0.35, 0.5))
        print(line)
    print()
    print("  Цепочка при базе 100 клиентов и K=0.35 (сколько людей придёт «волнами»):")
    for k in (0.2, 0.35, 0.5):
        rows = viral_reach(100, k, generations=6)
        print(f"    K={k:.2f}: " + " → ".join(str(x) for x in rows) + f"   всего {sum(rows)}")


def show_placements(econ: UnitEconomics) -> None:
    print()
    print("=" * 74)
    print("РАЗМЕЩЕНИЯ: что должен дать платный канал, чтобы окупиться")
    print("=" * 74)
    print(f"  потолок CAC: {money(econ.cac_ceiling_rub(1 / 3))} (осторожно) … {money(econ.cac_ceiling_rub(1 / 2))} (предел)")
    print()
    print(f"  {'цена поста':>11} | при конверсии из пробного в оплату")
    print(f"  {'':>11} | {'20 %':>18} {'30 %':>18} {'40 %':>18}")
    for spend in (500, 1000, 2000, 3000, 5000, 8000):
        cells = []
        for conv in (0.2, 0.3, 0.4):
            need = required_trials(spend, econ, conv)
            cells.append(f"{need:>7.0f} пробных")
        print(f"  {money(spend):>11} | " + "  ".join(cells))
    print()
    print("  Разбор конкретных размещений (заполняется по факту, пример структуры):")
    channels = (
        GrowthChannel("канал про удалённую работу, 1000 ₽", 1000, 25, 7),
        GrowthChannel("чат района, 0 ₽ (свой пост)", 0, 12, 3),
        GrowthChannel("конкурс историй, приз 3 мес", econ.months_of_service_rub(3), 40, 9),
        GrowthChannel("канал про путешествия, 3000 ₽", 3000, 30, 4),
    )
    print(f"  {'канал':<34} {'потрачено':>10} {'пробных':>8} {'оплат':>6} {'CAC':>9}  вердикт")
    for ch in channels:
        print(f"  {ch.name:<34} {money(ch.spend_rub):>10} {ch.trials:>8} {ch.payers:>6} "
              f"{money(ch.cac_rub):>9}  {ch.verdict(econ)}")
    print()
    print("  Правило: у каждого размещения — свой промокод. Без него канал не измерить.")


def show_plan(econ: UnitEconomics, spend: float) -> None:
    print()
    print("=" * 74)
    print("ПЛАН РОСТА: 12 месяцев")
    print("=" * 74)
    variants = (
        ("как есть (без акций)", GrowthPlan(organic_per_month=8, referral_per_month=2)),
        ("+ работа с рефералкой", GrowthPlan(organic_per_month=8, referral_per_month=6)),
        ("+ платные размещения", GrowthPlan(organic_per_month=8, referral_per_month=6, paid_per_month=8, paid_spend_rub=spend)),
    )
    for name, plan in variants:
        cac = plan.cac_rub
        print(f"\n  {name}: +{plan.new_per_month:.0f} платящих/мес"
              + (f", CAC {money(cac)}" if cac else "") + f", расходы на привлечение {money(plan.paid_spend_rub)}/мес")
        rows = plan.projection(econ, months=12)
        print("    мес | платящих | прибыль/мес")
        for month, users, profit in rows:
            if month in {1, 3, 6, 9, 12}:
                print(f"    {month:>3} | {users:>8} | {money(profit):>11}")
        last = rows[-1]
        total_profit = sum(row[2] for row in rows)
        print(f"    итог за год: прибыль {money(total_profit)}, клиентов к концу года {last[1]}")


def main() -> None:
    args = parse_args()
    econ = UnitEconomics(arpu_rub=args.arpu, fee_percent=args.fee, churn=args.churn, node_rub=args.node)
    show_all = args.all or not (args.referral or args.placements or args.plan)
    if show_all or args.referral:
        show_base(econ, args.spend)
    if show_all or args.referral:
        show_referral(econ)
    if show_all or args.placements:
        show_placements(econ)
    if show_all or args.plan:
        show_plan(econ, args.spend)


if __name__ == "__main__":
    main()
