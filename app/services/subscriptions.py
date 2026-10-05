"""Логика подписок: пробный доступ, покупка, продление, истечение.

Здесь сходятся бот, панель и БД. Правила:
  * срок подписки в БД считаем от ответа панели (панель — источник правды);
  * пробный доступ даётся один раз в жизни аккаунта;
  * продление — идемпотентно (нельзя продлить дважды за один платёж).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Plan, Subscription, User
from app.panels.base import PanelClient, PanelError, UserSpec
from app.services import events

settings = get_settings()


# ---------------------------------------------------------------- утилиты
def new_subscription_token() -> str:
    return secrets.token_urlsafe(24)


def new_referral_code() -> str:
    return secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]


def subscription_link(token: str) -> str:
    """Публичная ссылка-подписка (её пользователь вставляет в клиент)."""
    return f"{settings.subscription_base}/{token}"


def _panel_email(user: User) -> str:
    """Логин пользователя в панели. Стабилен и не содержит персональных данных."""
    return f"u{user.tg_id}"


# ---------------------------------------------------------------- пользователь
async def get_user_by_tg(session: AsyncSession, tg_id: int) -> User | None:
    return await session.scalar(select(User).where(User.tg_id == tg_id))


async def get_or_create_user(
    session: AsyncSession,
    *,
    tg_id: int,
    username: str | None = None,
    first_name: str | None = None,
) -> tuple[User, bool]:
    user = await get_user_by_tg(session, tg_id)
    if user is not None:
        # держим актуальные username/имя, чтобы поддержка могла найти человека
        changed = False
        if username and user.username != username:
            user.username = username
            changed = True
        if first_name and user.first_name != first_name:
            user.first_name = first_name
            changed = True
        if changed:
            await session.flush()
        return user, False

    user = User(
        tg_id=tg_id,
        username=username,
        first_name=first_name,
        referral_code=await _unique_referral_code(session),
    )
    session.add(user)
    await session.flush()
    return user, True


async def _unique_referral_code(session: AsyncSession, attempts: int = 8) -> str:
    for _ in range(attempts):
        code = new_referral_code()
        exists = await session.scalar(select(User.id).where(User.referral_code == code))
        if exists is None:
            return code
    return secrets.token_hex(8)


async def get_subscription(session: AsyncSession, user_id: int) -> Subscription | None:
    return await session.scalar(select(Subscription).where(Subscription.user_id == user_id))


async def get_subscription_by_token(session: AsyncSession, token: str) -> Subscription | None:
    return await session.scalar(select(Subscription).where(Subscription.subscription_token == token))


# ---------------------------------------------------------------- доступ
async def start_trial(session: AsyncSession, user: User, panel: PanelClient) -> tuple[Subscription, bool]:
    """Выдать пробный доступ. Возвращает (подписка, выдан_ли_сейчас)."""
    sub = await get_subscription(session, user.id)
    if sub is not None:
        # триал даётся один раз: повторно — только если подписки ещё не было
        return sub, False

    panel_user = await panel.create_user(
        UserSpec(
            email=_panel_email(user),
            days=settings.trial_days,
            traffic_gb=settings.trial_gb,
            devices=settings.trial_devices,
            note="trial",
        )
    )
    sub = Subscription(
        user_id=user.id,
        status="trial",
        expires_at=panel_user.expires_at or (datetime.now(timezone.utc) + timedelta(days=settings.trial_days)),
        devices_limit=panel_user.devices_limit or settings.trial_devices,
        traffic_limit_gb=settings.trial_gb,
        panel_user_uuid=panel_user.uuid,
        subscription_token=new_subscription_token(),
    )
    session.add(sub)
    await session.flush()
    await events.log_event(session, events.TRIAL_STARTED, user_id=user.id, payload={"days": settings.trial_days})
    return sub, True


async def activate_plan(
    session: AsyncSession,
    user: User,
    plan: Plan,
    panel: PanelClient,
    *,
    extra_days: int = 0,
) -> Subscription:
    """Оплаченная покупка/продление тарифа."""
    days = plan.days + max(0, extra_days)
    sub = await get_subscription(session, user.id)

    if sub is None or not sub.panel_user_uuid:
        panel_user = await panel.create_user(
            UserSpec(
                email=_panel_email(user),
                days=days,
                traffic_gb=plan.traffic_limit_gb,
                devices=plan.devices_limit,
                note=plan.code,
            )
        )
        if sub is None:
            sub = Subscription(user_id=user.id, subscription_token=new_subscription_token())
            session.add(sub)
        sub.panel_user_uuid = panel_user.uuid
    else:
        panel_user = await panel.update_user(
            sub.panel_user_uuid,
            extend_days=days,
            traffic_gb=plan.traffic_limit_gb,
            devices=plan.devices_limit,
            enable=True,
        )

    sub.status = "active"
    sub.plan_id = plan.id
    sub.devices_limit = plan.devices_limit
    sub.traffic_limit_gb = plan.traffic_limit_gb
    sub.expires_at = panel_user.expires_at or (datetime.now(timezone.utc) + timedelta(days=days))
    sub.notified_3d = False
    sub.notified_1d = False
    await session.flush()
    return sub


async def extend_days(
    session: AsyncSession,
    user: User,
    days: int,
    panel: PanelClient,
    *,
    reason: str = "bonus",
) -> Subscription | None:
    """Начислить дни без оплаты (реферальный бонус, компенсация сбоя)."""
    if days <= 0:
        return None
    sub = await get_subscription(session, user.id)
    if sub is None or not sub.panel_user_uuid:
        return None
    panel_user = await panel.update_user(sub.panel_user_uuid, extend_days=days, enable=True)
    sub.expires_at = panel_user.expires_at or (sub.expires_at + timedelta(days=days))
    if sub.status == "expired":
        sub.status = "active"
    sub.notified_3d = False
    sub.notified_1d = False
    await session.flush()
    await events.log_event(session, events.SUBSCRIPTION_EXTENDED, user_id=user.id, payload={"days": days, "reason": reason})
    return sub


async def set_enabled(sub: Subscription, panel: PanelClient, enabled: bool) -> None:
    if not sub.panel_user_uuid:
        return
    await panel.update_user(sub.panel_user_uuid, enable=enabled)
    sub.status = "active" if enabled else "blocked"


# ---------------------------------------------------------------- фоновые задачи
async def disable_expired(session: AsyncSession, panel: PanelClient) -> list[Subscription]:
    """Отключить доступ у истёкших подписок. Возвращает список изменённых."""
    now = datetime.now(timezone.utc)
    subs = (
        await session.scalars(
            select(Subscription).where(
                Subscription.status.in_(["trial", "active"]),
                Subscription.expires_at <= now,
            )
        )
    ).all()
    changed: list[Subscription] = []
    for sub in subs:
        sub.status = "expired"
        if sub.panel_user_uuid:
            try:
                await panel.update_user(sub.panel_user_uuid, enable=False)
            except PanelError:
                # панель недоступна — не теряем факт истечения, повторим позже
                pass
        await events.log_event(session, events.SUBSCRIPTION_EXPIRED, user_id=sub.user_id)
        changed.append(sub)
    if changed:
        await session.flush()
    return changed


async def due_for_reminder(session: AsyncSession, days_before: int) -> list[Subscription]:
    """Подписки, которым пора напомнить о скором окончании."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days_before)
    flag_column = Subscription.notified_3d if days_before == 3 else Subscription.notified_1d
    stmt = select(Subscription).where(
        Subscription.status.in_(["trial", "active"]),
        Subscription.expires_at > now,
        Subscription.expires_at <= horizon,
        flag_column.is_(False),
    )
    subs = (await session.scalars(stmt)).all()
    for sub in subs:
        if days_before == 3:
            sub.notified_3d = True
        else:
            sub.notified_1d = True
    if subs:
        await session.flush()
    return list(subs)
