"""Автосценарии в боте: подсказки, возврат клиентов и апселлы.

Зачем это нужно. Рефералка и подарки приводят людей, но дальше с ними ничего
не происходит: человек взял пробный доступ, не оплатил — и всё, он больше
никогда не услышит о сервисе. Напоминания за 3 и 1 день до конца подписки
есть, но они работают только для тех, кто уже платит.

Здесь — четыре сценария, которые заменяют ручную работу владельца:

  * **trial_no_payment** — человек попробовал и не купил: напоминаем о тарифах
    (это самый дешёвый рост: он уже видел сервис в работе);
  * **winback** — подписка кончилась неделю назад: возвращаем без давления;
  * **upsell** — человек платит месяц за месяцем: предлагаем тариф подлиннее,
    где месяц дешевле (растёт LTV и падает отток);
  * **referral** — активный клиент: напоминаем про дни за друзей.

Главное правило — **не спамить**. Между любыми двумя сообщениями держим паузу
(``lifecycle_min_gap_days``), каждый сценарий срабатывает один раз на человека,
а факт отправки пишется в событие. Это защищает бота от жалоб и блокировок,
а нас — от потери аудитории.

Экономика: каждое сообщение стоит ноль рублей, поэтому даже 5 % отклика дают
клиентов дешевле любого платного канала (см. ``docs/МАРКЕТИНГ-ЭКОНОМИКА.md``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Order, Plan, Subscription, User, utcnow
from app.services import events

settings = get_settings()

#: Коды сценариев. Пишутся в события и в ``User.last_lifecycle_kind``.
KIND_TRIAL_NO_PAYMENT = "trial_no_payment"
KIND_WINBACK = "winback"
KIND_UPSELL = "upsell"
KIND_REFERRAL = "referral"


@dataclass(slots=True)
class Scenario:
    """Один автосценарий: кого берём и что говорим.

    :param kind: код сценария (для статистики и защиты от повторов).
    :param title: название для отчёта.
    :param text: текст сообщения.
    """

    kind: str
    title: str
    text: str


def scenarios() -> tuple[Scenario, ...]:
    """Сценарии с подставленными настройками: тексты собираются один раз."""
    return (
        Scenario(
            kind=KIND_TRIAL_NO_PAYMENT,
            title="Попробовал и не оплатил",
            text=(
                "👋 Ты пробовал Kometa — как впечатления?\n\n"
                "Если всё устроило, доступ можно вернуть в один клик: тарифы от "
                "120 ₽ в месяц, до 3 устройств, трафик без ограничений.\n\n"
                "Если что-то не получилось — напиши в поддержку, разберёмся: "
                "обычно дело в одном шаге при подключении."
            ),
        ),
        Scenario(
            kind=KIND_WINBACK,
            title="Подписка закончилась",
            text=(
                "⌛️ Подписка закончилась, доступ отключён.\n\n"
                "Всё сохранено: настройки в приложении менять не нужно, после оплаты "
                "доступ вернётся сразу.\n\n"
                "Если что-то не устраивало — расскажи, что именно. Ответим честно: "
                "иногда проблема решается сменой локации."
            ),
        ),
        Scenario(
            kind=KIND_UPSELL,
            title="Пора на длинный тариф",
            text=(
                "💎 Ты с нами уже месяц — спасибо!\n\n"
                "На длинных тарифах месяц выходит дешевле: 3 месяца — 100 ₽/мес, "
                "6 месяцев — 90 ₽/мес, год — 79 ₽/мес вместо 120 ₽.\n\n"
                "Так спокойнее: не нужно вспоминать про продление каждый месяц."
            ),
        ),
        Scenario(
            kind=KIND_REFERRAL,
            title="Напоминание про друзей",
            text=(
                "👥 Друзья часто спрашивают, чем ты пользуешься для стабильного интернета?\n\n"
                "Поделись ссылкой из раздела «Пригласить друга»: друг получит скидку "
                f"{settings.referral_discount_percent} % на первый месяц, "
                f"а тебе капнут {settings.referral_bonus_days_referrer} дней — "
                "и ещё столько же за каждое его продление."
            ),
        ),
    )


async def _sent_before(session: AsyncSession, user_id: int, kind: str) -> bool:
    """Отправляли ли этому человеку этот сценарий раньше."""
    count = await session.scalar(
        select(func.count(events.Event.id)).where(
            events.Event.user_id == user_id,
            events.Event.kind == events.LIFECYCLE_SENT,
            events.Event.payload.like(f'%"kind": "{kind}"%'),
        )
    )
    return bool(count)


def _too_soon(user: User, now: datetime, gap_days: int) -> bool:
    """Не беспокоили ли человека в последние N дней."""
    if user.last_lifecycle_at is None:
        return False
    last = user.last_lifecycle_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) < timedelta(days=max(0, gap_days))


async def _trial_without_payment(session: AsyncSession, now: datetime) -> list[User]:
    """Взял пробный доступ, срок истёк, оплат не было."""
    days = settings.lifecycle_trial_days
    threshold = now - timedelta(days=days)
    stmt = (
        select(User)
        .join(Subscription, Subscription.user_id == User.id)
        .where(
            User.is_blocked.is_(False),
            Subscription.status == "trial",
            Subscription.expires_at.is_not(None),
            Subscription.expires_at <= threshold,
        )
    )
    candidates = list((await session.execute(stmt)).scalars().all())
    return [user for user in candidates if not await _has_paid(session, user.id)]


async def _has_paid(session: AsyncSession, user_id: int) -> bool:
    """Платил ли человек хотя бы раз."""
    count = await session.scalar(
        select(func.count(Order.id)).where(Order.user_id == user_id, Order.status == "paid")
    )
    return bool(count)


async def _winback(session: AsyncSession, now: datetime) -> list[User]:
    """Подписка кончилась N дней назад — предлагаем вернуться."""
    days = settings.lifecycle_winback_days
    stmt = (
        select(User)
        .join(Subscription, Subscription.user_id == User.id)
        .where(
            User.is_blocked.is_(False),
            Subscription.status == "expired",
            Subscription.expires_at.is_not(None),
            Subscription.expires_at <= now - timedelta(days=days),
        )
    )
    return list((await session.execute(stmt)).scalars().all())


async def _upsell(session: AsyncSession, now: datetime) -> list[User]:
    """Платит коротким тарифом дольше N дней — предлагаем длинный."""
    days = settings.lifecycle_upsell_days
    threshold = now - timedelta(days=days)
    # Короткий тариф определяем по сроку действия: до 45 дней. Длинные тарифы
    # апселлу не подлежат — предлагать год тому, кто уже купил год, бессмысленно.
    short_plan = (
        select(Plan.id)
        .where(Plan.id == Subscription.plan_id, Plan.days <= 45)
        .exists()
    )
    stmt = (
        select(User)
        .join(Subscription, Subscription.user_id == User.id)
        .where(
            User.is_blocked.is_(False),
            Subscription.status == "active",
            Subscription.expires_at.is_not(None),
            Subscription.expires_at > now,
            Subscription.plan_id.is_not(None),
            short_plan,
            Subscription.starts_at <= threshold,
        )
    )
    return list((await session.execute(stmt)).scalars().all())


async def _referral_nudge(session: AsyncSession, now: datetime) -> list[User]:
    """Активный клиент без приглашённых — напоминаем про программу."""
    threshold = now - timedelta(days=settings.lifecycle_referral_days)
    invited = select(func.count(events.Event.id)).where(
        events.Event.user_id == User.id, events.Event.kind == events.REFERRAL_JOINED
    )
    stmt = (
        select(User)
        .join(Subscription, Subscription.user_id == User.id)
        .where(
            User.is_blocked.is_(False),
            Subscription.status == "active",
            Subscription.expires_at > now,
            Subscription.starts_at <= threshold,
            ~invited.exists(),
        )
    )
    return list((await session.execute(stmt)).scalars().all())


#: Порядок сценариев. Первый подходящий и выигрывает: win-back важнее апселла,
#: потому что про истёкшую подписку человек точно забыл, а тариф подлиннее —
#: приятная опция, а не необходимость.
def _audiences() -> tuple[tuple[str, object], ...]:
    return (
        (KIND_WINBACK, _winback),
        (KIND_TRIAL_NO_PAYMENT, _trial_without_payment),
        (KIND_UPSELL, _upsell),
        (KIND_REFERRAL, _referral_nudge),
    )


async def plan_sends(session: AsyncSession, now: datetime | None = None) -> list[tuple[User, Scenario]]:
    """Кому и что отправляем. Чистая выборка без отправки — удобно тестировать."""
    if not settings.lifecycle_enabled:
        return []

    now = now or utcnow()
    by_kind = {scenario.kind: scenario for scenario in scenarios()}
    planned: list[tuple[User, Scenario]] = []
    seen: set[int] = set()

    for kind, audience in _audiences():
        scenario = by_kind.get(kind)
        if scenario is None:
            continue
        for user in await audience(session, now):  # type: ignore[operator]
            if user.id in seen:
                continue
            if _too_soon(user, now, settings.lifecycle_min_gap_days):
                continue
            if await _sent_before(session, user.id, kind):
                continue
            seen.add(user.id)
            planned.append((user, scenario))
    return planned


async def run_lifecycle(
    bot: Bot,
    session: AsyncSession,
    now: datetime | None = None,
    planned: list[tuple[User, Scenario]] | None = None,
) -> int:
    """Отправить сообщения по сценариям. Возвращает число отправленных.

    Каждое сообщение помечается событием и полями ``last_lifecycle_*``:
    повторный запуск задачи (или перезапуск бота) не отправит одно и то же дважды.

    :param planned: готовый список из :func:`plan_sends` — если вызывающий код
        уже его посчитал (например, чтобы ограничить размер пачки).
    """
    planned = planned if planned is not None else await plan_sends(session, now)
    moment = now or utcnow()
    sent = 0
    for user, scenario in planned:
        try:
            await bot.send_message(user.tg_id, scenario.text, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001 - бот мог быть заблокирован человеком
            continue
        user.last_lifecycle_at = moment
        user.last_lifecycle_kind = scenario.kind
        await events.log_event(
            session,
            events.LIFECYCLE_SENT,
            user_id=user.id,
            payload={"kind": scenario.kind, "title": scenario.title},
        )
        sent += 1
    if sent:
        await session.flush()
    return sent


async def audience_counts(session: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """Сколько человек ждёт в каждой очереди — для админки и отчёта."""
    now = now or utcnow()
    result: dict[str, int] = {}
    for kind, audience in _audiences():
        users = await audience(session, now)  # type: ignore[operator]
        result[kind] = len(users)
    return result
