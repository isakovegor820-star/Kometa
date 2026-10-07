"""CLI: сколько стоит связка «панель + ноды» и когда она окупается.

Запуск:
    .venv/bin/python -m app.tools.vps_costs                  # все схемы, шкала, режимы отказа
    .venv/bin/python -m app.tools.vps_costs --users 100      # что покупать под 100 клиентов
    .venv/bin/python -m app.tools.vps_costs --plan A --full  # одна схема, все масштабы
    .venv/bin/python -m app.tools.vps_costs --fx 100         # пересчёт по другому курсу EUR

Цифры отсюда попадают в docs/РЕМНАВАВЕ-СВЯЗКА.md: документ собирается генератором
`scripts/build_remnawave_doc.py` из той же модели. Если правишь прайс в
`app/services/finmodel_vps.py`, прогони оба инструмента.
"""

from __future__ import annotations

import argparse

from app.services.finmodel import DEFAULT_PLANS, PlanMix, average_monthly_revenue
from app.services.finmodel_vps import (
    DEFAULT_CHANNEL_FEE_PERCENT,
    monthly_traffic_tb,
    node_capacity_users,
    DEFAULT_FAILURES,
    DEFAULT_NET_PER_USER,
    LOAD_PROFILES,
    NODE_CLASSES,
    NODE_CLASSES_EU,
    RATES,
    SCENARIOS,
    SCENARIOS_BY_KEY,
    STARTUP_ITEMS,
    breakeven_users,
    failure_minutes,
    peak_load,
    plan_node_purchase,
    replacement_cost_rub,
    scale_row,
    scale_table,
    schema_reliability,
    startup_cost,
    switching_delta,
)
from app.services.finmodel_vps import (  # мультигео-планирование
    COUNTRY_WAVES,
    countries,
    country_break_even,
    countries_for_budget,
    multi_country_plan,
    next_country_price,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Затраты на VPS под VPN-сервис")
    parser.add_argument("--users", type=int, help="подробный расчёт под такое число клиентов")
    parser.add_argument("--plan", choices=[plan.key for plan in SCENARIOS], help="схема развёртывания")
    parser.add_argument("--fx", type=float, help="курс EUR в рублях (по умолчанию из модели)")
    parser.add_argument("--full", action="store_true", help="печатать всю шкалу клиентов")
    parser.add_argument("--countries", type=int, metavar="N",
                        help="сколько стоит мультигео из N стран")
    return parser.parse_args()


def money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def print_countries(net_per_user: float, count: int | None = None) -> None:
    """Сколько стран помещается в бюджет и что стоит каждая следующая."""
    print("\nМультигео: одна страна = один сервер. Цены с фондом замены и панелью.")

    print("\nВолны запуска:")
    print(f"  {'волна':<20} {'страны':<58} зачем")
    for wave, names, why in COUNTRY_WAVES:
        print(f"  {wave:<20} {names:<58} {why}")

    print("\nКаталог стран (полная стоимость месяца):")
    print(f"  {'страна':<34} {'сервер':<20} {'₽/мес':>8} {'ёмкость':>8}")
    for option in sorted(countries(), key=lambda o: (o.priority, o.full_rub)):
        print(f"  {option.country:<34} {option.offer.provider:<20} "
              f"{option.full_rub:>8,.0f} {option.capacity_users:>8}")

    print("\nСколько стран — сколько денег:")
    print(f"  {'стран':>6} {'₽/мес':>9} {'₽/год':>10} {'+1 страна':>10} {'окупается с':>12} {'ёмкость':>8}")
    for n in range(1, len(countries()) + 1):
        plan = multi_country_plan(n)
        add = next_country_price(n) if n < len(countries()) else 0.0
        print(f"  {n:>6} {plan.monthly_with_replacement_rub:>9,.0f} "
              f"{plan.monthly_with_replacement_rub * 12:>10,.0f} "
              f"{('+' + format(add, ',.0f').replace(',', ' ')) if add else '—':>10} "
              f"{country_break_even(n):>12,.1f} {plan.capacity_users:>8}")

    if count:
        plan = multi_country_plan(count)
        print(f"\nСхема на {count} стран — {money(plan.monthly_with_replacement_rub)}/мес:")
        for node in plan.nodes:
            full = node.price_rub + replacement_cost_rub(node)
            print(f"  {node.provider:<18} {node.location:<26} {money(full):>9} "
                  f"| ёмкость {node_capacity_users(node)} кл. | {monthly_traffic_tb(node):.0f} ТБ/мес")
        print(f"  {'панель (отдельно)':<18} {plan.panel.location:<26} {money(plan.panel.price_rub):>9}")
        print(f"  ёмкость всех стран: {plan.capacity_users} клиентов, "
              f"окупается с {country_break_even(count):.1f} клиента")

    print("\nОриентир по бюджету: за 5 000 ₽/мес помещается "
          f"{countries_for_budget(5000)} стран, за 8 000 ₽ — {countries_for_budget(8000)}.")
    print("Что не решает много стран: полное отключение интернета и белый список по IP — "
          "это не про количество локаций.")


def print_overview(net_per_user: float) -> None:
    """net_per_user — сколько доходит с клиента (8 % СБП + 2 % конвертации)."""
    print(f"Курсы ЦБ РФ на 06.10.2026: EUR = {RATES['EUR']:.4f} ₽, USD = {RATES['USD']:.4f} ₽\n")
    header = (f"{'схема':<4} {'нод':>4} {'счета ₽/мес':>12} {'фонд замены':>12} "
              f"{'полная ₽/мес':>13} {'полная ₽/год':>13} {'окупается с':>12} {'доступность':>12}")
    print(header)
    print("-" * len(header))
    for plan in SCENARIOS:
        reliability = schema_reliability(plan)
        nodes = len(plan.nodes) or 1  # у схемы «сейчас» сервер один и он же нода
        print(
            f"{plan.key:<4} {nodes:>4} {plan.monthly_rub:>12,.0f} {plan.replacement_rub:>12,.0f} "
            f"{plan.monthly_with_replacement_rub:>13,.0f} {plan.monthly_with_replacement_rub * 12:>13,.0f} "
            f"{breakeven_users(plan, net_per_user=net_per_user):>12,.1f} "
            f"{reliability.expected_availability:>11.3%}"
        )
    print("\n«счета» — что приходит в счетах (включая 8 % резерва на мелочи).")
    print("«фонд замены» — сколько откладывать на пересоздание нод: их меняют не «если», а «когда».")
    print(f"«полная» — реальная стоимость схемы в месяц; «окупается с» — клиентов при "
          f"{net_per_user:,.0f} ₽ на руки с клиента.")
    print("У схемы «сейчас» сервер один: он же панель, поэтому отдельной строки за панель нет.")

    print("\nРежимы отказа: сколько минут в месяц теряет один клиент:")
    print(f"  {'режим отказа':<52} {'1 сервер':>9} {'2 ноды':>8} {'3 ноды':>8}")
    for mode in DEFAULT_FAILURES:
        print(f"  {mode.title[:52]:<52} {failure_minutes(mode, 0):>8.1f}м "
              f"{failure_minutes(mode, 2):>7.1f}м {failure_minutes(mode, 3):>7.1f}м")
    print("  (режим «блокировка РФ-ноды» в итог не входит: в наших схемах РФ-нод нет)")

    print("\nПлохой месяц (один реальный инцидент, а не среднее):")
    for plan in SCENARIOS:
        reliability = schema_reliability(plan)
        print(f"  {plan.key:<4} обычный {reliability.downtime_minutes:>6.1f} мин "
              f"({reliability.expected_availability:.3%}) | "
              f"плохой {reliability.bad_month_minutes:>5.0f} мин "
              f"({1 - reliability.bad_month_minutes / (24 * 60 * 30):.1%}) | "
              f"переезд руками {reliability.worst_case_minutes:>4.0f} мин")

    print("\nСколько добавляет переход к текущей схеме (полная стоимость, ₽/мес):")
    base = SCENARIOS_BY_KEY["now"]
    for plan in SCENARIOS:
        if plan.key == base.key:
            continue
        delta = switching_delta(plan, baseline=base)
        print(f"  {plan.key}: {delta:+,.0f} ₽/мес ({delta * 12:+,.0f} ₽/год)")


def print_scale(plans: list) -> None:
    for plan in plans:
        print(f"\n{plan.title}")
        if plan.purpose:
            print(f"  зачем: {plan.purpose}")
        print("  клиентов | нод | ₽/мес | ₽/год | ₽ на клиента | окупается с | маржа | хватает | после аварии")
        for row in scale_table(plan):
            print(
                f"  {row.users:>8} | {row.nodes:>3} | {row.monthly_rub:>6,.0f} | {row.yearly_rub:>7,.0f} "
                f"| {row.per_user_rub:>12,.1f} | {row.breakeven_users:>11,.1f} "
                f"| {row.margin_percent:>4,.0f}% | {'да' if row.fits else 'нет':>7} "
                f"| {'да' if row.fits_after_one_loss else 'нет':>10}"
            )


def print_users_detail(users: int) -> None:
    concurrency, mbps = LOAD_PROFILES["обычный"]
    print(f"\nПод {users} клиентов (профиль «обычный»: онлайн {concurrency:.0%}, "
          f"{mbps:.0f} Мбит/с на активную сессию):")
    for title, classes in (
        ("дешевле всего", None),
        ("только зарубежный безлимит (рекомендуемый набор)", NODE_CLASSES_EU),
    ):
        purchase = plan_node_purchase(users, classes=classes)
        print(f"\n  {title}:")
        for key, count in purchase.counts.items():
            offer = NODE_CLASSES[key]
            print(f"    {count} × {offer.provider} {offer.location}: {offer.vcpu} vCPU / "
                  f"{offer.ram_gb} ГБ / {offer.price_rub:,.0f} ₽ → {offer.throughput_mbps:,.0f} Мбит/с живых")
        print(f"    итого {purchase.total_nodes} нод, {money(purchase.cost_rub)}/мес; "
              f"ёмкость {purchase.capacity_subscribers} подписчиков / "
              f"{purchase.capacity_sessions} одновременных сессий")
    print("\n  профиль нагрузки → сколько нужно нод и как загружен канал:")
    for name, (profile_concurrency, profile_mbps) in LOAD_PROFILES.items():
        purchase = plan_node_purchase(
            users, classes=NODE_CLASSES_EU,
            concurrency=profile_concurrency, mbps_per_session=profile_mbps,
        )
        offer = NODE_CLASSES["m"]
        per_node = purchase.capacity_subscribers // max(purchase.total_nodes, 1)
        load = peak_load(
            offer, min(users, per_node),
            concurrency=profile_concurrency, mbps_per_session=profile_mbps,
        )
        print(f"    {name:<15} онлайн {profile_concurrency:.0%}, {profile_mbps:>4.0f} Мбит/с → "
              f"{purchase.total_nodes} нод, {money(purchase.cost_rub)}/мес, "
              f"загрузка канала {load:.0%} на ноду")


def main() -> None:
    args = parse_args()
    if args.fx:
        old_eur = RATES["EUR"]
        RATES["EUR"] = args.fx
        RATES["USD"] = args.fx * RATES["USD"] / old_eur

    net = average_monthly_revenue(DEFAULT_PLANS, PlanMix())
    net_hand = DEFAULT_NET_PER_USER  # 182 ₽ чека минус 8 % СБП и 2 % конвертации
    print(f"Средний чек: {net:,.0f} ₽/мес с клиента; на руки после СБП 8 % и "
          f"конвертации 2 %: {net_hand:,.0f} ₽\n")

    if args.countries:
        print_countries(net_hand, args.countries)
        return

    if args.users:
        print_users_detail(args.users)

    plans = [SCENARIOS_BY_KEY[args.plan]] if args.plan else list(SCENARIOS)
    print_overview(net_hand)
    print_scale(plans if (args.full or args.plan) else plans[:2])

    print("\nРазовые расходы на старт связки:")
    for item in STARTUP_ITEMS:
        flag = "обязательно" if item.required else "опционально"
        print(f"  {item.title:<52} {money(item.rub):>9}  ({flag})")
    print(f"  {'ИТОГО минимум':<52} {money(startup_cost(required_only=True)):>9}")
    print(f"  {'ИТОГО с проверкой операторов':<52} {money(startup_cost()):>9}")
    print(f"\nСхема B стоит {money(scale_row(SCENARIOS_BY_KEY['B'], 100).monthly_rub)}/мес и окупается "
          f"с {breakeven_users(SCENARIOS_BY_KEY['B'], net_per_user=net_hand):.1f} клиента.")
    print("Шкала клиентов для остальных схем: --full")


if __name__ == "__main__":
    main()
