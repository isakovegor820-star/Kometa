"""Тесты экономики связки VPS: цены, себестоимость клиента, ёмкость, надёжность."""

from __future__ import annotations

import pathlib

import pytest

from app.services.finmodel import DEFAULT_PLANS, PlanMix, average_monthly_revenue
from app.services.finmodel_vps import (
    DEFAULT_AVERAGE_CHECK_RUB,
    POOL_INCIDENTS_PER_MONTH,
    DEFAULT_CHANNEL_FEE_PERCENT,
    DEFAULT_CONCURRENCY,
    DEFAULT_FAILURES,
    DEFAULT_REPLACEMENT_MONTHS,
    DEFAULT_NODE_KEY,
    DEFAULT_SCALE,
    LOAD_PROFILES,
    MAX_USERS_PER_NODE,
    MBPS_PER_VCPU,
    NODE_CLASSES,
    NODE_REPLACEMENT_MONTHS,
    OFFERS,
    OFFERS_BY_KEY,
    RATES,
    SCENARIOS,
    SCENARIOS_BY_KEY,
    STARTUP_ITEMS,
    ServerOffer,
    VpsPlan,
    availability,
    breakeven_users,
    cost_per_user_month,
    countries,
    countries_for_budget,
    country_break_even,
    downtime_minutes_per_month,
    expected_downtime_minutes,
    failure_minutes,
    days_to_traffic_cap,
    max_sessions_per_node,
    monthly_traffic_tb,
    multi_country_plan,
    next_country_price,
    node_capacity_users,
    nodes_for_users,
    peak_load,
    plan_node_purchase,
    replacement_cost_rub,
    scale_plan,
    scale_row,
    scale_table,
    replacement_months,
    rub,
    scope_share,
    schema_reliability,
    startup_cost,
)


# ---------------------------------------------------------------- валюта

def test_rub_converts_by_rate():
    assert rub(10, "EUR") == 10 * RATES["EUR"]
    assert rub(10, "USD") == 10 * RATES["USD"]
    assert rub(459, "RUB") == 459
    # курсы — снимок ЦБ РФ на 06.10.2026, а не «примерно»
    assert 90 < RATES["EUR"] < 100
    assert 80 < RATES["USD"] < 90


def test_rub_rejects_unknown_currency():
    with pytest.raises(ValueError, match="нет курса"):
        rub(10, "TRY")


def test_all_rates_positive():
    assert all(rate > 0 for rate in RATES.values())
    assert RATES["RUB"] == 1.0


# ---------------------------------------------------------------- офферы

def test_offer_keys_are_unique():
    keys = [offer.key for offer in OFFERS]

    assert len(keys) == len(set(keys))


def test_price_rub_uses_currency():
    aeza = OFFERS_BY_KEY["aeza-ams"]
    justhost = OFFERS_BY_KEY["justhost-ru"]

    assert aeza.price_rub == pytest.approx(5.93 * RATES["EUR"])
    assert justhost.price_rub == 351


def test_throughput_limited_by_cpu_not_only_port():
    """Порт 10 Гбит/с на одном ядре — это маркетинг: живого трафика там ~450 Мбит/с."""
    small = ServerOffer("t", "T", "L", 1.0, vcpu=1, port_mbps=10000)

    assert small.throughput_mbps == MBPS_PER_VCPU
    assert small.throughput_mbps < small.port_mbps


def test_throughput_limited_by_port_for_weak_port():
    offer = ServerOffer("t", "T", "L", 1.0, vcpu=8, port_mbps=100)

    assert offer.throughput_mbps == 100


# ---------------------------------------------------------------- ёмкость

def test_node_capacity_divides_throughput_by_load_profile():
    offer = ServerOffer("t", "T", "L", 1.0, vcpu=2, port_mbps=1000)  # 900 Мбит/с живых

    # 900 × 0.75 = 675 Мбит/с в пик ÷ 8 Мбит/с на сессию = 84 сессии;
    # при онлайне 25 % это 337 подписчиков по каналу, но практический потолок — 250
    assert node_capacity_users(offer) == MAX_USERS_PER_NODE == 250
    # при меньшей загрузке канала потолок не достигается — считаем по каналу
    assert node_capacity_users(offer, utilization=0.4) == 180


def test_heavier_profile_means_fewer_subscribers():
    offer = OFFERS_BY_KEY["aeza-waw"]
    light = node_capacity_users(offer, concurrency=0.15, mbps_per_session=5.0)
    heavy = node_capacity_users(offer, concurrency=0.35, mbps_per_session=25.0)

    assert light > heavy


def test_node_capacity_rejects_bad_profile():
    offer = OFFERS_BY_KEY["aeza-waw"]

    with pytest.raises(ValueError, match="скорость сессии"):
        node_capacity_users(offer, mbps_per_session=0)
    with pytest.raises(ValueError, match="доля онлайна"):
        node_capacity_users(offer, concurrency=1.5)
    with pytest.raises(ValueError, match="загрузка канала"):
        node_capacity_users(offer, utilization=1.5)


