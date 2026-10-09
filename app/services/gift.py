"""Подарочные сертификаты: подписка в подарок с активацией позже.

Зачем отдельная механика, а не просто промокод. Подарок продаёт то, чего нет
у обычной подписки:

  * **можно активировать позже** — снимает возражение «мне сейчас не надо»;
  * **покупает другой человек** — приходит тот, кто сам бы не пришёл;
  * **цена выше обычной** — это подарок, а не скидка: наценка поднимает чек.

Экономика (``docs/МАРКЕТИНГ-ЭКОНОМИКА.md``): себестоимость обслуживания одного
клиента — около 10 ₽/мес при ноде 459 ₽ на 45 человек, поэтому подарок в
днях подписки стоит кассе в разы меньше, чем выглядит для получателя.

Что считается «активацией»: получатель открывает ссылку ``?start=gift_<токен>``
и получает дни. Если подписки у него нет — дни ложатся в накопительный баланс
и применятся к первой оплате; если есть — срок продлевается сразу. Покупателю
после активации начисляется небольшой бонус: подарок должен радовать дважды.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Order, User, utcnow
from app.panels.base import PanelClient
from app.services import events, orders, subscriptions

settings = get_settings()

#: Префикс токена подарка. Видно, что это подарок, а не промокод.
TOKEN_PREFIX = "KOMETA-GIFT-"

#: Алфавит без похожих символов (0/O, 1/I/L): код диктуют голосом и переписывают.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


class GiftError(Exception):
    """Подарок нельзя выдать: с причиной, понятной для показа клиенту."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def new_gift_token() -> str:
    """Сгенерировать токен подарка."""
    body = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"{TOKEN_PREFIX}{body}"


def normalize_token(raw: str) -> str:
    """Привести введённый код к каноническому виду.

    Человек может прислать код без префикса, в нижнем регистре и с пробелами —
    всё это должно работать, иначе подарок «не активируется».
    """
    value = " ".join((raw or "").split()).upper()
    if not value:
        return ""
    if value.startswith(TOKEN_PREFIX):
        return value
    if value.startswith("GIFT-"):
        return TOKEN_PREFIX + value[len("GIFT-"):]
    # Прислали только тело кода — достраиваем префикс.
    if len(value) == 8 and all(ch in ALPHABET for ch in value):
        return TOKEN_PREFIX + value
    return value


def gift_price_rub(plan_price_rub: int) -> int:
    """Цена подарочного сертификата: цена тарифа плюс наценка за подарок."""
    markup = max(0, settings.gift_markup_percent)
    return plan_price_rub + round(plan_price_rub * markup / 100)


def gift_price_stars(plan_price_stars: int) -> int:
    """Цена подарка в звёздах — с той же наценкой, что и в рублях."""
    markup = max(0, settings.gift_markup_percent)
    return plan_price_stars + round(plan_price_stars * markup / 100)


def is_gift_order(order: Order) -> bool:
    """Подарочный ли это заказ."""
    return bool(order.gift_token)


def gift_expires_at(bought_at: datetime | None = None) -> datetime:
    """До какого момента сертификат можно активировать."""
    base = bought_at or datetime.now(timezone.utc)
    return base + timedelta(days=max(1, settings.gift_valid_days))


def gift_link(bot_username: str, token: str) -> str:
    """Ссылка для получателя подарка."""
    return f"https://t.me/{bot_username}?start=gift_{token}"


@dataclass(slots=True)
class GiftActivation:
    """Результат активации подарка."""

    order: Order
    days: int
    buyer: User | None
    buyer_days: int
    applied_to_active_subscription: bool
    already_activated: bool = False


async def attach_gift(
    session: AsyncSession,
    order: Order,
    *,
    recipient_tg_id: int | None = None,
    message: str = "",
) -> Order:
    """Превратить обычный заказ в подарочный.

    Вызывается сразу после создания заказа: пока он не оплачен, токен никому
    не показывается — это делает вызывающий код уже после подтверждения оплаты.
    """
    token = new_gift_token()
    # Токен уникален в пределах таблицы; при коллизии (практически невозможной)
    # генерируем заново, а не падаем на оплаченном заказе.
    for _ in range(5):
        exists = await session.scalar(select(Order.id).where(Order.gift_token == token))
        if exists is None:
            break
        token = new_gift_token()

    order.gift_token = token
    order.gift_recipient_tg_id = int(recipient_tg_id) if recipient_tg_id else None
    order.gift_message = (message or "").strip()[:200] or None
    await session.flush()
    return order


