"""Экономика привлечения: сколько стоит клиент и сколько за него можно платить.

Зачем отдельный модуль. Любая акция — скидка, бонусные дни, подарок, приз в
конкурсе, платное размещение — это расход. Пока он не посчитан в рублях, спор
идёт о вкусах: «дорого», «нормально», «давай попробуем». Здесь всё сводится к
одной величине: **во сколько обошёлся один новый ПЛАТЯЩИЙ клиент** и
укладывается ли это в потолок, который выдерживает экономика сервиса.

Опорные величины (сетка от 08.10.2026, разбор — docs/ФИНМОДЕЛЬ.md):

  * средний чек — 109 ₽/мес с клиента (`average_monthly_revenue`);
  * на руки после канала оплаты — 98,7 ₽ (СБП 8 % + 2 % конвертации);
  * срок жизни клиента при оттоке 15 %/мес — 6,7 мес, LTV ≈ 658 ₽;
  * нода 459 ₽/мес, точка безубыточности — 4,65 платящих.

Главное правило, которое модуль защищает: **скидка — это не «скидка», а
потраченные деньги**. Скидка 50 % на годовой тариф (959 ₽) стоит 480 ₽ — это
дороже, чем потолок привлечения, посчитанный от LTV. Поэтому у скидки обязана
быть планка в рублях, а у каждой акции — расчёт до её запуска.

Все функции чистые: без БД, сети и побочных эффектов. Крутить параметры можно
из CLI: ``python -m app.tools.marketing``.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.finmodel import DEFAULT_PLANS, Plan, PlanMix, average_monthly_revenue

#: Ставка канала оплаты в процентах: СБП партнёра 8 % плюс конвертация 2 %.
DEFAULT_FEE_PERCENT: float = 10.0

#: Доля выручки, которую допустимо отдать за привлечение одного клиента.
#: 1/3 LTV — консервативно (клиент окупает себя за ~2,2 месяца), 1/2 — предел,
#: за которым рост начинает съедать прибыль.
HEALTHY_CAC_SHARE: float = 1 / 3
MAX_CAC_SHARE: float = 1 / 2


@dataclass(frozen=True, slots=True)
class UnitEconomics:
    """Базовые величины «сколько клиент приносит».

    :param arpu_rub: средний чек в месяц с одного платящего клиента.
    :param fee_percent: комиссия канала оплаты (включая конвертацию), %.
    :param churn: месячный отток, доля (0.15 = 15 %).
    :param node_rub: стоимость одной ноды в месяц.
    :param users_per_node: сколько платящих держит одна нода.
    """

    arpu_rub: float = 109.0
    fee_percent: float = DEFAULT_FEE_PERCENT
    churn: float = 0.15
    node_rub: float = 459.0
    users_per_node: float = 45.0

    @property
    def net_rub(self) -> float:
        """Сколько остаётся с клиента в месяц после канала оплаты."""
        return self.arpu_rub * (1 - self.fee_percent / 100)

    @property
    def lifetime_months(self) -> float:
        """Средний срок жизни клиента в месяцах."""
        if self.churn <= 0:
            return float("inf")
        return 1 / self.churn

    @property
    def ltv_rub(self) -> float:
        """Сколько денег клиент принесёт за всю жизнь (на руки)."""
        return self.net_rub * self.lifetime_months

    @property
    def marginal_cost_rub(self) -> float:
        """Себестоимость обслуживания одного клиента в месяц.

        Нода платится целиком и делится на всех, кто на ней сидит: рост числа
        клиентов почти не увеличивает расходы, пока не куплена следующая нода.
        Именно поэтому бонусные дни в подписке стоят почти ноль, а скидка
        рублями — стоит полную сумму.
        """
        return self.node_rub / self.users_per_node

    @property
    def break_even_users(self) -> float:
        """Сколько платящих нужно, чтобы покрыть одну ноду."""
        return self.node_rub / self.net_rub

    def cac_ceiling_rub(self, share: float = HEALTHY_CAC_SHARE) -> float:
        """Потолок расходов на привлечение одного платящего клиента."""
        return self.ltv_rub * share

    def payback_months(self, cac_rub: float) -> float:
        """За сколько месяцев клиент окупает свою стоимость привлечения."""
        if self.net_rub <= 0:
            return float("inf")
        return cac_rub / self.net_rub

    def months_of_service_rub(self, months: float) -> float:
        """Сколько недополученной выручки стоит N бесплатных месяцев.

        Так считаются бонусные дни, призы в конкурсах и «месяц в подарок»:
        мы отдаём не деньги, а время подписки.
        """
        return self.net_rub * months


@dataclass(frozen=True, slots=True)
class ReferralTerms:
    """Условия реферальной программы — то, что можно менять в настройках.

    :param invited_discount_percent: скидка приглашённому на первую оплату.
    :param discount_cap_rub: планка скидки в рублях (0 = без планки).
    :param referrer_days: сколько дней получает пригласивший за оплату друга.
    :param invited_bonus_days: сколько дней получает приглашённый сверху.
    :param plan: по какому тарифу считается первая оплата друга.
    """

    invited_discount_percent: int = 50
    discount_cap_rub: int = 0
    referrer_days: int = 30
    invited_bonus_days: int = 3
    plan: Plan = DEFAULT_PLANS[0]

    def discount_rub(self) -> int:
        """Скидка в рублях для выбранного тарифа — с учётом планки."""
        raw = round(self.plan.price_rub * self.invited_discount_percent / 100)
        if self.discount_cap_rub:
            raw = min(raw, self.discount_cap_rub)
        return min(raw, self.plan.price_rub)


@dataclass(frozen=True, slots=True)
class ReferralCost:
    """Во сколько обошёлся один приведённый друг.

    :param first_payment_rub: сколько заплатил приглашённый (может быть 0,
        если скидка закрыла первый месяц целиком).
    :param discount_rub: скидка приглашённому.
    :param days_rub: стоимость бонусных дней пригласившего и приглашённого.
    :param first_payment_net_rub: что осталось нам с первой оплаты.
    :param months_paid_for: на какой срок друг оплатил (влияет на проверку
        окупаемости: скидка должна отбиться за время действия тарифа).
    """

    first_payment_rub: float
    discount_rub: float
    days_rub: float
    first_payment_net_rub: float
    months_paid_for: float

    @property
    def contra_revenue_rub(self) -> float:
        """Сколько реальных денег мы отдали: скидка плюс бесплатные дни.

        Это «упущенная выручка» — то, что клиенты заплатили бы, если бы акции
        не было. По ней видно нагрузку на кассу месяца.
        """
        return self.discount_rub + self.days_rub

    @property
    def effective_cac_rub(self) -> float:
        """Цена привлечения: сколько мы недозаработали на этом клиенте.

        Друг оплачивает часть своего доступа сам, поэтому из недополученной
        выручки вычитаем то, что он реально внёс (за вычетом комиссии канала).
        Ноль означает «друг оплатил своё привлечение сам» — программу можно
        масштабировать; большая величина означает «мы платим за него из
        прибыли с других клиентов».
        """
        return max(0.0, self.contra_revenue_rub - self.first_payment_net_rub)

    def payback_months(self, econ: UnitEconomics) -> float:
        """За сколько месяцев этот клиент окупает скидку.

        Сравнивать надо со сроком тарифа: если скидка окупается дольше, чем
        клиент за неё сидит, акция убыточна на первом же платеже.
        """
        return econ.payback_months(self.effective_cac_rub)


def referral_cost(terms: ReferralTerms, econ: UnitEconomics) -> ReferralCost:
    """Посчитать, во что обходится один друг по текущим условиям программы."""
    discount = terms.discount_rub()
    first_payment = terms.plan.price_rub - discount
    all_days = terms.referrer_days + terms.invited_bonus_days
    days_rub = econ.months_of_service_rub(all_days / 30)
    return ReferralCost(
        first_payment_rub=float(first_payment),
        discount_rub=float(discount),
        days_rub=days_rub,
        first_payment_net_rub=first_payment * (1 - econ.fee_percent / 100),
        months_paid_for=terms.plan.days / 30,
    )


def referral_verdict(
    cost: ReferralCost,
    econ: UnitEconomics,
    terms: ReferralTerms,
    cap: float | None = None,
) -> tuple[bool, str]:
    """Укладывается ли рефералка в потолок привлечения.

    Проверяются две вещи, и это разные диагнозы:

      * **не окупается за срок тарифа** — мы недозаработали больше, чем друг
        успел принести до конца купленного периода. Так выглядит слишком
        большая скидка на коротком тарифе;
      * **дорого** — цена привлечения выше половины LTV: акция съедает прибыль,
        которую этот клиент принесёт за всю жизнь.

    Возвращает (проходит ли, короткое объяснение словами).
    """
    ceiling = cap if cap is not None else econ.cac_ceiling_rub()
    if econ.net_rub <= 0:
        return False, "канал оплаты съедает всю выручку"
    payback = cost.payback_months(econ)
    if payback > cost.months_paid_for:
        return False, f"не окупается за срок тарифа ({payback:.1f} мес против {cost.months_paid_for:.0f})"
    if cost.effective_cac_rub > econ.cac_ceiling_rub(MAX_CAC_SHARE):
        return False, "дорого: акция съедает прибыль с клиента"
    if cost.effective_cac_rub <= ceiling:
        return True, f"ок, окупается за {payback:.1f} мес"
    return True, f"на грани, окупается за {payback:.1f} мес"


def viral_coefficient(invites_per_user: float, pay_conversion: float) -> float:
    """Коэффициент виральности K = приглашений на клиента × доля оплативших.

    K < 1 — рост затухает, рефералка только помогает платным каналам; K ≥ 1 —
    каждый клиент приводит больше одного нового, и рост идёт сам.
    """
    return invites_per_user * pay_conversion


def viral_reach(base_users: int, k: float, generations: int = 6) -> list[int]:
    """Сколько людей придёт по цепочке приглашений за N «поколений».

    Первое число — сколько пригласили существующие клиенты, второе — сколько
    пригласили уже они, и так далее. Сумма — добавка к базе.
    """
    rows: list[int] = []
    current = float(base_users)
    for _ in range(max(0, generations)):
        current = current * k
        rows.append(int(round(current)))
        if current < 0.5:
            break
    return rows


@dataclass(frozen=True, slots=True)
class GrowthChannel:
    """Канал привлечения: что потратили и сколько платящих получили.

    :param name: название для отчёта («размещение у X», «конкурс», «рефералка»).
    :param spend_rub: потраченные деньги (гонорар, приз, реклама).
    :param trials: сколько людей пришло на пробный доступ.
    :param payers: сколько из них оплатило.
    """

    name: str
    spend_rub: float
    trials: int
    payers: int

    @property
    def cac_rub(self) -> float:
        """Цена одного платящего клиента из этого канала."""
        if self.payers <= 0:
            return float("inf")
        return self.spend_rub / self.payers

    @property
    def trial_to_paid(self) -> float:
        """Конверсия из пробного доступа в оплату, доля."""
        if self.trials <= 0:
            return 0.0
        return self.payers / self.trials

    def verdict(self, econ: UnitEconomics) -> str:
        """Короткая оценка канала для таблицы отчёта."""
        if self.payers <= 0:
            return "нет оплат — выключить или менять креатив"
        cac = self.cac_rub
        if cac <= econ.cac_ceiling_rub():
            return "отлично"
        if cac <= econ.cac_ceiling_rub(MAX_CAC_SHARE):
            return "на грани"
        return "убыточно"


def required_trials(
    spend_rub: float,
    econ: UnitEconomics,
    trial_to_paid: float,
    share: float = HEALTHY_CAC_SHARE,
) -> float:
    """Сколько пробных доступов нужно с канала, чтобы он окупился.

    Помогает до покупки рекламы: если размещение стоит 3 000 ₽, а конверсия из
    пробного в оплату известна (например, 30 %), видно, сколько людей должно
    прийти, чтобы это имело смысл.
    """
    if trial_to_paid <= 0:
        return float("inf")
    ceiling = econ.cac_ceiling_rub(share)
    return spend_rub / ceiling / trial_to_paid


@dataclass(frozen=True, slots=True)
class GrowthPlan:
    """План роста: сколько платящих приходит из какого источника.

    :param organic_per_month: приходят сами (сарафан, поиск, канал).
    :param referral_per_month: приходят по приглашению.
    :param paid_per_month: приходят из платных размещений и акций.
    :param paid_spend_rub: расходы на привлечение в месяц.
    """

    organic_per_month: float = 8.0
    referral_per_month: float = 4.0
    paid_per_month: float = 0.0
    paid_spend_rub: float = 0.0

    @property
    def new_per_month(self) -> float:
        return self.organic_per_month + self.referral_per_month + self.paid_per_month

    @property
    def cac_rub(self) -> float:
        """Цена привлечения в среднем по всем платным источникам."""
        if self.paid_per_month <= 0:
            return 0.0
        return self.paid_spend_rub / self.paid_per_month

    def projection(self, econ: UnitEconomics, months: int = 12, start_users: int = 0) -> list[tuple[int, int, float]]:
        """Погодовая развёртка: (месяц, платящих, прибыль за месяц).

        Считаем в лоб, без формул геометрической прогрессии: на каждом шаге
        часть клиентов уходит (отток), приходит плановое число новых, расходы —
        ноды под текущее число клиентов плюс траты на привлечение.
        """
        rows: list[tuple[int, int, float]] = []
        users = float(start_users)
        for month in range(1, months + 1):
            users = users * (1 - econ.churn) + self.new_per_month
            paid = int(round(users))
            nodes = max(1, int(users / econ.users_per_node) + (1 if users % econ.users_per_node else 0))
            revenue = users * econ.net_rub
            costs = nodes * econ.node_rub + self.paid_spend_rub
            rows.append((month, paid, revenue - costs))
        return rows