def test_peak_load_grows_with_users_and_crosses_one():
    offer = OFFERS_BY_KEY["aeza-waw"]

    assert peak_load(offer, 0) == 0
    assert peak_load(offer, 100) < 1
    assert peak_load(offer, 1000) > 1
    assert peak_load(offer, 100) == pytest.approx(
        100 * 0.25 * 8 / (offer.throughput_mbps * 0.75)
    )


def test_peak_load_rejects_negative_users():
    with pytest.raises(ValueError, match="меньше нуля"):
        peak_load(OFFERS_BY_KEY["aeza-waw"], -1)


def test_monthly_traffic_shows_traffic_tariff_risk():
    """Нода на гигабитном порту прокачивает сотни ТБ — «лимит 5 ТБ» сгорает за сутки."""
    melbicom = OFFERS_BY_KEY["melbicom-sto"]

    # 675 Мбит/с × 0,6 суток × 30 дней ≈ 131 ТБ/мес даже без круглосуточной загрузки
    assert monthly_traffic_tb(melbicom) == pytest.approx(131.22, abs=0.5)
    assert monthly_traffic_tb(melbicom) > 5


def test_days_to_traffic_cap_exposes_hidden_limits():
    """Сколько дней нода живёт до порога хоостера: у Melbicom — меньше суток."""
    melbicom = OFFERS_BY_KEY["melbicom-sto"]
    skrime = OFFERS_BY_KEY["skrime-nl"]

    assert days_to_traffic_cap(melbicom, 5.0) < 2
    assert days_to_traffic_cap(skrime, 5.0) < 2  # тот же канал: лимит не спасает
    assert days_to_traffic_cap(melbicom, 60.0) < 20


def test_days_to_traffic_cap_rejects_bad_arguments():
    offer = OFFERS_BY_KEY["aeza-waw"]

    with pytest.raises(ValueError, match="лимит"):
        days_to_traffic_cap(offer, 0)
    with pytest.raises(ValueError, match="загрузка суток"):
        days_to_traffic_cap(offer, 5, duty_cycle=0)


def test_monthly_traffic_rejects_bad_duty_cycle():
    with pytest.raises(ValueError, match="загрузка суток"):
        monthly_traffic_tb(OFFERS_BY_KEY["aeza-waw"], duty_cycle=0)


def test_load_profiles_are_sane():
    assert "обычный" in LOAD_PROFILES
    assert all(0 < concurrency <= 1 for concurrency, _ in LOAD_PROFILES.values())
    assert all(mbps > 0 for _, mbps in LOAD_PROFILES.values())


# ---------------------------------------------------------------- закупка нод

def test_node_purchase_meets_both_constraints():
    """Набор нод обязан покрыть и число подписчиков, и одновременные сессии."""
    for users in (0, 10, 100, 337, 500, 1000, 2000):
        purchase = plan_node_purchase(users)

        assert purchase.total_nodes >= 2, users
        assert purchase.capacity_subscribers >= users, users
        assert purchase.capacity_sessions >= users * DEFAULT_CONCURRENCY, users


def test_node_purchase_is_cheapest_available():
    """Под 600 клиентов перебор выбирает самый дешёвый набор, а не первый попавшийся."""
    purchase = plan_node_purchase(600)
    alternatives = [
        plan_node_purchase(600, classes={key: offer})
        for key, offer in NODE_CLASSES.items()
    ]

    assert purchase.total_nodes >= 2
    assert all(purchase.cost_rub <= other.cost_rub for other in alternatives)


def test_node_purchase_grows_with_users():
    small = plan_node_purchase(100)
    big = plan_node_purchase(2000)

    assert big.total_nodes > small.total_nodes
    assert big.cost_rub > small.cost_rub


def test_node_purchase_cost_per_subscriber_stays_low():
    """Цена мегабита у хостеров почти линейна: рост клиентов не удорожает клиента."""
    small = plan_node_purchase(100)
    big = plan_node_purchase(2000)

    assert big.cost_per_subscriber_rub <= small.cost_per_subscriber_rub * 1.05
    assert 1 < big.cost_per_subscriber_rub < 6


def test_node_purchase_rejects_bad_arguments():
    with pytest.raises(ValueError, match="меньше нуля"):
        plan_node_purchase(-1)
    with pytest.raises(ValueError, match="доля онлайна"):
        plan_node_purchase(100, concurrency=0)
    with pytest.raises(ValueError, match="минимум нод"):
        plan_node_purchase(100, min_nodes=-1)
    with pytest.raises(ValueError, match="каталог нод пуст"):
        plan_node_purchase(100, classes={})


