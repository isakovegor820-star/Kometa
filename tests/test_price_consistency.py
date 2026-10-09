"""Предохранитель от расхождения цен между источниками.

Цена тарифа живёт в четырёх местах: сид базы (`app/db/session.py`), финмодель
(`app/services/finmodel.py`), юр. документы (`app/services/documents.py`) и сама
таблица `plans` (её правит админка или `app.tools.set_prices`). При смене сетки
легко поправить одно место из четырёх — эти тесты ловят именно такой дрейф.

Сетка от 08.10.2026: 120 / 299 / 539 / 959 ₽. Разбор решения — docs/РЕВЬЮ-ЦЕНЫ-120.md.
"""

from __future__ import annotations

from app.config import get_settings
from app.services import documents, orders
from app.services.finmodel import (
    DEFAULT_PLANS,
    PlanMix,
    average_monthly_revenue,
    net_after_partner_payout,
)
from app.services.finmodel_vps import DEFAULT_AVERAGE_CHECK_RUB, DEFAULT_NET_PER_USER

#: Базовая точка сетки: цена месячного тарифа.
MONTHLY_PRICE_RUB = 120

#: Ниже этого отношения «звёзды / рубли» звёздный канал уходит в убыток
#: (Telegram платит ~$0,013 за звезду при курсе 92 ₽/$ и 5 % на вывод).
MIN_STARS_RATIO = 0.84


def test_monthly_plan_costs_120():
    monthly = next(plan for plan in DEFAULT_PLANS if plan.code == "m1")

    assert monthly.price_rub == MONTHLY_PRICE_RUB
    assert monthly.per_month == MONTHLY_PRICE_RUB


def test_ladder_is_monotonic():
    """Чем длиннее срок, тем дешевле месяц — иначе длинные тарифы не продаются."""
    per_month = [plan.per_month for plan in DEFAULT_PLANS]

    assert per_month == sorted(per_month, reverse=True), f"лестница сломана: {per_month}"


def test_legal_prices_match_finmodel():
    """Прайс для банка и финмодель считают одни и те же тарифы."""
    assert [(row.title, row.price_rub, row.days) for row in documents.DEFAULT_PRICES] == [
        (plan.title, plan.price_rub, plan.days) for plan in DEFAULT_PLANS
    ]


def test_vps_model_average_check_matches_finmodel():
    """Средний чек в модели VPS — округлённое значение той же финмодели."""
    check = average_monthly_revenue(DEFAULT_PLANS, PlanMix())

    assert abs(DEFAULT_AVERAGE_CHECK_RUB - check) < 0.1
    assert abs(DEFAULT_NET_PER_USER - net_after_partner_payout(check, 8.0)) < 0.1


async def test_database_seed_matches_finmodel(session):
    """Сид базы (а значит, и витрина бота) совпадает с финмоделью."""
    plans = await orders.list_plans(session)

    assert [(plan.code, plan.price_rub, plan.days) for plan in plans] == [
        (plan.code, plan.price_rub, plan.days) for plan in DEFAULT_PLANS
    ]


async def test_stars_are_profitable_and_fraud_alert_can_fire(session):
    """Звёздная цена не убыточна, а порог контроля крупных оплат — достижим."""
    plans = await orders.list_plans(session)
    settings = get_settings()

    for plan in plans:
        assert plan.price_stars / plan.price_rub >= MIN_STARS_RATIO, (
            f"{plan.code}: {plan.price_stars} ⭐ за {plan.price_rub} ₽ — канал в убытке"
        )
    # Если порог выше самого дорогого тарифа в звёздах, алерт не сработает никогда.
    assert settings.stars_watch_threshold <= max(plan.price_stars for plan in plans)
