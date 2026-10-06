"""CLI: финансовая модель сервиса.

Запуск:
    .venv/bin/python -m app.tools.finmodel              # три сценария на 12 месяцев
    .venv/bin/python -m app.tools.finmodel --users 50   # что будет при 50 клиентах
    .venv/bin/python -m app.tools.finmodel --target 50000   # сколько нужно для выручки
"""

from __future__ import annotations

import argparse

from app.services.finmodel import (
    DEFAULT_PLANS,
    Costs,
    PlanMix,
    average_monthly_revenue,
    break_even_users,
    capacity_users,
    nodes_needed,
    project,
    users_for_revenue,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Финансовая модель Kometa")
    parser.add_argument("--users", type=int, help="посчитать для фиксированного числа клиентов")
    parser.add_argument("--target", type=float, help="целевая месячная выручка, ₽")
    parser.add_argument("--months", type=int, default=12, help="горизонт планирования (месяцев)")
    parser.add_argument("--nodes", type=int, default=1, help="сколько нод сейчас")
    return parser.parse_args()


def money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def main() -> None:
    args = parse_args()

    mix = PlanMix()
    net = average_monthly_revenue(DEFAULT_PLANS, mix)
    costs = Costs(nodes=args.nodes)

    print("Тарифы:")
    for plan in DEFAULT_PLANS:
        print(f"  {plan.title:10} {plan.price_rub:>5} ₽  →  {plan.per_month:6.0f} ₽/мес")
    print(f"\nСредний чек (при миксе {int(mix.monthly*100)}/{int(mix.quarterly*100)}/"
          f"{int(mix.half_year*100)}/{int(mix.yearly*100)}): {net:.0f} ₽ с клиента в месяц")
    print(f"Расходы: {money(costs.monthly)}/мес ({costs.nodes} нода(ы) × {costs.node_rub} ₽)")
    print(f"Точка безубыточности: {break_even_users(net, costs):.1f} платящих клиентов")

    lo, hi = capacity_users(args.nodes)
    print(f"Ёмкость {args.nodes} нод(ы): {lo}–{hi} клиентов "
          f"(нужно нод при 100 клиентах: {nodes_needed(100)})")

    if args.users:
        users = args.users
        needed = nodes_needed(users)
        revenue = users * net
        month_costs = Costs(nodes=needed).monthly
        print(f"\nПри {users} клиентах:")
        print(f"  выручка: {money(revenue)}/мес")
        print(f"  расходы: {money(month_costs)}/мес ({needed} нод(ы))")
        print(f"  прибыль: {money(revenue - month_costs)}/мес")
        print(f"  за год:  {money((revenue - month_costs) * 12)}")
        return

    if args.target:
        users = users_for_revenue(args.target, net)
        needed = nodes_needed(users)
        print(f"\nДля выручки {money(args.target)}/мес нужно ~{users} платящих "
              f"и {needed} нод(ы)")
        print(f"  расходы: {money(Costs(nodes=needed).monthly)}/мес, "
              f"прибыль: {money(args.target - Costs(nodes=needed).monthly)}/мес")
        return

    scenarios = (
        ("осторожный", 8, 0.18),
        ("базовый", 15, 0.15),
        ("оптимистичный", 30, 0.12),
    )
    print(f"\nПроекция на {args.months} месяцев (приток новых клиентов в месяц / отток):")
    for name, new_per_month, churn in scenarios:
        scenario = project(
            name,
            months=args.months,
            new_per_month=new_per_month,
            churn=churn,
            net_per_user=net,
            costs=costs,
        )
        print(f"\n  {name}: +{new_per_month} клиентов/мес, отток {churn:.0%}, "
              f"предел роста ≈ {scenario.steady_users:.0f}")
        print("    мес | клиентов | выручка/мес | прибыль/мес")
        for row in scenario.rows:
            if row.month in {1, 3, 6, 9, 12} or row.month == args.months:
                print(f"    {row.month:>3} | {row.users:>8} | {money(row.revenue_rub):>11} | "
                      f"{money(row.profit_rub):>11}")
        print(f"    итог за {args.months} мес: оборот {money(scenario.total_revenue)}, "
              f"прибыль {money(scenario.total_profit)}")


if __name__ == "__main__":
    main()