def test_max_sessions_per_node_matches_capacity_and_concurrency():
    offer = ServerOffer("t", "T", "L", 1.0, vcpu=1, port_mbps=1000)  # 450 живых Мбит/с

    assert max_sessions_per_node(offer) == 42
    assert node_capacity_users(offer) == pytest.approx(
        max_sessions_per_node(offer) / DEFAULT_CONCURRENCY, abs=1
    )


def test_node_classes_cover_small_medium_large():
    assert set(NODE_CLASSES) == {"s", "m", "l"}
    throughputs = [offer.throughput_mbps for offer in NODE_CLASSES.values()]

    assert throughputs == sorted(throughputs)


def test_more_users_need_more_nodes():
    offer = OFFERS_BY_KEY["aeza-waw"]
    capacity = node_capacity_users(offer)

    assert nodes_for_users(10, offer, min_nodes=1) == 1
    assert nodes_for_users(capacity * 3, offer, min_nodes=1) == 3


def test_reserve_node_adds_headroom_for_failure():
    offer = OFFERS_BY_KEY["aeza-waw"]
    users = node_capacity_users(offer) * 2

    plain = nodes_for_users(users, offer, min_nodes=1)
    with_reserve = nodes_for_users(users, offer, min_nodes=1, reserve_nodes=1)

    assert with_reserve == plain + 1


def test_default_keeps_two_nodes_for_redundancy():
    """Даже на 5 клиентах схема держит две ноды: одна нода — одна точка отказа."""
    offer = OFFERS_BY_KEY["melbicom-sto"]

    assert nodes_for_users(5, offer) >= 2
    assert nodes_for_users(5, offer, min_nodes=1) == 1


# ---------------------------------------------------------------- сценарии

def test_scenario_keys_unique_and_default_exists():
    keys = [plan.key for plan in SCENARIOS]

    assert len(keys) == len(set(keys))
    assert DEFAULT_NODE_KEY in OFFERS_BY_KEY


def test_scenario_monthly_is_fixed_plus_reserve():
    plan = SCENARIOS_BY_KEY["A"]

    assert plan.fixed_rub == pytest.approx(plan.panel_rub + plan.nodes_rub)
    assert plan.monthly_rub == pytest.approx(plan.fixed_rub * 1.08)
    assert plan.yearly_rub == pytest.approx(plan.monthly_rub * 12)
    # Полная стоимость владения = счета + фонд замены нод, и она больше счетов
    assert plan.monthly_with_replacement_rub == pytest.approx(
        plan.monthly_rub + plan.replacement_rub
    )
    assert plan.monthly_with_replacement_rub > plan.monthly_rub


def test_current_schema_is_cheapest_and_single_node():
    now = SCENARIOS_BY_KEY["now"]
    plan_a = SCENARIOS_BY_KEY["A"]

    assert now.monthly_rub < plan_a.monthly_rub
    assert len(now.nodes) == 0
    # 459 ₽ + 8 % резерва
    assert now.monthly_rub == pytest.approx(459 * 1.08)
    # один сервер — он же панель — тоже требует фонда замены
    assert now.replacement_rub > 0


def test_full_schema_has_three_independent_nodes():
    plan = SCENARIOS_BY_KEY["A"]

    # netcup 2 vCPU + Skrime 2 vCPU (по 250 практического потолка) + BuyVM 1 vCPU (168)
    assert len(plan.nodes) == 3
    assert len({node.provider for node in plan.nodes}) == 3  # разные хостеры
    assert len({node.location for node in plan.nodes}) == 3  # разные локации
    assert plan.capacity_users == 668
    # вычитаем самую крупную ноду: 668 − 250 = 418
    assert plan.capacity_users_after_one_loss == 418


def test_scale_plan_never_bills_one_server_twice():
    """Строка таблицы должна равняться сумме компонентов, без дубля сервера."""
    for plan in SCENARIOS:
        for users in DEFAULT_SCALE:
            scaled = scale_plan(plan, users)
            row = scale_row(plan, users)
            components = (scaled.panel_rub + scaled.nodes_rub + scaled.reserve_rub
                          + scaled.replacement_rub)
            assert row.monthly_rub == pytest.approx(components), f"{plan.key}/{users}"
            if any(node.key == scaled.panel.key for node in scaled.nodes):
                # панель стоит на ноде — её цена уже внутри nodes
                if scaled.panel.key != plan.panel.key or plan.nodes:
                    assert scaled.panel_rub == 0, f"{plan.key}/{users}"


def test_capacity_after_loss_cannot_be_claimed_for_single_server():
    plan = SCENARIOS_BY_KEY["now"]

    assert plan.capacity_after_one_loss() == 0
    assert not scale_row(plan, 50).fits_after_one_loss
    assert not scale_row(plan, 100).fits_after_one_loss


def test_single_node_schema_has_no_capacity_after_loss():
    plan = SCENARIOS_BY_KEY["now"]

    assert plan.capacity_users_after_one_loss == 0
    # один сервер на 1 vCPU: 450 Мбит/с × 0,75 ÷ 8 Мбит/с ÷ 0,25 = 168 подписчиков
    assert plan.capacity_users == 168


