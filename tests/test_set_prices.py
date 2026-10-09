"""Тесты CLI смены цен (`app.tools.set_prices`).

Скрипт нужен потому, что `seed_plans()` не перезаписывает цены существующих
тарифов: правка констант в коде на работающую базу не влияет.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import Event, Plan
from app.tools import set_prices


def test_preset_grid_passes_validation():
    """Готовая сетка «120» должна проходить проверку без замечаний."""
    target = set_prices.PRESETS["120"]
    titles = {code: code for code in target}

    assert set_prices.validate(target, titles) == []


def test_validation_rejects_broken_ladder():
    """Три месяца дороже месяца в пересчёте на месяц — сетка не годится."""
    target = {"m1": (120, 110), "m3": (499, 450), "m6": (539, 485), "m12": (959, 860)}

    errors = set_prices.validate(target, {"m1": "1 месяц", "m3": "3 месяца", "m6": "6 месяцев", "m12": "12 месяцев"})

    assert any("лестница" in error for error in errors)


def test_validation_rejects_unprofitable_stars():
    """Слишком дешёвые звёзды — канал в убытке, такую цену записывать нельзя."""
    target = {"m1": (120, 50)}

    errors = set_prices.validate(target, {"m1": "1 месяц"})

    assert any("убыток" in error for error in errors)


def test_validation_rejects_zero_price():
    errors = set_prices.validate({"m1": (0, 0)}, {"m1": "1 месяц"})

    assert any("больше нуля" in error for error in errors)


async def test_apply_writes_prices_and_audit(session):
    """Применение меняет цену в базе и оставляет след в журнале действий."""
    await session.commit()  # отпускаем блокировку SQLite перед записью из CLI
    plan = (await session.scalars(select(Plan).where(Plan.code == "m1"))).one()
    old_rub, old_stars = plan.price_rub, plan.price_stars

    changes = [set_prices.Change("m1", plan.title, old_rub, 111, old_stars, 101)]
    await set_prices.apply_changes(changes)

    session.expire_all()  # CLI писал из своей сессии — читаем свежие значения
    updated = (await session.scalars(select(Plan).where(Plan.code == "m1"))).one()
    assert (updated.price_rub, updated.price_stars) == (111, 101)

    events = list((await session.scalars(select(Event).where(Event.kind == "admin.plan_saved"))).all())
    assert events, "смена цены обязана попадать в журнал действий"
    assert "111" in (events[-1].payload or "")


async def test_desired_prices_keeps_untouched_plans(session):
    """Точечная правка одного тарифа не трогает остальные."""
    import argparse

    await session.commit()
    current = await set_prices.load_plans()
    namespace = argparse.Namespace(
        grid=None,
        apply=False,
        **{code: None for code in set_prices.PLAN_CODES},
        **{f"stars_{code}": None for code in set_prices.PLAN_CODES},
    )
    namespace.m1 = 99

    target = set_prices.desired_prices(namespace, current)

    assert target["m1"][0] == 99
    assert target["m1"][1] == current["m1"].price_stars  # звёзды не тронуты
    for code in ("m3", "m6", "m12"):
        assert target[code] == (current[code].price_rub, current[code].price_stars)


async def test_preset_applies_all_four_plans(session):
    """Сетка «120» меняет все четыре тарифа, а не только месячный."""
    current = await set_prices.load_plans()
    namespace = __import__("argparse").Namespace(
        grid="120",
        apply=False,
        **{code: None for code in set_prices.PLAN_CODES},
        **{f"stars_{code}": None for code in set_prices.PLAN_CODES},
    )

    target = set_prices.desired_prices(namespace, current)

    assert target == set_prices.PRESETS["120"]