async def get_by_token(session: AsyncSession, token: str) -> Order | None:
    """Найти подарочный заказ по токену (в любом регистре и без префикса)."""
    normalized = normalize_token(token)
    if not normalized:
        return None
    return await session.scalar(
        select(Order).where(Order.gift_token == normalized, Order.status == "paid")
    )


async def redeem(
    session: AsyncSession,
    order: Order,
    recipient: User,
    panel: PanelClient,
) -> GiftActivation:
    """Выдать подарок получателю.

    Идемпотентность: повторная активация тем же человеком не выдаёт дни второй
    раз, а возвращает уже применённый подарок — иначе один сертификат можно
    «погасить» многократно перезапуском ссылки.
    """
    if not order.gift_token:
        raise GiftError("not_gift", "Этот заказ не подарочный.")
    if order.status != "paid":
        raise GiftError("not_paid", "Подарок ещё не оплачен.")
    if order.gift_activated_at is not None:
        if order.gift_activated_by == recipient.id:
            return GiftActivation(
                order=order,
                days=0,
                buyer=await session.get(User, order.user_id),
                buyer_days=0,
                applied_to_active_subscription=False,
                already_activated=True,
            )
        raise GiftError("already_activated", "Этот подарок уже активировали.")
    if order.user_id == recipient.id:
        raise GiftError("self_activation", "Свой же подарок активировать нельзя — отправь его другу.")
    if gift_expires_at(order.created_at) < utcnow():
        raise GiftError("expired", "Срок активации подарка истёк.")

    plan = await orders.get_plan(session, order.plan_id) if order.plan_id else None
    days = plan.days if plan is not None else 30

    sub = await subscriptions.get_subscription(session, recipient.id)
    had_active = bool(sub is not None and sub.is_active)
    await subscriptions.add_bonus_days(session, recipient, days, panel, reason="gift_activated")

    order.gift_activated_at = utcnow()
    order.gift_activated_by = recipient.id
    await session.flush()

    await events.log_event(
        session,
        events.GIFT_ACTIVATED,
        user_id=recipient.id,
        payload={
            "order_id": order.id,
            "token": order.gift_token,
            "days": days,
            "buyer_id": order.user_id,
        },
    )

    # Покупателю — спасибо днями: подарок должен радовать дважды, а нам важно,
    # чтобы человек дарил ещё (LTV покупателя растёт без нового привлечения).
    buyer = await session.get(User, order.user_id)
    buyer_days = 0
    if buyer is not None and settings.gift_buyer_bonus_days > 0:
        buyer_sub = await subscriptions.get_subscription(session, buyer.id)
        before_expires = buyer_sub.expires_at if buyer_sub is not None else None
        before_balance = int(buyer.bonus_days_balance or 0)
        await subscriptions.add_bonus_days(
            session, buyer, settings.gift_buyer_bonus_days, panel, reason="gift_sent"
        )
        # Считаем фактически начисленное, а не запрошенное: дни могли уйти
        # в срок подписки (если она есть) или в запас (если подписки нет).
        # Обещать клиенту надо то, что он реально получил.
        if buyer_sub is not None and buyer_sub.expires_at and before_expires is not None:
            buyer_days = max(0, (buyer_sub.expires_at - before_expires).days)
        if not buyer_days:
            buyer_days = max(0, int(buyer.bonus_days_balance or 0) - before_balance)

    return GiftActivation(
        order=order,
        days=days,
        buyer=buyer,
        buyer_days=buyer_days,
        applied_to_active_subscription=had_active,
    )


async def active_gifts(session: AsyncSession, limit: int = 50) -> list[Order]:
    """Последние подарки — для админки: кто купил, активирован ли."""
    rows = await session.execute(
        select(Order)
        .where(Order.gift_token.is_not(None))
        .order_by(Order.id.desc())
        .limit(limit)
    )
    return list(rows.scalars().all())