def test_schemas_differ_in_nodes_and_price():
    """Схемы не должны быть клонами друг друга: смысл выбора — в разнице."""
    prices = {plan.key: round(plan.monthly_rub) for plan in SCENARIOS}

    assert len(set(prices.values())) == len(prices)


# ---------------------------------------------------------------- себестоимость

def test_cost_per_user_falls_with_growth():
    plan = SCENARIOS_BY_KEY["now"]

    assert cost_per_user_month(plan, 10) > cost_per_user_month(plan, 100)
    assert cost_per_user_month(plan, 1000) < cost_per_user_month(plan, 100)


def test_cost_per_user_contains_channel_fee():
    plan = SCENARIOS_BY_KEY["now"]
    users = 100
    fixed_only = plan.monthly_with_replacement_rub / users

    # комиссия канала — 10 % (8 % СБП + 2 % конвертации), а не 8 %
    assert cost_per_user_month(plan, users) == pytest.approx(
        fixed_only + DEFAULT_AVERAGE_CHECK_RUB * DEFAULT_CHANNEL_FEE_PERCENT / 100
    )


def test_cost_per_user_rejects_zero_users():
    with pytest.raises(ValueError, match="больше нуля"):
        cost_per_user_month(SCENARIOS_BY_KEY["now"], 0)


def test_breakeven_matches_average_check():
    plan = SCENARIOS_BY_KEY["now"]
    net = average_monthly_revenue(DEFAULT_PLANS, PlanMix()) * (1 - DEFAULT_CHANNEL_FEE_PERCENT / 100)

    # полная стоимость (счета + фонд замены) ÷ 98,7 ₽ на руки ≈ 6,6 клиента
    assert breakeven_users(plan, net_per_user=net) == pytest.approx(
        plan.monthly_with_replacement_rub / net
    )
    assert 5.5 < breakeven_users(plan, net_per_user=net) < 7.5


def test_breakeven_grows_for_full_schema():
    plan = SCENARIOS_BY_KEY["A"]
    now = SCENARIOS_BY_KEY["now"]

    assert breakeven_users(plan) > breakeven_users(now)
    assert 15 < breakeven_users(plan) < 50


def test_breakeven_rejects_zero_revenue():
    with pytest.raises(ValueError, match="больше нуля"):
        breakeven_users(SCENARIOS_BY_KEY["now"], net_per_user=0)


# ---------------------------------------------------------------- разовые расходы

def test_startup_cost_is_sum_of_items():
    assert startup_cost() == pytest.approx(sum(item.rub for item in STARTUP_ITEMS))
    assert startup_cost(required_only=True) <= startup_cost()


def test_startup_includes_domain_and_first_nodes():
    titles = " ".join(item.title for item in STARTUP_ITEMS)

    assert "Домен" in titles
    assert "нод" in titles


# ---------------------------------------------------------------- надёжность

def test_availability_matches_downtime_sum():
    """Доступность считается через сумму простоев — одна мера на всю модель."""
    for nodes, has_ru in ((0, False), (2, False), (3, False), (2, True)):
        downtime = expected_downtime_minutes(nodes=nodes, has_ru_node=has_ru)

        assert availability(nodes=nodes, has_ru_node=has_ru) == pytest.approx(
            1 - downtime / (24 * 60 * 30)
        )
    assert 0.99 < availability() < 1.0
    # РФ-нода добавляет свой режим отказа
    assert availability(nodes=2, has_ru_node=True) < availability(nodes=2)


def test_downtime_minutes_are_inverse_of_availability():
    assert downtime_minutes_per_month(1.0) == 0
    assert downtime_minutes_per_month(0.99) == pytest.approx(432)


def test_downtime_rejects_nonsense():
    for value in (0, -0.5, 1.5):
        with pytest.raises(ValueError, match="доступность"):
            downtime_minutes_per_month(value)


def test_more_nodes_means_better_availability():
    now = schema_reliability(SCENARIOS_BY_KEY["now"])
    plan_a = schema_reliability(SCENARIOS_BY_KEY["A"])

    assert plan_a.expected_availability > now.expected_availability
    assert plan_a.downtime_minutes < now.downtime_minutes
    assert now.nodes == 0 and plan_a.nodes == 3


def test_multi_node_schema_has_regions_and_notes():
    reliability = schema_reliability(SCENARIOS_BY_KEY["A"])

    assert reliability.regions >= 3
    assert not reliability.has_ru_node
    assert any("локации" in note for note in reliability.notes)


