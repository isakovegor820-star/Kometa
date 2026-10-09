"""Финансовая модель сервиса: чистые функции без БД и сети.

Зачем отдельный модуль: «сколько мы заработаем» должно считаться по формулам,
а не на глаз. Здесь — цена тарифов, средний чек, точка безубыточности,
ёмкость нод и проекция роста с учётом оттока.

Все функции чистые: их легко тестировать и безопасно вызывать из CLI.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Plan:
    """Тариф: сколько стоит и на сколько дней."""

    code: str
    title: str
    price_rub: int
    days: int

    @property
    def per_month(self) -> float:
        """Цена за месяц при оплате этого тарифа."""
        return self.price_rub / self.days * 30


#: Наши тарифы (те же, что в БД — здесь для расчётов).
#: Сетка от 08.10.2026: 120 ₽ за месяц и пропорционально дешевле на длинных
#: тарифах (120 / 100 / 90 / 79 ₽ в месяц). Разбор решения — docs/РЕВЬЮ-ЦЕНЫ-120.md.
DEFAULT_PLANS: tuple[Plan, ...] = (
    Plan("m1", "1 месяц", 120, 30),
    Plan("m3", "3 месяца", 299, 90),
    Plan("m6", "6 месяцев", 539, 180),
    Plan("m12", "12 месяцев", 959, 365),
)


@dataclass(frozen=True, slots=True)
class PlanMix:
    """Какую долю покупателей какой тариф выбирает (сумма = 1)."""

    monthly: float = 0.50
    quarterly: float = 0.25
    half_year: float = 0.15
    yearly: float = 0.10

    def as_list(self) -> list[float]:
        return [self.monthly, self.quarterly, self.half_year, self.yearly]

    @property
    def total(self) -> float:
        return sum(self.as_list())


@dataclass(slots=True)
class Costs:
    """Постоянные расходы в месяц."""

    node_rub: int = 459
    nodes: int = 1
    domain_rub_per_month: float = 0.0
    other_rub: float = 0.0

    @property
    def monthly(self) -> float:
        return self.node_rub * self.nodes + self.domain_rub_per_month + self.other_rub


@dataclass(slots=True)
class MonthRow:
    month: int
    users: int
    new_users: int
    churned: int
    revenue_rub: float
    costs_rub: float
    profit_rub: float


@dataclass(slots=True)
class Scenario:
    """Сценарий роста: сколько платящих и сколько денег."""

    name: str
    new_per_month: int
    churn: float
    rows: list[MonthRow] = field(default_factory=list)

    @property
    def steady_users(self) -> float:
        """Предел роста при постоянном притоке и оттоке."""
        return self.new_per_month / self.churn if self.churn else float("inf")

    @property
    def total_revenue(self) -> float:
        return sum(row.revenue_rub for row in self.rows)

    @property
    def total_profit(self) -> float:
        return sum(row.profit_rub for row in self.rows)


# ---------------------------------------------------------------- базовые расчёты
def average_monthly_revenue(
    plans: tuple[Plan, ...] = DEFAULT_PLANS,
    mix: PlanMix | None = None,
    *,
    stars_net_factor: float = 1.03,
) -> float:
    """Средняя выручка с одного платящего клиента в месяц.

    :param stars_net_factor: поправка на то, что оплата звёздами даёт чуть
        больше рублёвой цены (по нашей сетке ≈ +3 %). 1.0 — если платят рублями.
    """
    mix = mix or PlanMix()
    if len(mix.as_list()) != len(plans):
        raise ValueError("количество долей в миксе не совпадает с числом тарифов")
    if abs(mix.total - 1.0) > 1e-6:
        raise ValueError(f"сумма долей микса должна быть 1, а не {mix.total}")

    per_month = sum(share * plan.per_month for share, plan in zip(mix.as_list(), plans))
    return per_month * stars_net_factor


def break_even_users(net_per_user: float, costs: Costs) -> float:
    """Сколько платящих нужно, чтобы покрыть постоянные расходы."""
    if net_per_user <= 0:
        raise ValueError("выручка с клиента должна быть больше нуля")
    return costs.monthly / net_per_user


#: Комиссии каналов оплаты в процентах от оборота — условия партнёра
#: от 06.10.2026. Процент СБП зависит от объёма оборотов и снижается
#: по мере роста (пересматривается с партнёром).
CHANNEL_FEES: dict[str, float] = {
    "sbp": 8.0,  # СБП НСПК: QR-код или оплата по ссылке из банковского приложения
    "crypto": 5.0,  # криптоплатежи через платёжный сервис
    "stars": 5.0,  # вывод звёзд через Fragment
}

#: Наценка банка на конвертацию рублей в USDT при выплатах (Rapira), %.
#: Партнёр своей комиссии за конвертацию не берёт — обмен делает банк,
#: поэтому эта наценка вычитается из каждой выплаты.
CONVERSION_FEE_PERCENT: float = 2.0


def net_after_channel_fee(amount_rub: float, fee_percent: float) -> float:
    """Сколько остаётся с чека после комиссии канала оплаты.

    Нужно, чтобы «средний чек» в модели не выдавал желаемое за действительное:
    с тарифа 120 ₽ при комиссии 8 % на руки приходит 110 ₽, а не 120 ₽.
    """
    if not 0 <= fee_percent < 100:
        raise ValueError("комиссия должна быть в диапазоне [0, 100)")
    return amount_rub * (1 - fee_percent / 100)


def net_after_partner_payout(
    amount_rub: float,
    channel_fee_percent: float,
    conversion_percent: float = CONVERSION_FEE_PERCENT,
) -> float:
    """Сколько реально доходит до нас: комиссия канала + конвертация в USDT.

    Партнёр удерживает процент с оборота (8 % по СБП), а затем банк конвертирует
    рубли в USDT с наценкой 2 %. Итог по СБП — около 9,8 % от чека.
    """
    after_channel = net_after_channel_fee(amount_rub, channel_fee_percent)
    return net_after_channel_fee(after_channel, conversion_percent)


def capacity_users(nodes: int = 1, *, per_node_min: int = 30, per_node_max: int = 60) -> tuple[int, int]:
    """Сколько клиентов выдержат ноды (по опыту: 1 vCPU ≈ 30–60 активных)."""
    return per_node_min * nodes, per_node_max * nodes


def nodes_needed(users: int, *, per_node: int = 45) -> int:
    """Сколько нод нужно под такое число клиентов."""
    return max(1, -(-users // per_node))


def project(
    name: str,
    *,
    months: int = 12,
    new_per_month: int,
    churn: float,
    net_per_user: float,
    costs: Costs,
    per_node: int = 45,
) -> Scenario:
    """Смоделировать рост: приток новых, отток старых, расходы на ноды."""
    scenario = Scenario(name=name, new_per_month=new_per_month, churn=churn)
    users = 0.0
    for month in range(1, months + 1):
        churned = users * churn
        users = users - churned + new_per_month
        rounded_users = int(round(users))
        active_costs = Costs(
            node_rub=costs.node_rub,
            nodes=nodes_needed(rounded_users, per_node=per_node),
            domain_rub_per_month=costs.domain_rub_per_month,
            other_rub=costs.other_rub,
        )
        revenue = rounded_users * net_per_user
        scenario.rows.append(
            MonthRow(
                month=month,
                users=rounded_users,
                new_users=new_per_month,
                churned=int(round(churned)),
                revenue_rub=revenue,
                costs_rub=active_costs.monthly,
                profit_rub=revenue - active_costs.monthly,
            )
        )
    return scenario


def users_for_revenue(target_rub: float, net_per_user: float) -> int:
    """Сколько платящих нужно для заданной месячной выручки."""
    if net_per_user <= 0:
        raise ValueError("выручка с клиента должна быть больше нуля")
    return int(-(-target_rub // net_per_user))
