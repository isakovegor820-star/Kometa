"""Промокоды и скидка на первую оплату.

Правила, которые здесь закодированы:

  * у каждого пользователя есть персональный код ``KOMETA-<его код>`` —
    его друг может ввести руками, если потерял ссылку;
  * скидка даётся **один раз в жизни аккаунта** (уникальный ``user_id``
    в ``promo_redemptions``) и только на первую оплату;
  * пришёл по реферальной ссылке — скидка применяется сама, код вводить
    не нужно;
  * админ может создать обычный код (акция, компенсация) с любым процентом,
    лимитом активаций и сроком;
  * скидка считается от цены тарифа, округляется в пользу клиента и может
    упираться в потолок ``max_discount_rub``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Order, PromoCode, PromoRedemption, User

logger = logging.getLogger(__name__)
settings = get_settings()

#: Префикс персонального кода. Читается человеком и не путается с тарифом.
CODE_PREFIX = "KOMETA-"

#: Почему скидка не применена — тексты показывает бот.
REASON_NOT_FOUND = "not_found"
REASON_INACTIVE = "inactive"
REASON_EXPIRED = "expired"
REASON_USES_OVER = "uses_over"
REASON_ALREADY_USED = "already_used"
REASON_NOT_FIRST = "not_first"
REASON_SELF = "self"


@dataclass(slots=True)
class Discount:
    """Готовая скидка для конкретного заказа."""

    promo: PromoCode | None
    code: str | None
    percent: int
    base_rub: int
    discount_rub: int

    @property
    def amount_rub(self) -> int:
        return max(0, self.base_rub - self.discount_rub)

    def stars_for(self, price_stars: int) -> int:
        """Сколько звёзд заплатить: скидка переносится на звёздную цену."""
        if price_stars <= 0 or self.base_rub <= 0 or self.discount_rub <= 0:
            return price_stars
        cut = round(price_stars * self.discount_rub / self.base_rub)
        return max(1, price_stars - cut)


# ------------------------------------------------------------------ утилиты
def normalize(raw: str | None) -> str:
    """Привести введённый код к виду хранения: без пробелов, в верхнем регистре."""
    if not raw:
        return ""
    return "".join(raw.split()).upper()


def code_for_referral(referral_code: str) -> str:
    """Персональный промокод по реферальному коду пользователя."""
    return f"{CODE_PREFIX}{referral_code.upper()}"


def calc_discount_rub(base_rub: int, percent: int, max_rub: int = 0) -> int:
    """Скидка в рублях: целые рубли, округление скидки вниз.

    Рубли целые, потому что все платёжные провайдеры и автоподтверждение
    переводов работают с целыми суммами (копейки — только уникальная
    надбавка для сопоставления платежа). Округляем вниз, чтобы цена
    выглядела круглой: 199 ₽ − 99 ₽ = 100 ₽.
    """
    if base_rub <= 0 or percent <= 0:
        return 0
    percent = min(100, percent)
    discount = base_rub * percent // 100
    if max_rub > 0:
        discount = min(discount, max_rub)
    return max(0, min(discount, base_rub))


# ------------------------------------------------------------------ поиск
async def get_by_code(session: AsyncSession, raw: str) -> PromoCode | None:
    """Найти промокод. Персональный код создаётся лениво.

    Владелец мог ни разу не открыть экран «Пригласи друга», и записи ещё нет:
    друг вводит ``KOMETA-XXXX`` — значит, код надо создать на месте, иначе
    человек увидит «такого промокода нет» на рабочем коде.
    """
    code = normalize(raw)
    if not code:
        return None
    existing = await session.scalar(select(PromoCode).where(PromoCode.code == code))
    if existing is not None:
        return existing
    if not code.startswith(CODE_PREFIX):
        return None

    referral_code = code[len(CODE_PREFIX):]
    owner = await session.scalar(select(User).where(User.referral_code == referral_code))
    if owner is None:
        # Реферальные коды хранятся в смешанном регистре, а промокод — в верхнем.
        owner = await session.scalar(
            select(User).where(func.upper(User.referral_code) == referral_code.upper())
        )
    if owner is None:
        return None
    return await ensure_referral_code(session, owner)


async def ensure_referral_code(session: AsyncSession, user: User) -> PromoCode:
    """Вернуть (при необходимости создать) персональный код пользователя.

    Создаётся лениво: у аккаунтов, заведённых до появления промокодов,
    записи ещё нет.
    """
    code = code_for_referral(user.referral_code)
    # Прямой запрос, а не get_by_code: тот сам умеет создавать код и
    # получилась бы рекурсия.
    existing = await session.scalar(select(PromoCode).where(PromoCode.code == code))
    if existing is not None:
        return existing

    promo = PromoCode(
        code=code,
        kind="referral",
        owner_user_id=user.id,
        percent=settings.referral_discount_percent,
        max_discount_rub=settings.referral_discount_max_rub,
        first_only=True,
        uses_limit=settings.promo_referral_uses_limit,
        is_active=True,
        note=f"реферальный код {user.display_name}",
    )
    session.add(promo)
    await session.flush()
    return promo


# ------------------------------------------------------------------ проверки
async def has_used_discount(session: AsyncSession, user: User) -> bool:
    """Пользователь уже пользовался скидкой (хотя бы раз)."""
    found = await session.scalar(select(PromoRedemption.id).where(PromoRedemption.user_id == user.id))
    return found is not None


async def has_paid_order(session: AsyncSession, user: User) -> bool:
    """Была ли у пользователя хотя бы одна оплаченная покупка."""
    found = await session.scalar(
        select(Order.id).where(Order.user_id == user.id, Order.status == "paid").limit(1)
    )
    return found is not None


async def check_code(
    session: AsyncSession,
    user: User,
    raw: str,
) -> tuple[PromoCode | None, str | None]:
    """Проверить код для конкретного пользователя.

    :returns: (промокод, причина отказа) — ровно одно из двух непустое.
    """
    promo = await get_by_code(session, raw)
    if promo is None:
        return None, REASON_NOT_FOUND
    reason = await _reject_reason(session, user, promo)
    return (None, reason) if reason else (promo, None)


async def _reject_reason(session: AsyncSession, user: User, promo: PromoCode) -> str | None:
    if not promo.is_active:
        return REASON_INACTIVE
    if promo.expires_at is not None and promo.expires_at <= datetime.now(timezone.utc):
        return REASON_EXPIRED
    if promo.uses_limit and (promo.uses_count or 0) >= promo.uses_limit:
        return REASON_USES_OVER
    if promo.kind == "referral" and promo.owner_user_id == user.id:
        return REASON_SELF
    if await has_used_discount(session, user):
        return REASON_ALREADY_USED
    if promo.first_only and await has_paid_order(session, user):
        return REASON_NOT_FIRST
    return None


# ------------------------------------------------------------------ применение
def make_discount(promo_row: PromoCode, base_rub: int) -> Discount:
    """Собрать скидку для конкретной цены."""
    return Discount(
        promo=promo_row,
        code=promo_row.code,
        percent=promo_row.percent,
        base_rub=base_rub,
        discount_rub=calc_discount_rub(base_rub, promo_row.percent, promo_row.max_discount_rub),
    )


async def available(session: AsyncSession, user: User, raw: str | None = None) -> PromoCode | None:
    """Какой промокод применится к заказу прямо сейчас.

    Порядок: код, введённый руками → скидка за приглашение (если человек
    пришёл по ссылке) → ничего. Введённый руками код имеет приоритет:
    человек сделал действие осознанно.
    """
    candidates: list[str] = []
    manual = normalize(raw) or normalize(user.promo_code)
    if manual:
        candidates.append(manual)

    if user.referred_by:
        referrer = await session.get(User, user.referred_by)
        if referrer is not None and not referrer.is_blocked:
            candidates.append(code_for_referral(referrer.referral_code))

    for code in candidates:
        promo_row, reason = await check_code(session, user, code)
        if promo_row is not None:
            return promo_row
        logger.info("Промокод %s не применён (%s), user=%s", code, reason, user.id)
    return None


async def resolve(
    session: AsyncSession,
    user: User,
    *,
    base_rub: int,
    raw: str | None = None,
) -> Discount | None:
    """Подобрать и посчитать скидку для конкретного заказа."""
    promo_row = await available(session, user, raw)
    return make_discount(promo_row, base_rub) if promo_row is not None else None


async def redeem(
    session: AsyncSession,
    promo: PromoCode,
    user: User,
    *,
    order: Order,
    discount_rub: int,
) -> PromoRedemption | None:
    """Зафиксировать использование скидки после оплаты заказа.

    Один пользователь — одна скидка: если запись уже есть (например, человек
    оплатил два заказа со скидкой подряд), второй раз не начисляем.
    """
    existing = await session.scalar(select(PromoRedemption).where(PromoRedemption.user_id == user.id))
    if existing is not None:
        if existing.order_id == order.id:
            existing.confirmed_at = existing.confirmed_at or datetime.now(timezone.utc)
        logger.info("Скидка уже использована пользователем %s, повторно не фиксируем", user.id)
        return None

    redemption = PromoRedemption(
        promo_id=promo.id,
        user_id=user.id,
        order_id=order.id,
        code=promo.code,
        discount_rub=discount_rub,
        confirmed_at=datetime.now(timezone.utc),
    )
    session.add(redemption)
    promo.uses_count = (promo.uses_count or 0) + 1
    await session.flush()
    return redemption


# ------------------------------------------------------------------ админка
async def create_admin_code(
    session: AsyncSession,
    code: str,
    *,
    percent: int,
    uses_limit: int = 0,
    days: int = 0,
    first_only: bool = True,
    max_discount_rub: int = 0,
    note: str | None = None,
) -> PromoCode:
    """Создать обычный (не реферальный) промокод руками владельца."""
    from datetime import timedelta

    normalized = normalize(code)
    if not normalized:
        raise ValueError("пустой код")
    existing = await session.scalar(select(PromoCode).where(PromoCode.code == normalized))
    if existing is not None:
        raise ValueError("такой код уже есть")

    promo_row = PromoCode(
        code=normalized,
        kind="admin",
        percent=max(1, min(100, percent)),
        max_discount_rub=max(0, max_discount_rub),
        first_only=first_only,
        uses_limit=max(0, uses_limit),
        is_active=True,
        expires_at=(
            datetime.now(timezone.utc) + timedelta(days=days) if days else None
        ),
        note=note,
    )
    session.add(promo_row)
    await session.flush()
    return promo_row


async def list_codes(session: AsyncSession, limit: int = 200) -> list[PromoCode]:
    stmt = select(PromoCode).order_by(PromoCode.uses_count.desc(), PromoCode.id.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())


async def program_stats(session: AsyncSession) -> dict:
    """Сводка по промокодам для админки."""
    codes = await session.scalar(select(func.count(PromoCode.id))) or 0
    active = await session.scalar(select(func.count(PromoCode.id)).where(PromoCode.is_active.is_(True))) or 0
    used = await session.scalar(select(func.count(PromoRedemption.id))) or 0
    discount = await session.scalar(
        select(func.coalesce(func.sum(PromoRedemption.discount_rub), 0))
    ) or 0
    return {
        "codes": int(codes),
        "active": int(active),
        "used": int(used),
        "discount_total": int(discount),
    }