def test_russian_node_lowers_availability():
    """РФ-нода даёт пинг, но добавляет риск блокировки по требованию регулятора."""
    eu_only = schema_reliability(SCENARIOS_BY_KEY["C"])
    with_ru = schema_reliability(
        VpsPlan(
            key="ru-test",
            title="тест",
            panel=SCENARIOS_BY_KEY["C"].panel,
            nodes=(OFFERS_BY_KEY["justhost-ru"], OFFERS_BY_KEY["skrime-nl"]),
        )
    )

    assert not eu_only.has_ru_node
    assert with_ru.has_ru_node
    assert with_ru.expected_availability < eu_only.expected_availability


def test_worst_case_is_bigger_than_average_downtime():
    reliability = schema_reliability(SCENARIOS_BY_KEY["A"])

    assert reliability.worst_case_minutes > reliability.downtime_minutes


def test_schema_reliability_accepts_custom_plan():
    plan = VpsPlan(
        key="t",
        title="тест",
        panel=OFFERS_BY_KEY["aeza-ams"],
        nodes=(OFFERS_BY_KEY["aeza-waw"], OFFERS_BY_KEY["skrime-nl"]),
    )
    reliability = schema_reliability(plan)

    assert reliability.nodes == 2
    assert reliability.expected_availability > 0.995


# ---------------------------------------------------------------- фонд замены нод

def test_replacement_months_come_from_table_or_default():
    assert replacement_months(OFFERS_BY_KEY["melbicom-sto"]) == 3.0
    assert replacement_months(ServerOffer("unknown-host", "X", "L", 1.0)) == DEFAULT_REPLACEMENT_MONTHS


def test_replacement_cost_is_price_divided_by_lifetime():
    offer = OFFERS_BY_KEY["skrime-nl"]  # 531 ₽, живёт 4 месяца

    assert replacement_cost_rub(offer) == pytest.approx(offer.price_rub / 4)
    assert replacement_cost_rub(offer, months=2) == pytest.approx(offer.price_rub / 2)


def test_frequent_replacement_makes_cheap_host_more_expensive():
    """Срок жизни ноды важнее её прайса: у хостера из списков он короче."""
    filtered = OFFERS_BY_KEY["melbicom-sto"]  # живёт 3 месяца
    clean = OFFERS_BY_KEY["aeza-waw"]  # живёт 6 месяцев

    assert replacement_months(filtered) < replacement_months(clean)
    # при одинаковой цене более короткий срок = больший фонд замены
    assert replacement_cost_rub(clean, months=1) > replacement_cost_rub(clean, months=6)


def test_replacement_cost_rejects_zero_lifetime():
    with pytest.raises(ValueError, match="срок жизни"):
        replacement_cost_rub(OFFERS_BY_KEY["aeza-waw"], months=0)


def test_plan_replacement_fund_covers_all_nodes():
    plan = SCENARIOS_BY_KEY["A"]

    expected = sum(replacement_cost_rub(node) for node in plan.nodes)
    assert plan.replacement_rub == pytest.approx(expected)
    # фонд замены — заметная часть стоимости, а не мелочь
    assert 0.1 < plan.replacement_rub / plan.monthly_rub < 0.3


def test_single_server_plan_also_pays_replacement_fund():
    now = SCENARIOS_BY_KEY["now"]

    assert now.replacement_rub == pytest.approx(replacement_cost_rub(now.panel))


# ---------------------------------------------------------------- режимы отказа

def test_scope_share_divides_by_nodes():
    assert scope_share("node", 0) == 1.0  # один сервер = весь сервис
    assert scope_share("provider", 1) == 1.0
    # доля месяцев, в которые инцидент попал именно в твою ноду
    assert scope_share("node", 2) == pytest.approx(POOL_INCIDENTS_PER_MONTH / 2)
    assert scope_share("provider", 4) == pytest.approx(POOL_INCIDENTS_PER_MONTH / 4)
    assert scope_share("all", 3) == 1.0
    # при большом пуле доля не превышает единицу
    assert scope_share("node", 1) == 1.0


def test_scope_share_rejects_unknown_scope():
    with pytest.raises(ValueError, match="охват отказа"):
        scope_share("galaxy", 2)


def test_failure_minutes_follow_scope():
    mode = DEFAULT_FAILURES[0]  # 20 % × 2 ч = 24 минуты на весь сервис

    assert failure_minutes(mode, 0) == pytest.approx(24.0)
    assert failure_minutes(mode, 2) == pytest.approx(24.0 * POOL_INCIDENTS_PER_MONTH / 2)
    assert failure_minutes(mode, 3) == pytest.approx(24.0 * POOL_INCIDENTS_PER_MONTH / 3)


def test_expected_downtime_is_sum_of_modes():
    # для одного сервера (накрывает всё) — сумма всех режимов, включая РФ-ноду
    expected = sum(failure_minutes(mode, 0) for mode in DEFAULT_FAILURES)

    assert expected_downtime_minutes(nodes=0, has_ru_node=True) == pytest.approx(expected)
    # без РФ-ноды её режим не считается
    assert expected_downtime_minutes(nodes=0) == pytest.approx(
        expected - failure_minutes(next(m for m in DEFAULT_FAILURES if "регулятора" in m.title), 0)
    )
    # с двумя нодами ожидаемый простой меньше: отказ накрывает половину клиентов
    assert expected_downtime_minutes(nodes=2) < expected_downtime_minutes(nodes=0)


