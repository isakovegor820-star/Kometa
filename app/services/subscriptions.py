"""Логика подписок: пробный доступ, покупка, продление, истечение.

Здесь сходятся бот, панель и БД. Правила:
  * срок подписки в БД считаем от ответа панели (панель — источник правды);
  * пробный доступ даётся один раз в жизни аккаунта;
  * продление — идемпотентно (нельзя продлить дважды за один платёж).
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Plan, Subscription, User
from app.panels.base import PanelClient, PanelError, PanelUser, UserSpec, panel_label
from app.services import events

settings = get_settings()
logger = logging.getLogger(__name__)


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
async def drain_bonus_balance(session: AsyncSession, user: User, *, reason: str) -> int:
    """Забрать накопленные бонусные дни и вернуть их количество.

    Вызывается при создании подписки: дни, заработанные на приглашениях,
    «докапываются» до первой же подписки — в том числе до пробной.
    """
    bonus = int(user.bonus_days_balance or 0)
    if bonus <= 0:
        return 0
    user.bonus_days_balance = 0
    await session.flush()
    await events.log_event(
        session,
        events.BONUS_DAYS_APPLIED,
        user_id=user.id,
        payload={"days": bonus, "reason": reason},
    )
    return bonus


async def all_user_panels(session: AsyncSession) -> list[PanelClient]:
    """Панели, на которых должен существовать клиент: основная + активные ноды.

    Выдаём один и тот же uuid и email на всех панелях. Тогда ссылка-подписка
    собирает конфиги всех стран сразу, а продление, блокировка и истечение
    применяются к каждой ноде.
    """
    from app.panels.registry import registry

    return await registry.all_panels(session)


def _panel_list(panel: PanelClient | Sequence[PanelClient]) -> list[PanelClient]:
    """Привести аргумент к списку: сервис принимает и одну панель, и все."""
    if isinstance(panel, (list, tuple, set, frozenset)):
        return list(panel)
    return [panel]


async def _ensure_client(panel: PanelClient, spec: UserSpec, canonical_uuid: str = "") -> PanelUser:
    """Клиент с нужным uuid на одной панели: создать или переиспользовать.

    Зачем развилка: БД бота и панели живут отдельно и расходятся — восстановили
    БД из бэкапа, переустановили бота, чистили клиентов руками. Панель на попытку
    создать дубль отвечает ``success: false`` («Duplicate email»), и человек с
    оплаченным доступом получал «Сервис временно недоступен», хотя доступ был.

    Если панель знает этот email с **другим** uuid — пересоздаём клиента: иначе
    ссылка-подписка не сможет собрать конфиг с этой панели (она ищет по uuid).

    :raises PanelError: панель недоступна или клиента нет и создать не удалось.
    """
    target = canonical_uuid or spec.uuid
    attempt = replace(spec, uuid=target) if target and target != spec.uuid else spec
    try:
        return await panel.create_user(attempt)
    except PanelError as exc:
        existing = await panel.find_user_by_email(spec.email)
        if existing is None:
            raise
        if target and existing.uuid != target:
            logger.warning(
                "Панель %s знает %s с другим uuid — пересоздаю подписку", panel_label(panel), spec.email
            )
            await panel.delete_user(existing.uuid)
            return await panel.create_user(attempt)
        logger.warning(
            "Панель %s уже знает клиента %s — переиспользую %s (%s)",
            panel_label(panel),
            spec.email,
            existing.uuid,
            exc,
        )
        return await panel.update_user(
            existing.uuid,
            extend_days=spec.days,
            traffic_gb=spec.traffic_gb,
            devices=spec.devices,
            enable=True,
        )


async def _create_on_panels(panels: Sequence[PanelClient], spec: UserSpec) -> tuple[PanelUser, list[str]]:
    """Создать клиента на всех панелях одним uuid.

    Первая панель (основная) обязана ответить: её отказ — это отказ выдачи.
    Недоступная нода выдачу не срывает: клиент появится на остальных, страна
    просто не попадёт в подписку до следующей попытки — это видно в логе и в
    админке (``/nodes``).

    :returns: (клиент основной панели, имена панелей, которые не ответили)
    """
    primary_user: PanelUser | None = None
    canonical = ""
    failed: list[str] = []
    for index, panel in enumerate(panels):
        try:
            user = await _ensure_client(panel, spec, canonical)
        except PanelError as exc:
            if index == 0:
                raise
            logger.error("Нода %s не приняла клиента %s: %s", panel_label(panel), spec.email, exc)
            failed.append(panel_label(panel))
            continue
        if not canonical:
            canonical, primary_user = user.uuid, user
    if primary_user is None:  # pragma: no cover - первая панель либо отдала клиента, либо бросила
        raise PanelError("ни одна панель не приняла клиента")
    return primary_user, failed


async def _update_on_panels(
    panels: Sequence[PanelClient],
    uuid: str,
    *,
    extend_days: int | None = None,
    traffic_gb: int | None = None,
    devices: int | None = None,
    enable: bool | None = None,
) -> PanelUser | None:
    """Применить изменение на всех панелях.

    Падение ноды не срывает операцию (оплата уже принята, доступ должен жить),
    но падение основной панели — срывает: это единственная точка, через которую
    человек получает конфиг по умолчанию.
    """
    result: PanelUser | None = None
    for index, panel in enumerate(panels):
        try:
            user = await panel.update_user(
                uuid,
                extend_days=extend_days,
                traffic_gb=traffic_gb,
                devices=devices,
                enable=enable,
            )
        except PanelError as exc:
            if index == 0:
                raise
            logger.error("Нода %s не обновила клиента %s: %s", panel_label(panel), uuid, exc)
            continue
        result = result or user
    return result


async def start_trial(session: AsyncSession, user: User, panel: PanelClient) -> tuple[Subscription, bool]:
    """Выдать пробный доступ. Возвращает (подписка, выдан_ли_сейчас)."""
    sub = await get_subscription(session, user.id)
    if sub is not None:
        # триал даётся один раз: повторно — только если подписки ещё не было
        return sub, False

    # Бонусные дни по умолчанию НЕ добавляются к триалу: иначе заработанные
    # на приглашениях дни уходят в бесплатный доступ, а не сокращают срок
    # окупаемости первой оплаты. Поведение переключается настройкой.
    bonus_days = (
        await drain_bonus_balance(session, user, reason="trial")
        if settings.trial_applies_bonus_days
        else 0
    )
    days = settings.trial_days + bonus_days
    panel_user, failed_panels = await _create_on_panels(
        _panel_list(panel),
        UserSpec(
            email=_panel_email(user),
            days=days,
            traffic_gb=settings.trial_gb,
            devices=settings.trial_devices,
            note="trial",
        ),
    )
    if failed_panels:
        logger.warning("Триал выдан без нод: %s", ", ".join(failed_panels))
    sub = Subscription(
        user_id=user.id,
        status="trial",
        expires_at=panel_user.expires_at or (datetime.now(timezone.utc) + timedelta(days=days)),
        devices_limit=panel_user.devices_limit or settings.trial_devices,
        traffic_limit_gb=settings.trial_gb,
        panel_user_uuid=panel_user.uuid,
        subscription_token=new_subscription_token(),
    )
    session.add(sub)
    await session.flush()
    await events.log_event(
        session,
        events.TRIAL_STARTED,
        user_id=user.id,
        payload={"days": days, "bonus_days": bonus_days},
    )
    return sub, True


async def activate_plan(
    session: AsyncSession,
    user: User,
    plan: Plan,
    panel: PanelClient | Sequence[PanelClient],
    *,
    extra_days: int = 0,
    paid_days: int | None = None,
    paid_devices: int | None = None,
    paid_traffic_gb: int | None = None,
) -> Subscription:
    """Оплаченная покупка/продление тарифа.

    Накопленные бонусные дни списываем **после** успешного ответа панели: если
    панель не ответила, выдача повторится (``orders.grant_ungranted_orders``), и
    баланс должен дойти до клиента, а не исчезнуть в неудачной попытке.

    ``paid_*`` — условия, которые клиент оплатил (снимок заказа). Их передаёт
    выдача: пока счёт не закрыт, тариф могли отредактировать, и читать живой
    прайс значит выдать не то, за что заплатили. ``None`` — снимка нет, берём
    тариф как раньше.
    """
    bonus_days = int(user.bonus_days_balance or 0)
    plan_days = plan.days if paid_days is None else int(paid_days)
    devices = plan.devices_limit if paid_devices is None else int(paid_devices)
    traffic_gb = plan.traffic_limit_gb if paid_traffic_gb is None else int(paid_traffic_gb)
    days = plan_days + max(0, extra_days) + bonus_days
    sub = await get_subscription(session, user.id)
    spec = UserSpec(
        email=_panel_email(user),
        days=days,
        traffic_gb=traffic_gb,
        devices=devices,
        note=plan.code,
    )

    panels = _panel_list(panel)
    if sub is None or not sub.panel_user_uuid:
        # Оплаченный доступ нельзя терять: если панель уже знает такого клиента
        # (БД бота восстановили из бэкапа), находим его и продлеваем.
        panel_user, _failed = await _create_on_panels(panels, spec)
        if sub is None:
            sub = Subscription(user_id=user.id, subscription_token=new_subscription_token())
            session.add(sub)
        sub.panel_user_uuid = panel_user.uuid
    else:
        try:
            panel_user = await _update_on_panels(
                panels,
                sub.panel_user_uuid,
                extend_days=days,
                traffic_gb=traffic_gb,
                devices=devices,
                enable=True,
            )
        except PanelError as exc:
            # Клиента могли удалить в панели руками или он потерялся при
            # переносе. Ищем по email (чтобы не создать дубль), иначе создаём.
            logger.warning(
                "Панель не продлила клиента %s: %s — ищу по email %s",
                sub.panel_user_uuid,
                exc,
                spec.email,
            )
            panel_user, _failed = await _create_on_panels(
                panels, replace(spec, uuid=sub.panel_user_uuid)
            )
            sub.panel_user_uuid = panel_user.uuid

    sub.status = "active"
    sub.plan_id = plan.id
    sub.devices_limit = devices
    sub.traffic_limit_gb = traffic_gb
    sub.expires_at = panel_user.expires_at or (datetime.now(timezone.utc) + timedelta(days=days))
    sub.notified_3d = False
    sub.notified_1d = False

    # Дни уже учтены в сроке выше — теперь их можно списать с баланса.
    if bonus_days > 0:
        user.bonus_days_balance = 0
        await events.log_event(
            session,
            events.BONUS_DAYS_APPLIED,
            user_id=user.id,
            payload={"days": bonus_days, "reason": plan.code},
        )
    await session.flush()
    return sub


async def sync_subscription_to_panels(
    session: AsyncSession,
    sub: Subscription,
    panels: Sequence[PanelClient],
) -> list[str]:
    """Досоздать клиента на панелях, где его ещё нет.

    Нужно, когда нода появилась позже выдачи (новая страна): ключа на ней нет,
    и в подписке у человека будет меньше локаций, чем он оплатил. Существующих
    клиентов **не трогаем** — иначе повторная синхронизация продлевала бы срок.

    :returns: имена панелей, которые не ответили
    """
    if not sub.panel_user_uuid:
        return []
    user = await session.get(User, sub.user_id)
    if user is None:
        return []

    email = _panel_email(user)
    spec = UserSpec(
        email=email,
        days=max(0, (sub.expires_at - datetime.now(timezone.utc)).days) or 30,
        traffic_gb=sub.traffic_limit_gb,
        devices=sub.devices_limit,
        uuid=sub.panel_user_uuid,
    )
    failed: list[str] = []
    for panel in panels:
        try:
            existing = await panel.find_user_by_email(email)
        except PanelError as exc:
            logger.error("Панель %s недоступна при синхронизации: %s", panel_label(panel), exc)
            failed.append(panel_label(panel))
            continue
        if existing is not None:
            continue
        try:
            await panel.create_user(replace(spec, uuid=sub.panel_user_uuid))
        except PanelError as exc:
            logger.error("Нода %s не приняла клиента %s: %s", panel_label(panel), email, exc)
            failed.append(panel_label(panel))
    return failed


async def sync_all_subscriptions(session: AsyncSession, panels: Sequence[PanelClient]) -> tuple[int, list[str]]:
    """Раздать всех активных клиентов по всем панелям.

    :returns: (сколько подписок проверено, имена панелей с ошибками)
    """
    subs = (
        await session.scalars(
            select(Subscription).where(Subscription.status.in_(["trial", "active"]))
        )
    ).all()
    failed: set[str] = set()
    for sub in subs:
        failed.update(await sync_subscription_to_panels(session, sub, panels))
    return len(subs), sorted(failed)


async def extend_days(
    session: AsyncSession,
    user: User,
    days: int,
    panel: PanelClient | Sequence[PanelClient],
    *,
    reason: str = "bonus",
) -> Subscription | None:
    """Начислить дни без оплаты (реферальный бонус, компенсация сбоя)."""
    if days <= 0:
        return None
    sub = await get_subscription(session, user.id)
    if sub is None or not sub.panel_user_uuid:
        return None
    panel_user = await _update_on_panels(
        _panel_list(panel), sub.panel_user_uuid, extend_days=days, enable=True
    )
    sub.expires_at = panel_user.expires_at or (sub.expires_at + timedelta(days=days))
    if sub.status == "expired":
        sub.status = "active"
    sub.notified_3d = False
    sub.notified_1d = False
    await session.flush()
    await events.log_event(session, events.SUBSCRIPTION_EXTENDED, user_id=user.id, payload={"days": days, "reason": reason})
    return sub


async def add_bonus_days(
    session: AsyncSession,
    user: User,
    days: int,
    panel: PanelClient | Sequence[PanelClient],
    *,
    reason: str = "bonus",
) -> tuple[Subscription | None, int]:
    """Наградить днями: продлить подписку или отложить их в баланс.

    :returns: (подписка, сколько дней ушло в накопительный баланс).
    """
    if days <= 0:
        return None, 0
    sub = await get_subscription(session, user.id)
    if sub is None or not sub.panel_user_uuid:
        user.bonus_days_balance = int(user.bonus_days_balance or 0) + days
        await session.flush()
        await events.log_event(
            session,
            events.REFERRAL_BONUS_ACCRUED,
            user_id=user.id,
            payload={"days": days, "reason": reason, "balance": user.bonus_days_balance},
        )
        return None, days
    return await extend_days(session, user, days, panel, reason=reason), 0


async def set_enabled(
    sub: Subscription,
    panel: PanelClient | Sequence[PanelClient],
    enabled: bool,
    *,
    reason: str = "",
) -> None:
    """Включить или выключить доступ, сохранив «род» подписки.

    Раньше включение всегда писало ``active``: бывший пробный или истёкший
    доступ превращался в платный, и статистика начинала врать. Теперь статус
    восстанавливается по факту: пробный остаётся пробным, истёкший — истёкшим.
    """
    if not sub.panel_user_uuid:
        return
    await _update_on_panels(_panel_list(panel), sub.panel_user_uuid, enable=enabled)
    sub.status = status_after_toggle(sub, enabled)
    if enabled:
        sub.notified_3d = False
        sub.notified_1d = False
    logger.info("Доступ подписки %s: %s (%s)", sub.id, "включён" if enabled else "выключен", sub.status)


def status_after_toggle(sub: Subscription, enabled: bool) -> str:
    """Какой статус подписки логичен после включения/выключения доступа."""
    if not enabled:
        return "blocked"
    if sub.expires_at <= datetime.now(timezone.utc):
        return "expired"
    if sub.status in {"trial", "active"}:
        return sub.status
    return "trial" if sub.plan_id is None else "active"


async def revoke_access(
    session: AsyncSession,
    sub: Subscription,
    panel: PanelClient | Sequence[PanelClient],
    *,
    reason: str = "",
) -> Subscription:
    """Отозвать доступ, не блокируя аккаунт клиента в боте.

    Нужно при возврате денег и при подозрении на шеринг: человек перестаёт
    подключаться, но продолжает получать сообщения бота и может оплатить снова.
    """
    if sub.panel_user_uuid:
        await _update_on_panels(_panel_list(panel), sub.panel_user_uuid, enable=False)
    sub.status = "blocked"
    await session.flush()
    await events.log_event(
        session,
        events.SUBSCRIPTION_REVOKED,
        user_id=sub.user_id,
        payload={"reason": reason or "admin"},
    )
    return sub


async def restore_access(
    session: AsyncSession,
    sub: Subscription,
    panel: PanelClient | Sequence[PanelClient],
    *,
    reason: str = "",
) -> Subscription:
    """Вернуть доступ после отзыва/блокировки — статус восстанавливается по факту."""
    await set_enabled(sub, panel, True, reason=reason)
    await session.flush()
    await events.log_event(
        session,
        events.SUBSCRIPTION_RESTORED,
        user_id=sub.user_id,
        payload={"reason": reason or "admin"},
    )
    return sub


async def adjust_days(
    session: AsyncSession,
    user: User,
    days: int,
    panel: PanelClient | Sequence[PanelClient],
    *,
    reason: str = "",
) -> tuple[Subscription | None, int, str]:
    """Изменить срок доступа руками: плюс — начислить, минус — списать.

    :returns: (подписка, сколько дней ушло в накопительный баланс, текст ошибки).

    Списание — не «минус в базе», а реальное сокращение срока и на панели:
    иначе ссылка-подписка продолжает работать, и модератор думает, что доступ
    закрыт, хотя клиент подключается.
    """
    if days == 0:
        return None, 0, "Укажи число дней (плюс — начислить, минус — списать)"
    if days > 0:
        sub, to_balance = await add_bonus_days(session, user, days, panel, reason=reason or "admin_grant")
        return sub, to_balance, ""

    sub = await get_subscription(session, user.id)
    if sub is None:
        return None, 0, "У пользователя нет подписки — сначала выдай доступ"

    now = datetime.now(timezone.utc)
    target = sub.expires_at + timedelta(days=days)
    panels = _panel_list(panel)
    if sub.panel_user_uuid:
        try:
            panel_user = await _update_on_panels(panels, sub.panel_user_uuid, extend_days=days)
        except PanelError as exc:
            return None, 0, f"Панель не приняла списание: {exc}"
        if panel_user is not None and panel_user.expires_at is not None:
            target = panel_user.expires_at

    sub.expires_at = max(target, now)
    sub.notified_3d = False
    sub.notified_1d = False
    if sub.expires_at <= now and sub.panel_user_uuid:
        # Срок вышел — сразу закрываем доступ на всех панелях.
        try:
            await _update_on_panels(panels, sub.panel_user_uuid, enable=False)
        except PanelError as exc:
            logger.warning("Не смог выключить клиента %s после списания: %s", sub.panel_user_uuid, exc)
        sub.status = "expired"
    await session.flush()
    await events.log_event(
        session,
        events.SUBSCRIPTION_EXTENDED,
        user_id=user.id,
        payload={"days": days, "reason": reason or "admin_write_off"},
    )
    return sub, 0, ""


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
                await _update_on_panels(_panel_list(panel), sub.panel_user_uuid, enable=False)
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
