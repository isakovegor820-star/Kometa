"""Персональные ссылки: свой процент под конкретного человека.

Зачем отдельно от партнёрских ссылок и промокодов.

* **Партнёрская ссылка** одна на партнёра, и её условия видит вся его аудитория.
  Когда блогер делает несколько размещений, по ней нельзя понять, какой пост
  сработал, а условия у всех постов одинаковые.
* **Промокод** человек должен ввести руками — на этом теряется часть людей.
* **Персональная ссылка** — под одного адресата: своя скидка, свой срок, свой
  лимит активаций. Скидка применяется автоматически.

Что видно в админке по каждой ссылке: сколько человек пришло, сколько оплатило,
сколько денег принесли и во сколько обошлась скидка. То есть та же логика, что
у партнёров, но на уровне отдельной ссылки, а не канала целиком.

Ссылка вида ``t.me/<bot>?start=p_<код>``. Код генерируется сам, но его можно
задать руками — удобно, когда ссылку диктуют голосом.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, Partner, PersonalLink, PromoCode, User, utcnow
from app.services import events

#: Код ссылки: латиница, цифры, дефис. Тот же алфавит, что у партнёров, чтобы
#: ссылки не отличались на глаз.
CODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")

#: Алфавит автогенерации: без похожих символов (0/O, 1/I/L) — код диктуют голосом.
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


class PersonalLinkError(ValueError):
    """Ссылку нельзя создать или изменить: причина понятна человеку."""


# ------------------------------------------------------------------ утилиты
def new_code(length: int = 7) -> str:
    """Сгенерировать код персональной ссылки."""
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def normalize_code(raw: str) -> str:
    """Привести код к допустимому виду.

    Владелец может ввести «Иван 12.10» или «Serega» — приводим к латинице в
    нижнем регистре; всё недопустимое становится дефисом.
    """
    value = " ".join((raw or "").split()).casefold()
    value = re.sub(r"[^a-z0-9_-]+", "-", value).strip("-_")
    return value[:32]


def link_url(bot_username: str, code: str) -> str:
    """Персональная ссылка для отправки человеку."""
    return f"https://t.me/{bot_username}?start=p_{code}"


def promo_code_for(code: str) -> str:
    """Код-двойник для ввода руками: тот же, что у ссылки, но с префиксом."""
    return f"KOMETA-{code.upper()}"[:32]


def validate(
    title: str,
    code: str,
    discount_percent: int,
    discount_max_rub: int,
    uses_limit: int,
) -> None:
    """Проверить поля до записи. Ошибки — текстом для админки."""
    if not (title or "").strip():
        raise PersonalLinkError("Укажи название ссылки: «Сергей, коллега» или «Пост у Ивана 12.10»")
    if not CODE_PATTERN.match(code or ""):
        raise PersonalLinkError("Код ссылки: латиница, цифры, дефис; 2–32 знака. Например «serega»")
    if not 0 <= int(discount_percent) <= 100:
        raise PersonalLinkError("Скидка указывается в процентах: от 0 до 100")
    if int(discount_max_rub) < 0:
        raise PersonalLinkError("Потолок скидки не может быть отрицательным")
    if int(uses_limit) < 0:
        raise PersonalLinkError("Лимит активаций не может быть отрицательным")


# ------------------------------------------------------------------ создание
async def create_link(
    session: AsyncSession,
    *,
    title: str,
    code: str = "",
    owner_name: str = "",
    partner_id: int | None = None,
    discount_percent: int = 20,
    discount_max_rub: int = 0,
    uses_limit: int = 0,
    days: int = 0,
    note: str = "",
) -> PersonalLink:
    """Создать персональную ссылку.

    :param code: код в ссылке. Пусто — сгенерируем и проверим на уникальность.
    :param days: срок действия в днях. 0 — бессрочно.
    """
    clean_code = normalize_code(code) or await _unique_code(session)
    validate(title, clean_code, discount_percent, discount_max_rub, uses_limit)

    if await session.scalar(select(PersonalLink.id).where(PersonalLink.code == clean_code)) is not None:
        raise PersonalLinkError(f"Ссылка с кодом «{clean_code}» уже есть")
    if await session.scalar(select(PromoCode.id).where(PromoCode.code == promo_code_for(clean_code))) is not None:
        raise PersonalLinkError(f"Код «{promo_code_for(clean_code)}» уже занят — выбери другой")

    if partner_id is not None:
        partner = await session.get(Partner, partner_id)
        if partner is None:
            raise PersonalLinkError("Партнёр не найден")

    link = PersonalLink(
        code=clean_code,
        title=title.strip()[:64],
        owner_name=(owner_name or "").strip()[:64],
        partner_id=partner_id,
        discount_percent=max(0, min(100, int(discount_percent))),
        discount_max_rub=max(0, int(discount_max_rub)),
        uses_limit=max(0, int(uses_limit)),
        expires_at=(datetime.now(timezone.utc) + timedelta(days=days)) if days else None,
        is_active=True,
        note=(note or "").strip()[:200] or None,
    )
    session.add(link)
    await session.flush()

    # Код-двойник: тот же процент, чтобы человек получил одинаковые условия,
    # ввёл он ссылку или продиктованный код.
    session.add(
        PromoCode(
            code=promo_code_for(link.code),
            kind="personal",
            personal_link_id=link.id,
            percent=link.discount_percent,
            max_discount_rub=link.discount_max_rub,
            first_only=True,
            uses_limit=link.uses_limit,
            is_active=True,
            note=f"персональная ссылка: {link.title}",
        )
    )
    await session.flush()
    await events.log_event(
        session,
        events.PERSONAL_LINK_CREATED,
        payload={
            "link_id": link.id,
            "code": link.code,
            "discount_percent": link.discount_percent,
            "partner_id": partner_id,
        },
    )
    return link


async def _unique_code(session: AsyncSession) -> str:
    """Подобрать свободный код: 7 символов из алфавита без похожих букв."""
    for _ in range(12):
        candidate = new_code()
        exists = await session.scalar(select(PersonalLink.id).where(PersonalLink.code == candidate))
        if exists is None:
            return candidate
    # Практически недостижимо, но лучше длинный код, чем падение.
    return new_code(12)


async def update_link(
    session: AsyncSession,
    link: PersonalLink,
    *,
    title: str | None = None,
    discount_percent: int | None = None,
    discount_max_rub: int | None = None,
    uses_limit: int | None = None,
    is_active: bool | None = None,
    note: str | None = None,
) -> PersonalLink:
    """Изменить условия ссылки. Код не меняем: ссылка уже отправлена человеку."""
    new_title = link.title if title is None else title.strip()[:64]
    new_discount = link.discount_percent if discount_percent is None else int(discount_percent)
    new_cap = link.discount_max_rub if discount_max_rub is None else int(discount_max_rub)
    new_limit = link.uses_limit if uses_limit is None else int(uses_limit)
    validate(new_title, link.code, new_discount, new_cap, new_limit)

    link.title = new_title
    link.discount_percent = max(0, min(100, new_discount))
    link.discount_max_rub = max(0, new_cap)
    link.uses_limit = max(0, new_limit)
    if is_active is not None:
        link.is_active = bool(is_active)
    if note is not None:
        link.note = note.strip()[:200] or None
    await session.flush()

    # Код-двойник держим синхронно: условия должны совпадать.
    promo_row = await promo_for_link(session, link.id)
    if promo_row is not None:
        promo_row.percent = link.discount_percent
        promo_row.max_discount_rub = link.discount_max_rub
        promo_row.uses_limit = link.uses_limit
        promo_row.is_active = link.is_active and not link.is_expired
        await session.flush()
    return link


async def promo_for_link(session: AsyncSession, link_id: int) -> PromoCode | None:
    """Код-двойник персональной ссылки."""
    return await session.scalar(
        select(PromoCode).where(PromoCode.personal_link_id == link_id).order_by(PromoCode.id).limit(1)
    )


# ------------------------------------------------------------------ поиск и активация
async def get_by_code(session: AsyncSession, code: str) -> PersonalLink | None:
    """Найти действующую персональную ссылку по коду."""
    clean = normalize_code(code)
    if not clean:
        return None
    link = await session.scalar(select(PersonalLink).where(PersonalLink.code == clean))
    if link is None or not link.is_usable:
        return None
    return link


async def list_links(session: AsyncSession, limit: int = 200) -> list[PersonalLink]:
    """Все персональные ссылки: свежие сверху."""
    stmt = select(PersonalLink).order_by(PersonalLink.is_active.desc(), PersonalLink.id.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())


async def attach_link(session: AsyncSession, user: User, code: str) -> PersonalLink | None:
    """Привязать человека к персональной ссылке и засчитать активацию.

    Работает один раз: если человек уже закреплён за ссылкой или партнёром,
    повторный переход ничего не меняет — иначе лимит активаций можно было бы
    «съесть» повторными заходами.
    """
    if user.personal_link_id is not None:
        return None
    link = await get_by_code(session, code)
    if link is None:
        return None

    user.personal_link_id = link.id
    user.source = "personal"
    user.source_detail = link.code
    user.source_at = utcnow()
    link.uses_count = (link.uses_count or 0) + 1
    # Персональная ссылка конкретного человека: заслуга идёт и партнёру, если он есть.
    if link.partner_id is not None and user.partner_id is None:
        user.partner_id = link.partner_id
    await session.flush()
    await events.log_event(
        session,
        events.PERSONAL_LINK_USED,
        user_id=user.id,
        payload={"link_id": link.id, "code": link.code, "uses_count": link.uses_count},
    )
    return link


# ------------------------------------------------------------------ статистика
@dataclass(slots=True)
class LinkStats:
    """Что принесла конкретная персональная ссылка.

    :param joined: сколько человек пришло по ссылке.
    :param payers: сколько из них оплатило.
    :param orders: число оплаченных заказов.
    :param revenue_rub: выручка с этих заказов.
    :param discount_rub: сколько мы отдали скидкой — цена акции.
    """

    link: PersonalLink
    joined: int
    payers: int
    orders: int
    revenue_rub: float
    discount_rub: float

    @property
    def conversion(self) -> float:
        """Доля пришедших, дошедших до оплаты."""
        return self.payers / self.joined if self.joined else 0.0

    @property
    def cost_rub(self) -> float:
        """Во что обошлась ссылка: только скидка, отдельных выплат нет."""
        return self.discount_rub

    @property
    def cac_rub(self) -> float:
        """Цена платящего клиента из этой ссылки."""
        if not self.payers:
            return float("inf")
        return self.cost_rub / self.payers

    @property
    def roi(self) -> float:
        """Сколько рублей выручки на каждый рубль скидки."""
        if self.cost_rub <= 0:
            return float("inf") if self.revenue_rub > 0 else 0.0
        return self.revenue_rub / self.cost_rub


async def link_stats(session: AsyncSession, link: PersonalLink) -> LinkStats:
    """Собрать статистику одной ссылки."""
    joined = await session.scalar(
        select(func.count(User.id)).where(User.personal_link_id == link.id)
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
            .where(Order.status == "paid", User.personal_link_id == link.id)
        )
    ).one()
    return LinkStats(
        link=link,
        joined=int(joined),
        orders=int(row[0] or 0),
        payers=int(row[1] or 0),
        revenue_rub=float(row[2] or 0),
        discount_rub=float(row[3] or 0),
    )


async def all_link_stats(session: AsyncSession, limit: int = 200) -> list[LinkStats]:
    """Статистика по всем персональным ссылкам."""
    return [await link_stats(session, link) for link in await list_links(session, limit)]


async def link_totals(stats: list[LinkStats]) -> dict[str, float]:
    """Итоги по ссылкам: сколько привели и сколько стоило."""
    return {
        "links": float(len(stats)),
        "joined": float(sum(row.joined for row in stats)),
        "payers": float(sum(row.payers for row in stats)),
        "revenue_rub": sum(row.revenue_rub for row in stats),
        "discount_rub": sum(row.discount_rub for row in stats),
    }


async def users_of_link(session: AsyncSession, link: PersonalLink, limit: int = 100) -> list[dict]:
    """Кто пришёл по ссылке и сколько заплатил."""
    rows = (
        await session.execute(
            select(User, func.count(Order.id), func.coalesce(func.sum(Order.amount_rub), 0))
            .outerjoin(Order, (Order.user_id == User.id) & (Order.status == "paid"))
            .where(User.personal_link_id == link.id)
            .group_by(User.id)
            .order_by(func.coalesce(func.sum(Order.amount_rub), 0).desc(), User.id.desc())
            .limit(limit)
        )
    ).all()
    return [
        {"user": user, "orders": int(orders or 0), "revenue_rub": float(revenue or 0)}
        for user, orders, revenue in rows
    ]