def test_russian_node_mode_skipped_without_ru_nodes():
    with_ru = expected_downtime_minutes(nodes=2, has_ru_node=True)
    without_ru = expected_downtime_minutes(nodes=2, has_ru_node=False)

    assert with_ru > without_ru


def test_more_nodes_means_less_expected_downtime():
    one = expected_downtime_minutes(nodes=1)
    three = expected_downtime_minutes(nodes=3)

    assert three < one
    # простой делится не ровно на число нод: инцидент может задеть несколько сразу
    assert three > one / 3
    assert three == pytest.approx(one * POOL_INCIDENTS_PER_MONTH / 3)


def test_bad_month_is_worse_than_average_and_better_than_total_loss():
    for key in ("now", "B", "A"):
        rel = schema_reliability(SCENARIOS_BY_KEY[key])

        assert rel.bad_month_minutes > rel.downtime_minutes
        assert rel.bad_month_minutes <= 24 * 60
        assert rel.expected_availability < 1


def test_single_server_bad_month_is_a_full_day_of_downtime():
    """Наш реальный кейс: единственный сервер лежал сутки — это и есть плохой месяц."""
    rel = schema_reliability(SCENARIOS_BY_KEY["now"])

    assert rel.bad_month_minutes == 24 * 60
    assert rel.worst_case_minutes == 6 * 60


def test_multi_node_bad_month_is_hours_not_days():
    rel = schema_reliability(SCENARIOS_BY_KEY["A"])

    assert rel.bad_month_minutes == 4 * 60
    assert rel.worst_case_minutes == 2 * 60


def test_schema_reliability_uses_failure_table():
    """Схема без нод считается по одному серверу, и это видно в цифрах."""
    one = schema_reliability(SCENARIOS_BY_KEY["now"])
    two = schema_reliability(SCENARIOS_BY_KEY["B"])

    assert one.downtime_minutes == pytest.approx(expected_downtime_minutes(nodes=0))
    assert two.downtime_minutes == pytest.approx(expected_downtime_minutes(nodes=2))
    assert two.expected_availability > one.expected_availability


# ---------------------------------------------------------------- инварианты масштабирования

def test_scale_plan_does_not_bill_single_server_twice():
    """Схема «один сервер»: сервер — это нода, а не нода + панель одновременно."""
    plan = SCENARIOS_BY_KEY["now"]
    scaled = scale_plan(plan, 100)

    assert len(scaled.nodes) == 1
    assert scaled.nodes[0].key == plan.panel.key  # тот же сервер
    assert scaled.panel_rub == 0  # второй раз его не считаем
    assert scaled.monthly_with_replacement_rub == pytest.approx(
        plan.monthly_with_replacement_rub
    )


def test_scale_plan_adds_node_when_single_server_runs_out():
    plan = SCENARIOS_BY_KEY["now"]
    capacity = plan.capacity_users

    assert scale_plan(plan, capacity).nodes == tuple([plan.panel])
    bigger = scale_plan(plan, capacity + 50)

    assert len(bigger.nodes) == 2
    # сервер по-прежнему один из нод, поэтому отдельной строкой за панель не платим
    assert bigger.panel_rub == 0
    assert bigger.nodes[0].key == plan.panel.key
    assert bigger.monthly_with_replacement_rub > plan.monthly_with_replacement_rub


def test_scale_plan_keeps_purchased_nodes():
    """Докупаем только нехватающую ёмкость, а не клонируем премиум-ноду."""
    plan = SCENARIOS_BY_KEY["A"]
    scaled = scale_plan(plan, 300, concurrency=0.35, mbps_per_session=25.0)

    keys = [node.key for node in scaled.nodes]
    for node in plan.nodes:
        assert keys.count(node.key) >= 1
    assert len(scaled.nodes) > len(plan.nodes)
    assert keys.count(plan.panel.key) == 0


def test_capacity_after_loss_never_exceeds_full_capacity():
    for plan in SCENARIOS:
        scaled = scale_plan(plan, 300)
        assert scaled.capacity_users_after_one_loss <= scaled.capacity_users


def test_capacity_after_loss_subtracts_the_largest_node():
    plan = SCENARIOS_BY_KEY["A"]

    assert plan.capacity_users_after_one_loss == plan.capacity_users - max(
        node_capacity_users(node) for node in plan.nodes
    )
    # в B самая крупная нода — Skrime, значит остаётся только Ava
    plan_b = SCENARIOS_BY_KEY["B"]
    assert plan_b.capacity_users_after_one_loss == node_capacity_users(plan_b.nodes[1])


