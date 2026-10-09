"""Данные экрана «Мой профиль»: только факты, ничего не додумываем.

Зачем отдельный сервис. Профиль — это визитка клиента: статус доступа, срок,
устройства, трафик, локации и приглашения. У каждой цифры на экране есть
источник: подписка и заказы — в БД, лимиты — в тарифе или настройках триала,
прогресс — из срока подписки, приглашения — из :mod:`app.services.referral`,
состояние локаций — из последней пробы «глазами клиента»
(:func:`app.services.probe.probe_verdict`).

Где данных нет — экран говорит «нет данных». Это не педантизм: 08.10.2026
владелец поймал систему на том, что она бодро рапортовала о состоянии,
которого не измеряла (см. ``docs/ПРОФИЛЬ-И-РЕФЕРАЛКА.md``). Поэтому «без
ограничений» пишется, только если в тарифе стоит ``traffic_limit_gb == 0``,
а «локации доступны» — только если проба это подтвердила.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.config import get_settings
from app.db.models import Node, Order, Plan, Subscription, User
from app.panels.base import PanelClient, PanelError
from app.services import referral, subscriptions
from app.services.probe import PROBE_OK, PROBE_PORT_FAILED, probe_verdict

settings = get_settings()
logger = logging.getLogger(__name__)

#: Статусы карточки. Отдельно от ``Subscription.status``: у экрана есть
#: состояние «подписки нет вовсе», которого в БД не бывает.
STATUS_ACTIVE = "active"
STATUS_TRIAL = "trial"
STATUS_EXPIRED = "expired"
STATUS_BLOCKED = "blocked"
STATUS_NONE = "none"

#: Дальше этого срока подписку показываем как «бессрочную»: в базе бессрочные
#: хранятся далёкой датой (иначе фоновые задачи их погасят), а клиенту
#: «осталось 26 000 дн.» говорить нельзя. Та же граница, что на экране
#: «Моя подписка» (``app/bot/handlers/subscription.py``).
FOREVER_AFTER = timedelta(days=365 * 10)

#: Ширина полосы остатка срока. Полоса — только вместе с числом рядом.
BAR_WIDTH = 10
BAR_FILLED = "█"
BAR_EMPTY = "░"

GIB = 1024**3


@dataclass(slots=True)
class ProfileCard:
    """Всё, что видно на экране профиля. Значения — из БД, настроек и панели."""

    name: str
    username: str
    member_since: str
    people_count: int
    status: str
    expires_text: str
    days_left: int
    forever: bool
    bar: str
    devices: int
    traffic_limit_gb: int
    #: Расход трафика в гигабайтах. ``None`` — панель не ответила: цифру не
    #: выдумываем, показываем только лимит.
    traffic_used_gb: float | None
    plan_title: str
    paid_rub: int
    #: Строка-статус локаций: одна-две строки вместо списка. Пусто — показывать
    #: нечего (нет активного доступа или нет нод с названиями).
    locations_line: str
    invited: int
    paid_friends: int
    earned_days: int
    balance_days: int
    rewards_left: int

    @property
    def has_access(self) -> bool:
        """Есть ли доступ прямо сейчас (пробный тоже доступ)."""
        return self.status in {STATUS_ACTIVE, STATUS_TRIAL}


def progress_bar(days_left: int, days_total: int, width: int = BAR_WIDTH) -> str:
    """Полоса остатка срока: заполнено — сколько дней доступ ещё живёт.

    :param days_left: сколько дней осталось.
    :param days_total: длина оплаченного (или пробного) периода.
    """
    total = max(1, int(days_total))
    left = max(0, int(days_left))
    filled = round(min(1.0, left / total) * width)
    return BAR_FILLED * filled + BAR_EMPTY * (width - filled)


def is_forever(expires_at: datetime | None) -> bool:
    """Подписка «бессрочная»? Бессрочные храним далёкой датой."""
    if expires_at is None:
        return False
    moment = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
    return moment - datetime.now(timezone.utc) > FOREVER_AFTER


def locations_line(nodes: Sequence[object]) -> str:
    """Строка-статус локаций: только то, что измерено пробой.

    Ноды без замера не превращаются в «доступны»: пока проба не прошла,
    честный ответ — «статус появится после первой проверки».
    """
    ok: list[str] = []
    failed: list[str] = []
    unknown: list[str] = []
    for node in nodes:
        title = str(getattr(node, "title", "") or "").strip()
        if not title:
            continue
        verdict = probe_verdict(node)
        if verdict == PROBE_OK:
            ok.append(title)
        elif verdict == PROBE_PORT_FAILED:
            failed.append(title)
        else:  # PROBE_UNKNOWN / PROBE_UNAVAILABLE / PROBE_NOT_CONFIGURED
            unknown.append(title)
    return texts.profile_locations_line(
        ok=ok, failed=failed, unknown=unknown, measured=bool(ok or failed)
    )


async def _used_traffic_gb(panel: PanelClient, uuid: str) -> float | None:
    """Расход трафика по данным панели. ``None`` — панель не ответила.

    Трафик клиента живёт только в панели. Недоступная панель — это «нет
    данных», а не «0 ГБ»: ноль здесь был бы выдумкой.
    """
    try:
        panel_user = await panel.get_user(uuid)
    except PanelError as exc:
        logger.warning("Профиль: панель не отдала трафик клиента %s: %s", uuid, exc)
        return None
    except Exception as exc:  # noqa: BLE001 — чужая панель отвечает чем угодно
        logger.warning("Профиль: панель ответила ошибкой на запрос трафика: %s", exc)
        return None
    if panel_user is None:
        return None
    return max(0.0, float(panel_user.used_bytes or 0) / GIB)


async def build_card(
    session: AsyncSession,
    user: User,
    *,
    panel: PanelClient | None = None,
) -> ProfileCard:
    """Собрать карточку профиля. Ничего не отправляет и не меняет в БД.

    :param panel: панель для цифры расхода трафика. Пусто — трафик показываем
        без расхода (лимит и всё). Панель спрашиваем только когда лимит вообще
        есть: у безлимитных тарифов расхода не существует.
    """
    sub: Subscription | None = await subscriptions.get_subscription(session, user.id)
    plan: Plan | None = None
    if sub is not None and sub.plan_id:
        plan = await session.get(Plan, sub.plan_id)

    status = STATUS_NONE
    if sub is not None:
        if not sub.is_active:
            status = STATUS_BLOCKED if sub.status == "blocked" else STATUS_EXPIRED
        else:
            status = STATUS_TRIAL if sub.status == "trial" else STATUS_ACTIVE

    forever = sub is not None and is_forever(sub.expires_at)
    days_left = sub.days_left if sub is not None else 0
    expires_text = sub.expires_at.strftime("%d.%m.%Y") if sub is not None and sub.expires_at else ""

    # Без подписки показываем условия пробного доступа: человек решает, начинать
    # ли, и должен видеть реальные лимиты, а не пустое место.
    devices = sub.devices_limit if sub is not None else settings.trial_devices
    traffic_limit_gb = sub.traffic_limit_gb if sub is not None else settings.trial_gb

    traffic_used_gb: float | None = None
    if traffic_limit_gb > 0 and sub is not None and sub.panel_user_uuid and panel is not None:
        traffic_used_gb = await _used_traffic_gb(panel, sub.panel_user_uuid)

    bar = ""
    if status in {STATUS_ACTIVE, STATUS_TRIAL} and not forever:
        total = plan.days if plan is not None and plan.days > 0 else _period_days(sub)
        bar = progress_bar(days_left, total)

    locations = ""
    if status in {STATUS_ACTIVE, STATUS_TRIAL}:
        nodes = (
            await session.scalars(
                select(Node).where(Node.is_active.is_(True)).order_by(Node.priority, Node.id)
            )
        ).all()
        locations = locations_line(list(nodes))

    stats = await referral.overview(session, user)

    return ProfileCard(
        name=user.display_name,
        username=(user.username or "").strip().lstrip("@"),
        member_since=user.created_at.strftime("%d.%m.%Y") if user.created_at else "",
        people_count=int(await session.scalar(select(func.count(User.id))) or 0),
        status=status,
        expires_text=expires_text,
        days_left=days_left,
        forever=forever,
        bar=bar,
        devices=devices,
        traffic_limit_gb=traffic_limit_gb,
        traffic_used_gb=traffic_used_gb,
        plan_title=plan.title if plan is not None else "",
        paid_rub=await _last_paid_rub(session, user),
        locations_line=locations,
        invited=stats["invited"],
        paid_friends=stats["paid"],
        earned_days=stats["earned_days"],
        balance_days=stats["balance"],
        rewards_left=stats["rewards_left"],
    )


def _period_days(sub: Subscription) -> int:
    """Длина текущего периода подписки в днях — знаменатель для полосы."""
    if sub.starts_at is None or sub.expires_at is None:
        return max(1, int(sub.days_left) or 1)
    return max(1, round((sub.expires_at - sub.starts_at).total_seconds() / 86400))


async def _last_paid_rub(session: AsyncSession, user: User) -> int:
    """Сколько человек заплатил последним оплаченным заказом (0 — не платил)."""
    order = await session.scalar(
        select(Order)
        .where(Order.user_id == user.id, Order.status == "paid")
        .order_by(Order.id.desc())
        .limit(1)
    )
    return int(order.amount_rub or 0) if order is not None else 0