async def gift_stats(session: AsyncSession) -> dict[str, int]:
    """Сводка по подаркам: куплено, оплачено, активировано."""
    bought = await session.scalar(select(func.count(Order.id)).where(Order.gift_token.is_not(None))) or 0
    paid = (
        await session.scalar(
            select(func.count(Order.id)).where(Order.gift_token.is_not(None), Order.status == "paid")
        )
        or 0
    )
    activated = (
        await session.scalar(
            select(func.count(Order.id)).where(Order.gift_activated_at.is_not(None))
        )
        or 0
    )
    return {"bought": int(bought), "paid": int(paid), "activated": int(activated)}


# ------------------------------------------------------------------ уведомления
def certificate_text(link: str, token: str, plan_title: str, message: str = "") -> str:
    """Текст открытки для покупателя: что подарил и как передать.

    Покупатель должен получить готовую ссылку и код — иначе он не понимает,
    что делать дальше, и подарок остаётся неактивированным.
    """
    note = f"\n\n💬 <i>{message}</i>" if message else ""
    return (
        "🎁 <b>Подарок готов!</b>\n\n"
        f"Ты оформил подарочный сертификат: <b>{plan_title}</b>.\n"
        f"Он ждёт получателя и не сгорает {settings.gift_valid_days} дней.{note}\n\n"
        "<b>Как передать</b>\n"
        "1️⃣ Перешли это сообщение тому, кого поздравляешь — или отправь ссылку ниже.\n"
        "2️⃣ Он открывает ссылку и нажимает «Активировать».\n"
        "3️⃣ С этого момента идёт срок подписки, а тебе капает "
        f"<b>+{settings.gift_buyer_bonus_days} дней</b> в благодарность.\n\n"
        f"<b>Ссылка-подарок</b>\n<code>{link}</code>\n\n"
        f"<b>Код</b>, если ссылку неудобно: <code>{token}</code>\n\n"
        "<i>Активировать может только другой человек: свой подарок себе оставить нельзя.</i>"
    )


async def notify_buyer(session: AsyncSession, order: Order, bot, buyer: User | None = None) -> bool:  # noqa: ANN001
    """Отправить покупателю готовый сертификат. True — сообщение ушло."""
    if not order.gift_token:
        return False
    buyer = buyer or await session.get(User, order.user_id)
    if buyer is None or buyer.is_blocked:
        return False
    plan = await orders.get_plan(session, order.plan_id) if order.plan_id else None
    me = await bot.get_me()
    link = gift_link(me.username, order.gift_token)
    text = certificate_text(
        link,
        order.gift_token,
        plan.title if plan is not None else "подписка",
        order.gift_message or "",
    )
    try:
        await bot.send_message(buyer.tg_id, text, disable_web_page_preview=True)
    except Exception:  # noqa: BLE001 - бот мог быть заблокирован
        return False
    return True


def activation_text(activation: "GiftActivation", recipient_name: str = "") -> str:
    """Текст получателю подарка: сколько дней и что делать дальше."""
    if activation.already_activated:
        return (
            "🎁 Этот подарок уже активирован — дни начислены ранее.\n\n"
            "Проверить срок можно в разделе «Моя подписка»."
        )
    where = (
        "Срок подписки продлён — можно подключаться."
        if activation.applied_to_active_subscription
        else "Дни добавлены в запас и применятся при первой оплате."
    )
    return (
        "🎁 <b>Тебе подарок!</b>\n\n"
        f"Активировано <b>{activation.days} дней</b> защищённого подключения.\n"
        f"{where}\n\n"
        "Если ещё не подключался — открой «Как подключить»: это две минуты."
    )


def buyer_thanks_text(activation: "GiftActivation") -> str:
    """Текст покупателю, когда подарок активировали."""
    tail = (
        f"Тебе начислено <b>+{activation.buyer_days} дней</b> — подарок сработал дважды 🎉"
        if activation.buyer_days
        else "Спасибо, что делишься сервисом!"
    )
    return (
        "🎉 <b>Твой подарок активировали!</b>\n\n"
        f"{tail}\n\n"
        "Можешь дарить ещё — сертификат можно оформить в любой момент в главном меню."
    )