def test_capacity_depends_on_profile():
    """Под торренты нода держит в разы меньше клиентов, чем под обычное видео.

    Сравниваем на одной ноде: у трёх нод срабатывает практический потолок
    (MAX_USERS_PER_NODE), и разница между профилями сглаживается.
    """
    offer = OFFERS_BY_KEY["skrime-nl"]
    light = node_capacity_users(offer, concurrency=0.15, mbps_per_session=5.0)
    heavy = node_capacity_users(offer, concurrency=0.35, mbps_per_session=25.0)

    assert light == MAX_USERS_PER_NODE  # лёгкий профиль упирается в потолок, а не в канал
    assert heavy < light
    plan = SCENARIOS_BY_KEY["A"]
    assert plan.capacity(concurrency=0.35, mbps_per_session=25.0) < plan.capacity()


def test_cli_and_document_use_same_net_revenue():
    """CLI и документ должны считать безубыточность по одной ставке канала."""
    from app.services.finmodel import DEFAULT_PLANS, PlanMix, average_monthly_revenue, net_after_partner_payout
    from app.services.finmodel_vps import switching_delta  # noqa: F401  (проверка импорта CLI-зависимостей)

    net = net_after_partner_payout(average_monthly_revenue(DEFAULT_PLANS, PlanMix()), 8.0)
    doc = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "РЕМНАВАВЕ-СВЯЗКА.md")
    if not doc.exists():
        pytest.skip("документ не найден")
    text = doc.read_text(encoding="utf-8")
    plan = SCENARIOS_BY_KEY["B"]
    expected = f"{plan.monthly_with_replacement_rub / net:.1f}".replace(".", ",")
    assert expected in text, f"документ не считает по ставке {net:.1f} ₽ (ищем {expected})"


def test_scale_row_fits_matches_capacity():
    """«Хватает» считается по ёмкости того же профиля, а не стоит всегда True."""
    plan = SCENARIOS_BY_KEY["B"]
    for users in (100, 400, 900):
        for concurrency, mbps in LOAD_PROFILES.values():
            row = scale_row(plan, users, concurrency=concurrency, mbps_per_session=mbps)
            scaled = scale_plan(plan, users, concurrency=concurrency, mbps_per_session=mbps)
            capacity = scaled.capacity(concurrency=concurrency, mbps_per_session=mbps)
            assert row.fits == (users <= capacity), f"{users}/{concurrency}/{mbps}"


def test_plan_rejects_negative_reserve():
    with pytest.raises(ValueError, match="резерв"):
        VpsPlan(key="x", title="x", panel=OFFERS_BY_KEY["skrime-nl"],
                nodes=(OFFERS_BY_KEY["skrime-nl"],), reserve_percent=-200)


def test_plan_rejects_node_without_throughput():
    dead = ServerOffer("dead", "X", "L", 100.0, vcpu=0, port_mbps=0)

    with pytest.raises(ValueError, match="пропускн"):
        VpsPlan(key="x", title="x", panel=OFFERS_BY_KEY["skrime-nl"], nodes=(dead,))


def test_zero_throughput_is_rejected_everywhere():
    dead = ServerOffer("dead", "X", "L", 100.0, vcpu=0, port_mbps=0)

    with pytest.raises(ValueError, match="пропускн"):
        node_capacity_users(dead)
    with pytest.raises(ValueError, match="пропускн"):
        peak_load(dead, 10)


def test_peak_load_and_traffic_reject_bad_input():
    offer = OFFERS_BY_KEY["skrime-nl"]

    with pytest.raises(ValueError, match="скорость сессии"):
        peak_load(offer, 10, mbps_per_session=0)
    with pytest.raises(ValueError, match="загрузка канала"):
        peak_load(offer, 10, utilization=0)
    with pytest.raises(ValueError, match="загрузка канала"):
        monthly_traffic_tb(offer, utilization=-0.5)


def test_every_offer_has_explicit_replacement_lifetime():
    """Молчаливый дефолт скрывал бы, что срок жизни ноды никто не оценил."""
    unset = [offer.key for offer in OFFERS if offer.key.split("-")[0] not in NODE_REPLACEMENT_MONTHS]

    assert not unset, f"нет срока жизни для: {unset}"


