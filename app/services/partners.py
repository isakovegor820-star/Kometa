"""Партнёры: своя ссылка, свой промокод, свой процент и учёт выплат.

Зачем это отдельно от рефералки. Рефералка — программа для клиентов: друг
приводит друга и получает дни подписки. Партнёр — это внешний канал (блогер,
админ чата, реселлер, знакомый с аудиторией), и ему нужны три вещи, которых
рефералка не даёт:

  1. **своя ссылка** ``t.me/<bot>?start=src_<код>`` — чтобы считать именно его;
  2. **свой процент скидки** для его аудитории (у кого-то 10 %, у кого-то 50 %);
  3. **выплата ему** — процент с платежей приведённых людей или фикс за оплату.

Что отслеживается по каждому партнёру:

| Метрика | Как считается |
|---|---|
| Переходы | ``users.partner_id`` — сколько людей закрепилось за партнёром |
| Оплаты | число оплаченных заказов этих людей |
| Платящие | сколько разных людей из них заплатили |
| Выручка | сумма оплаченных заказов |
| Скидка, которую мы дали | сумма скидок по заказам этих людей |
| **Наша выплата партнёру** | процент с платежей или фикс за каждую оплату |
| **Долг** | начислено минус уже выплачено |
| **ROI** | выручка, делённая на выплату |

Решение о выплате принимает владелец: сервис показывает долг и кнопку
«Выплачено». Автоматических переводов нет намеренно — деньги уходят человеку,
и подтверждать это должен человек.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Order, Partner, PromoCode, User, utcnow
from app.services import events

settings = get_settings()

#: Код партнёра в ссылке: латиница, цифры, дефис и подчёркивание.
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")

#: Как считаем выплату партнёру.
REWARD_PERCENT = "percent"
REWARD_FIXED = "fixed"
REWARD_NONE = "none"
REWARD_KINDS = (REWARD_PERCENT, REWARD_FIXED, REWARD_NONE)

REWARD_TITLES = {
    REWARD_PERCENT: "процент с платежей",
    REWARD_FIXED: "фикс за каждую оплату",
    REWARD_NONE: "без выплаты (ради скидки аудитории)",
}


class PartnerError(ValueError):
    """Партнёра нельзя создать или изменить: причина понятна человеку."""


# ------------------------------------------------------------------ создание
def normalize_slug(raw: str) -> str:
    """Привести код ссылки к допустимому виду.

    Человек может ввести «Иван Канал» или «YT-Ivanov» — приводим к латинице
    в нижнем регистре. Пробелы и недопустимые символы превращаем в дефис:
    иначе ссылка ломается, а партнёр не понимает, почему не считается.
    """
    value = " ".join((raw or "").split()).casefold()
    # Кириллицу транслитерировать не пытаемся: проще попросить латиницу,
    # чем получить нечитаемую ссылку. Заменяем всё недопустимое на дефис.
    value = re.sub(r"[^a-z0-9_-]+", "-", value).strip("-_")
    return value[:32]


def normalize_code(raw: str) -> str:
    """Промокод партнёра: «KOMETA-<КОД>», всегда в верхнем регистре."""
    value = " ".join((raw or "").split()).upper().replace(" ", "-")
    value = re.sub(r"[^A-Z0-9_-]+", "", value)
    if not value:
        return ""
    return value if value.startswith("KOMETA-") else f"KOMETA-{value}"[:32]


def validate(name: str, slug: str, discount_percent: int, reward_kind: str, reward_value: float) -> None:
    """Проверить поля до записи в базу. Ошибки — текстом для админки."""
    if not (name or "").strip():
        raise PartnerError("Укажи имя партнёра — иначе непонятно, кому платить")
    if not SLUG_PATTERN.match(slug or ""):
        raise PartnerError(
            "Код ссылки: латиница, цифры, дефис; 2–32 знака. Например «ivan-youtube»"
        )
    if not 0 <= int(discount_percent) <= 100:
        raise PartnerError("Скидка указывается в процентах: от 0 до 100")
    if reward_kind not in REWARD_KINDS:
        raise PartnerError("Выплата: процент с платежей, фикс за оплату или без выплаты")
    if reward_kind != REWARD_NONE and float(reward_value) < 0:
        raise PartnerError("Выплата не может быть отрицательной")
    if reward_kind == REWARD_PERCENT and float(reward_value) > 100:
        raise PartnerError("Процент выплаты больше 100 — партнёр получит больше, чем мы")


async def create_partner(
    session: AsyncSession,
    *,
    name: str,
    slug: str,
    discount_percent: int = 0,
    discount_max_rub: int = 0,
    reward_kind: str = REWARD_PERCENT,
    reward_value: float = 30.0,
    note: str = "",
    promo_code: str = "",
) -> Partner:
    """Создать партнёра и его промокод.

    :param promo_code: код для ввода руками. Пусто — соберём из кода ссылки.
    """
    clean_slug = normalize_slug(slug)
    validate(name, clean_slug, discount_percent, reward_kind, reward_value)

    existing = await session.scalar(select(Partner).where(Partner.slug == clean_slug))
    if existing is not None:
        raise PartnerError(f"Партнёр с кодом «{clean_slug}» уже есть")

    code = normalize_code(promo_code or clean_slug)
    if await session.scalar(select(PromoCode).where(PromoCode.code == code)) is not None:
        raise PartnerError(f"Промокод «{code}» уже занят — выбери другой")

    partner = Partner(
        name=name.strip()[:64],
        slug=clean_slug,
        discount_percent=max(0, min(100, int(discount_percent))),
        discount_max_rub=max(0, int(discount_max_rub)),
        reward_kind=reward_kind,
        reward_value=float(reward_value),
        is_active=True,
        note=(note or "").strip()[:200] or None,
    )
    session.add(partner)
    await session.flush()

    # Промокод партнёра: скидка такая же, как по ссылке, чтобы человек получил
    # одно и то же, каким бы способом он ни пришёл.
    session.add(
        PromoCode(
            code=code,
            kind="partner",
            partner_id=partner.id,
            # Ноль тоже допустим: партнёр может приводить людей без скидки,
            # и тогда цена для них не меняется.
            percent=max(0, min(100, int(discount_percent))),
            max_discount_rub=max(0, int(discount_max_rub)),
            first_only=True,
            uses_limit=0,
            is_active=True,
            note=f"промокод партнёра {partner.name}",
        )
    )
    await session.flush()
    return partner


async def update_partner(
    session: AsyncSession,
    partner: Partner,
    *,
    name: str | None = None,
    discount_percent: int | None = None,
    discount_max_rub: int | None = None,
    reward_kind: str | None = None,
    reward_value: float | None = None,
    note: str | None = None,
    is_active: bool | None = None,
) -> Partner:
    """Изменить условия партнёра. Код ссылки не меняем: ссылки уже разошлись."""
    new_name = partner.name if name is None else name.strip()[:64]
    new_discount = partner.discount_percent if discount_percent is None else int(discount_percent)
    new_kind = partner.reward_kind if reward_kind is None else reward_kind
    new_value = partner.reward_value if reward_value is None else float(reward_value)
    validate(new_name, partner.slug, new_discount, new_kind, new_value)

    partner.name = new_name
    partner.discount_percent = max(0, min(100, new_discount))
    if discount_max_rub is not None:
        partner.discount_max_rub = max(0, int(discount_max_rub))
    partner.reward_kind = new_kind
    partner.reward_value = new_value
    if note is not None:
        partner.note = note.strip()[:200] or None
    if is_active is not None:
        partner.is_active = bool(is_active)
    await session.flush()

    # Промокод держим синхронно: скидка в боте и в ссылке должна совпадать.
    promo_row = await promo_for_partner(session, partner.id)
    if promo_row is not None:
        promo_row.percent = max(0, min(100, partner.discount_percent))
        promo_row.max_discount_rub = partner.discount_max_rub
        promo_row.is_active = partner.is_active
        await session.flush()
    return partner


async def promo_for_partner(session: AsyncSession, partner_id: int) -> PromoCode | None:
    """Промокод партнёра (первый найденный)."""
    return await session.scalar(
        select(PromoCode).where(PromoCode.partner_id == partner_id).order_by(PromoCode.id).limit(1)
    )


# ------------------------------------------------------------------ поиск
async def get_by_slug(session: AsyncSession, slug: str) -> Partner | None:
    """Найти активного партнёра по коду ссылки."""
    clean = normalize_slug(slug)
    if not clean:
        return None
    return await session.scalar(
        select(Partner).where(Partner.slug == clean, Partner.is_active.is_(True))
    )


async def list_partners(session: AsyncSession, limit: int = 200) -> list[Partner]:
    stmt = select(Partner).order_by(Partner.is_active.desc(), Partner.id.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())


def partner_link(bot_username: str, slug: str) -> str:
    """Ссылка партнёра для распространения."""
    return f"https://t.me/{bot_username}?start=src_{slug}"


# ------------------------------------------------------------------ статистика
@dataclass(slots=True)
class PartnerStats:
    """Что партнёр принёс и сколько мы ему за это должны.

    :param clicked: сколько людей закрепилось за партнёром (пришли по его ссылке).
    :param payers: сколько из них дошло до оплаты.
    :param orders: число оплаченных заказов.
    :param revenue_rub: сколько денег принесли эти заказы.
    :param discount_rub: сколько скидок мы им выдали.
    :param reward_rub: сколько начислено партнёру за всё время.
    :param paid_out_rub: сколько уже выплачено вручную.
    """

    partner: Partner
    clicked: int
    payers: int
    orders: int
    revenue_rub: float
    discount_rub: float
    reward_rub: float
    paid_out_rub: float
    #: Промокод партнёра — его аудитория может ввести его руками вместо ссылки.
    promo_code: str = ""

    @property
    def conversion(self) -> float:
        """Доля пришедших, которые заплатили."""
        return self.payers / self.clicked if self.clicked else 0.0

    @property
    def debt_rub(self) -> float:
        """Сколько остались должны партнёру."""
        return max(0.0, self.reward_rub - self.paid_out_rub)

    @property
    def total_cost_rub(self) -> float:
        """Полная стоимость канала: выплата партнёру плюс скидки его аудитории."""
        return self.reward_rub + self.discount_rub

    @property
    def cac_rub(self) -> float:
        """Сколько нам стоил один платящий клиент из этого канала."""
        if not self.payers:
            return float("inf")
        return self.total_cost_rub / self.payers

    @property
    def roi(self) -> float:
        """Сколько рублей пришло на каждый рубль, отданный каналу."""
        if self.total_cost_rub <= 0:
            return float("inf") if self.revenue_rub > 0 else 0.0
        return self.revenue_rub / self.total_cost_rub


def _order_amounts(partner_id: int):
    """Условия выборки «платежи людей этого партнёра»."""
    return (
        Order.status == "paid",
        User.partner_id == partner_id,
    )


async def partner_stats(session: AsyncSession, partner: Partner) -> PartnerStats:
    """Собрать статистику одного партнёра одним набором запросов."""
    clicked = await session.scalar(
        select(func.count(User.id)).where(User.partner_id == partner.id)
    ) or 0
    row = (
        await session.execute(
            select(
                func.count(Order.id),
                func.count(func.distinct(Order.user_id)),
                func.coalesce(func.sum(Order.amount_rub), 0),
                func.coalesce(func.sum(Order.discount_rub), 0),
            )
            .join(User, User.id == Order.user_id)
            .where(*_order_amounts(partner.id))
        )
    ).one()
    orders_count, payers, revenue, discount = (int(row[0] or 0), int(row[1] or 0), float(row[2] or 0), float(row[3] or 0))

    # Начисление: процент с каждого платежа или фикс за каждую оплату.
    # Считаем на лету по текущим условиям — если владелец поменяет процент,
    # в таблице сразу видно новый долг, а история выплат остаётся в paid_out_rub.
    if partner.reward_kind == REWARD_PERCENT:
        reward = revenue * float(partner.reward_value) / 100
    elif partner.reward_kind == REWARD_FIXED:
        reward = orders_count * float(partner.reward_value)
    else:
        reward = 0.0

    promo_row = await promo_for_partner(session, partner.id)
    return PartnerStats(
        partner=partner,
        promo_code=promo_row.code if promo_row is not None else "",
        clicked=int(clicked),
        payers=payers,
        orders=orders_count,
        revenue_rub=revenue,
        discount_rub=discount,
        reward_rub=round(reward, 2),
        paid_out_rub=float(partner.paid_out_rub or 0.0),
    )


async def all_stats(session: AsyncSession, limit: int = 200) -> list[PartnerStats]:
    """Статистика по всем партнёрам — для таблицы в админке."""
    return [await partner_stats(session, partner) for partner in await list_partners(session, limit)]


async def totals(stats: list[PartnerStats]) -> dict[str, float]:
    """Итоги по всем партнёрам: сколько принесли и сколько мы должны."""
    return {
        "clicked": float(sum(row.clicked for row in stats)),
        "payers": float(sum(row.payers for row in stats)),
        "revenue_rub": sum(row.revenue_rub for row in stats),
        "reward_rub": sum(row.reward_rub for row in stats),
        "discount_rub": sum(row.discount_rub for row in stats),
        "debt_rub": sum(row.debt_rub for row in stats),
        "paid_out_rub": sum(row.paid_out_rub for row in stats),
    }


async def register_payout(session: AsyncSession, partner: Partner, amount_rub: float) -> Partner:
    """Отметить, что партнёру выплатили.

    Сумма прибавляется к уже выплаченному: долг считается как «начислено минус
    выплачено», поэтому частичные выплаты видно корректно.
    """
    value = float(amount_rub)
    if value < 0:
        raise PartnerError("Сумма выплаты не может быть отрицательной")
    partner.paid_out_rub = float(partner.paid_out_rub or 0.0) + value
    partner.paid_out_at = utcnow()
    await session.flush()
    await events.log_event(
        session,
        events.PARTNER_PAID_OUT,
        payload={"partner_id": partner.id, "amount_rub": value, "total_rub": partner.paid_out_rub},
    )
    return partner


async def attach_partner(session: AsyncSession, user: User, slug: str) -> Partner | None:
    """Закрепить человека за партнёром по коду ссылки.

    Работает один раз: первый канал сохраняет заслугу. Если человек уже
    закреплён (за партнёром или пришёл по рефералке друга) — не переписываем.
    """
    if user.partner_id is not None:
        return None
    partner = await get_by_slug(session, slug)
    if partner is None:
        return None
    user.partner_id = partner.id
    user.source = "src"
    user.source_detail = partner.slug
    user.source_at = utcnow()
    await session.flush()
    return partner


async def users_of_partner(session: AsyncSession, partner: Partner, limit: int = 100) -> list[dict]:
    """Кто пришёл от партнёра и сколько заплатил — для карточки партнёра."""
    rows = (
        await session.execute(
            select(
                User,
                func.count(Order.id),
                func.coalesce(func.sum(Order.amount_rub), 0),
            )
            .outerjoin(Order, (Order.user_id == User.id) & (Order.status == "paid"))
            .where(User.partner_id == partner.id)
            .group_by(User.id)
            .order_by(func.coalesce(func.sum(Order.amount_rub), 0).desc(), User.id.desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "user": user,
            "orders": int(orders or 0),
            "revenue_rub": float(revenue or 0),
        }
        for user, orders, revenue in rows
    ]


# ------------------------------------------------------------------ начисление
async def accrue_reward(session: AsyncSession, order: Order, user: User) -> float:
    """Зафиксировать партнёра и начислить выплату за оплаченный заказ.

    Вызывается из :func:`app.services.orders.mark_paid` на каждой оплате.
    Партнёр берётся из привязки человека, а сумма снимком пишется в заказ:
    если условия партнёра потом изменят, история платежей не пересчитается
    задним числом.

    :returns: начисленная сумма в рублях (0 — партнёра нет или выплата не положена).
    """
    if user.partner_id is None:
        return 0.0
    partner = await session.get(Partner, user.partner_id)
    if partner is None or not partner.is_active:
        return 0.0

    if partner.reward_kind == REWARD_PERCENT:
        reward = float(order.amount_rub) * float(partner.reward_value) / 100
    elif partner.reward_kind == REWARD_FIXED:
        reward = float(partner.reward_value)
    else:
        reward = 0.0

    order.partner_id = partner.id
    order.partner_reward_rub = round(reward, 2)
    await session.flush()
    if reward:
        await events.log_event(
            session,
            events.PARTNER_REWARDED,
            user_id=user.id,
            payload={"order_id": order.id, "partner_id": partner.id, "reward_rub": order.partner_reward_rub},
        )
    return order.partner_reward_rub
