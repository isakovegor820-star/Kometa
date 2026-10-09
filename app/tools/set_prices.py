"""CLI: смена цен тарифов в базе.

Зачем отдельный инструмент. `seed_plans()` создаёт тарифы один раз и **никогда
не перезаписывает** цену существующих: правка констант в коде (сид, финмодель,
юр. документы) на работающую базу — локальную и на проде — не влияет. Цена живёт
в таблице `plans`, и менять её нужно здесь (или руками в админке, `/admin`).

По умолчанию скрипт ничего не пишет: показывает, что станет, и просит `--apply`.

Запуск:
    .venv/bin/python -m app.tools.set_prices                     # что в базе сейчас
    .venv/bin/python -m app.tools.set_prices --grid 120          # предпросмотр сетки
    .venv/bin/python -m app.tools.set_prices --grid 120 --apply  # применить
    .venv/bin/python -m app.tools.set_prices --m1 120 --stars-m1 110 --apply

Сетка «120» (решение 08.10.2026, разбор — docs/РЕВЬЮ-ЦЕНЫ-120.md):
    1 месяц — 120 ₽ / 110 ⭐ · 3 месяца — 299 ₽ / 270 ⭐
    6 месяцев — 539 ₽ / 485 ⭐ · 12 месяцев — 959 ₽ / 860 ⭐
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from sqlalchemy import select

from app.db.models import Plan
from app.db.session import SessionMaker, init_db
from app.services import audit

#: Готовые сетки: код → {код тарифа: (цена ₽, цена ⭐)}.
PRESETS: dict[str, dict[str, tuple[int, int]]] = {
    "120": {
        "m1": (120, 110),
        "m3": (299, 270),
        "m6": (539, 485),
        "m12": (959, 860),
    },
}

#: Ниже этого отношения «звёзды / рубли» звёздный канал уходит в убыток
#: (Telegram платит ~$0,013 за звезду при курсе 92 ₽/$ и 5 % на вывод).
MIN_STARS_RATIO = 0.84

PLAN_CODES = ("m1", "m3", "m6", "m12")


@dataclass(frozen=True, slots=True)
class Change:
    """Что меняется у одного тарифа."""

    code: str
    title: str
    old_rub: int
    new_rub: int
    old_stars: int
    new_stars: int

    @property
    def changed(self) -> bool:
        return (self.old_rub, self.old_stars) != (self.new_rub, self.new_stars)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Смена цен тарифов в базе")
    parser.add_argument("--grid", choices=sorted(PRESETS), help="готовая сетка (120 = 120/299/539/959 ₽)")
    parser.add_argument("--apply", action="store_true", help="записать в базу (без флага — только показать)")
    for code in PLAN_CODES:
        parser.add_argument(f"--{code}", type=int, help=f"цена тарифа {code} в рублях")
        parser.add_argument(f"--stars-{code}", type=int, help=f"цена тарифа {code} в звёздах")
    return parser.parse_args()


def desired_prices(args: argparse.Namespace, current: dict[str, Plan]) -> dict[str, tuple[int, int]]:
    """Собрать целевые цены: готовая сетка, затем точечные переопределения."""
    target = {code: (plan.price_rub, plan.price_stars) for code, plan in current.items()}
    if args.grid:
        for code, (rub, stars) in PRESETS[args.grid].items():
            if code in target:
                target[code] = (rub, stars)
    for code in PLAN_CODES:
        rub = getattr(args, code)
        stars = getattr(args, f"stars_{code}")
        if code in target:
            price, star_price = target[code]
            target[code] = (rub if rub is not None else price, stars if stars is not None else star_price)
    return target


def validate(target: dict[str, tuple[int, int]], titles: dict[str, str]) -> list[str]:
    """Проверить сетку до записи. Возвращает список ошибок (пустой — всё хорошо)."""
    errors: list[str] = []
    for code, (rub, stars) in target.items():
        if rub <= 0 or stars <= 0:
            errors.append(f"{code}: цена должна быть больше нуля (₽ {rub}, ⭐ {stars})")
            continue
        if stars / rub < MIN_STARS_RATIO:
            errors.append(
                f"{code}: {stars} ⭐ за {rub} ₽ — отношение {stars / rub:.2f} ниже "
                f"{MIN_STARS_RATIO} (звёздный канал уйдёт в убыток)"
            )
    ladder = [
        (code, target[code][0] / days * 30)
        for code, days in (("m1", 30), ("m3", 90), ("m6", 180), ("m12", 365))
        if code in target
    ]
    for (first_code, first_per_month), (next_code, next_per_month) in zip(ladder, ladder[1:]):
        if next_per_month > first_per_month:
            errors.append(
                f"{titles.get(next_code, next_code)} в пересчёте на месяц дороже, чем "
                f"{titles.get(first_code, first_code)} ({next_per_month:.0f} ₽/мес против "
                f"{first_per_month:.0f} ₽/мес) — лестница тарифов сломана"
            )
    return errors


async def load_plans() -> dict[str, Plan]:
    async with SessionMaker() as session:
        plans = (await session.scalars(select(Plan).order_by(Plan.sort_order, Plan.price_rub))).all()
        return {plan.code: plan for plan in plans}


async def apply_changes(changes: list[Change]) -> None:
    """Записать новые цены и оставить след в журнале действий."""
    async with SessionMaker() as session:
        plans = {plan.code: plan for plan in (await session.scalars(select(Plan))).all()}
        for change in changes:
            plan = plans[change.code]
            plan.price_rub = change.new_rub
            plan.price_stars = change.new_stars
            await audit.log_action(
                session,
                "plan_saved",
                actor=audit.Actor(name="CLI set_prices", role="auto", source=audit.SOURCE_AUTO),
                payload={
                    "plan": change.code,
                    "price_rub": {"before": change.old_rub, "after": change.new_rub},
                    "price_stars": {"before": change.old_stars, "after": change.new_stars},
                },
            )
        await session.commit()


def money(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₽"


async def run(args: argparse.Namespace) -> int:
    await init_db()
    current = await load_plans()
    if not current:
        print("В базе нет тарифов — сначала запусти бота (создаст справочник) или `init_db()`.")
        return 1

    target = desired_prices(args, current)
    titles = {code: plan.title for code, plan in current.items()}
    changes = [
        Change(code, plan.title, plan.price_rub, target[code][0], plan.price_stars, target[code][1])
        for code, plan in current.items()
    ]

    errors = validate(target, titles)
    if errors:
        print("Сетка не проходит проверку:")
        for error in errors:
            print(f"  ✗ {error}")
        return 2

    print(f"{'тариф':14} {'было':>20} {'станет':>20}")
    for change in changes:
        was = f"{money(change.old_rub)} / {change.old_stars} ⭐"
        will = f"{money(change.new_rub)} / {change.new_stars} ⭐"
        mark = "→" if change.changed else "="
        print(f"{change.title:14} {was:>20} {mark} {will:>20}")

    touched = [change for change in changes if change.changed]
    if not touched:
        print("\nМенять нечего: в базе уже эти цены.")
        return 0
    if not args.apply:
        print(f"\nПредпросмотр: изменится тарифов — {len(touched)}. Чтобы записать, добавь --apply.")
        return 0

    await apply_changes(touched)
    print(f"\nГотово: обновлено тарифов — {len(touched)}. Проверь витрину бота и /admin.")
    return 0


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