def test_document_is_byte_exact_build():
    """Документ должен совпадать со свежей сборкой генератора, а не «примерно»."""
    import importlib.util

    root = pathlib.Path(__file__).resolve().parent.parent
    doc_path = root / "docs" / "РЕМНАВАВЕ-СВЯЗКА.md"
    script = root / "scripts" / "build_remnawave_doc.py"
    if not (doc_path.exists() and script.exists()):
        pytest.skip("нет документа или генератора")
    spec = importlib.util.spec_from_file_location("build_remnawave_doc", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert doc_path.read_text(encoding="utf-8") == module.build(), (
        "документ разошёлся со сборкой: запусти scripts/build_remnawave_doc.py"
    )


def test_availability_helper_agrees_with_schema_reliability():
    """Две меры надёжности не должны расходиться: одна нода = весь сервис."""
    one_server = expected_downtime_minutes(nodes=0)
    expected = 1 - one_server / (24 * 60 * 30)

    assert availability() == pytest.approx(expected, abs=1e-9)
    assert availability(DEFAULT_FAILURES[:3]) == pytest.approx(
        1 - expected_downtime_minutes(DEFAULT_FAILURES[:3], nodes=0) / (24 * 60 * 30), abs=1e-9
    )


def test_document_matches_model():
    """Защита от дрейфа: цифры в документе должны совпадать с моделью."""
    import re
    from pathlib import Path

    doc_path = Path(__file__).resolve().parent.parent / "docs" / "РЕМНАВАВЕ-СВЯЗКА.md"
    if not doc_path.exists():  # документ может отсутствовать в чужой сборке
        pytest.skip("документ РЕМНАВАВЕ-СВЯЗКА.md не найден")
    doc = doc_path.read_text(encoding="utf-8")

    # схемы: полная стоимость месяца и год
    for plan in SCENARIOS:
        monthly = f"{plan.monthly_with_replacement_rub:,.0f}".replace(",", " ")
        yearly = f"{plan.monthly_with_replacement_rub * 12:,.0f}".replace(",", " ")
        assert monthly in doc, f"{plan.key}: нет {monthly} ₽/мес"
        assert yearly in doc, f"{plan.key}: нет {yearly} ₽/год"

    # шкала клиентов: все строки таблиц, включая текущую схему
    for key in ("now", "B", "C", "A", "D"):
        for row in scale_table(SCENARIOS_BY_KEY[key]):
            monthly = f"{row.monthly_rub:,.0f}".replace(",", " ")
            yearly = f"{row.yearly_rub:,.0f}".replace(",", " ")
            per_user = f"{row.per_user_rub:.1f}".replace(".", ",")
            pattern = (rf"\|\s*{row.users}\s*\|\s*{row.nodes}\s*\|\s*{re.escape(monthly)}\s*\|"
                       rf"\s*{re.escape(yearly)}\s*\|\s*{re.escape(per_user)}\s*\|")
            assert re.search(pattern, doc), f"шкала {key}/{row.users}: строка не совпадает"

    # доступность схем
    for plan in SCENARIOS:
        avail = f"{schema_reliability(plan).expected_availability:.2%}".replace(".", ",")
        assert avail.replace("%", " %") in doc or avail in doc, f"{plan.key}: нет {avail}"


# ---------------------------------------------------------------- мультигео

def test_country_catalog_has_one_offer_per_country():
    from app.services.finmodel_vps import COUNTRY_CATALOG

    names = [name for name, _, _ in COUNTRY_CATALOG]

    assert len(names) == len(set(names)), "страна не должна повторяться в каталоге"
    assert all(key in OFFERS_BY_KEY for _, key, _ in COUNTRY_CATALOG)


def test_country_catalog_covers_all_waves():
    from app.services.finmodel_vps import COUNTRY_WAVES

    priorities = {option.priority for option in countries()}

    assert priorities == {1, 2, 3, 4}
    assert len(COUNTRY_WAVES) == 4


def test_multi_country_plan_is_one_server_per_country():
    for count in (1, 3, 6, len(countries())):
        plan = multi_country_plan(count)

        assert len(plan.nodes) == count
        assert len({node.key for node in plan.nodes}) == count, "страна = отдельный сервер"
        assert plan.panel.key != plan.nodes[0].key or count == 1


def test_more_countries_cost_more_but_add_capacity():
    small = multi_country_plan(2)
    big = multi_country_plan(6)

    assert big.monthly_with_replacement_rub > small.monthly_with_replacement_rub
    assert big.capacity_users > small.capacity_users
    assert country_break_even(6) > country_break_even(2)


def test_each_new_country_costs_about_one_server():
    """Добавление страны — линейная статья: примерно цена ещё одного сервера."""
    for count in (1, 3, 5, 9):
        add = next_country_price(count)

        assert 400 < add < 3_600, f"{count} → {count + 1}: {add:.0f} ₽"


def test_countries_for_budget_is_consistent():
    for budget in (3_000, 5_000, 8_000):
        count = countries_for_budget(budget)
        plan = multi_country_plan(count)

        assert plan.monthly_with_replacement_rub <= budget
        if count < len(countries()):
            assert multi_country_plan(count + 1).monthly_with_replacement_rub > budget


def test_multi_country_plan_rejects_impossible_counts():
    with pytest.raises(ValueError, match="минимум"):
        multi_country_plan(0)
    with pytest.raises(ValueError, match="только"):
        multi_country_plan(len(countries()) + 1)
    with pytest.raises(ValueError, match="бюджет"):
        countries_for_budget(0)
