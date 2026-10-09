"""Ретенция персональных данных: сроки, чистка и удаление по запросу.

Что обещано в Политике конфиденциальности (docs/legal/ПОЛИТИКА-КОНФИДЕНЦИАЛЬНОСТИ.md)
и что делает этот модуль — одно и то же место):

  * **30 дней** — технические журналы: события (``events``), алерты (``alerts``)
    и рассылки (``broadcasts``) старше срока удаляются задачей. Пункт 6.3.
  * **12 месяцев без активности** — данные аккаунта анонимизируются: имя,
    @username, идентификатор Telegram и заметки модераторов исчезают, а строка
    остаётся ради заказов и расчётов. Пункт 6.1.
  * **4 года** — сведения о заказах и платежах не удаляются: налоговый учёт
    (пункт 6.2). Поэтому анонимизация не трогает ``orders``/``payments``.
  * **по запросу** — кнопка в карточке клиента удаляет персональные данные
    сразу, с записью в аудит и без ошибки при повторном нажатии. Пункт 6.4.

Почему константы здесь, а не в текстах: сроки — решение владельца, и менять их
нужно в одном месте. Тест ``tests/test_retention.py::test_policy_matches_retention``
сверяет числа в Политике с этими константами — обещание не может разойтись с кодом.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ANONYMIZED_DISPLAY_NAME,
    Alert,
    Broadcast,
    Event,
    Order,
    Subscription,
    User,
    UserNote,
)
from app.services import audit, events

logger = logging.getLogger(__name__)

#: Технические журналы: пункт 6.3 Политики — «не более 30 дней».
EVENTS_RETENTION_DAYS = 30

#: Аккаунт без активности: пункт 6.1 — «12 месяцев после последней активности».
INACTIVITY_MONTHS = 12

#: Заказы и платежи: пункт 6.2 — «4 года» (налоговый учёт). Это срок, в течение
#: которого финансовые записи НЕ удаляются; сама чистка по возрасту их не трогает.
FINANCIAL_YEARS = 4

#: Как подписываем анонимизированного клиента в интерфейсе (одно место — модель).
ANONYMIZED_NAME = ANONYMIZED_DISPLAY_NAME

#: С какого числа начинаются синтетические Telegram-ID. Настоящие id
#: положительные, поэтому отрицательное значение уникально и не столкнётся с
#: реальным человеком, даже если тот вернётся в бота как новый пользователь.
SYNTHETIC_TG_BASE = 10**12

#: Сколько пользователей анонимизируем за один прогон задачи.
ANONYMIZE_BATCH = 200


@dataclass(slots=True)
class PurgeResult:
    """Что удалила задача ретенции."""

    events: int = 0
    alerts: int = 0
    broadcasts: int = 0

    @property
    def total(self) -> int:
        return self.events + self.alerts + self.broadcasts

    def as_text(self) -> str:
        return (
            f"события: {self.events}, алерты: {self.alerts}, рассылки: {self.broadcasts}"
        )


@dataclass(slots=True)
class ActivityInfo:
    """Данные для решения «пора анонимизировать»."""

    last_activity_at: datetime
    expires_at: datetime | None = None
    orders: int = 0

    def is_inactive(self, *, now: datetime, months: int = INACTIVITY_MONTHS) -> bool:
        return self.last_activity_at <= _months_ago(now, months)


def _months_ago(moment: datetime, months: int) -> datetime:
    """Дата «N месяцев назад» без внешних зависимостей.

    Календарный месяц считаем как 30,44 суток: точность до дня здесь не важна,
    а понятная арифметика важнее — по этой границе удаляются данные.
    """
    return moment - timedelta(days=round(months * 30.44))


def synthetic_tg_id(user_id: int) -> int:
    """Уникальный отрицательный tg_id для анонимизированного пользователя."""
    return -(SYNTHETIC_TG_BASE + int(user_id))


# --------------------------------------------------------------------- чистка
async def purge_old_records(session: AsyncSession, *, now: datetime | None = None) -> PurgeResult:
    """Удалить технические журналы старше ``EVENTS_RETENTION_DAYS``.

    Чистим все три таблицы одним правилом по ``created_at``. Открытые алерты не
    исключение: если проблема жива, проверка нод поднимет алерт заново — а
    «вечный» алерт 2024 года в панели только мешает (и это персональные данные,
    которые мы обещали не хранить дольше 30 дней).
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=EVENTS_RETENTION_DAYS)
    result = PurgeResult()
    for model, attribute in ((Event, "events"), (Alert, "alerts"), (Broadcast, "broadcasts")):
        deleted = await session.execute(delete(model).where(model.created_at < cutoff))
        setattr(result, attribute, int(deleted.rowcount or 0))
    await session.flush()
    if result.total:
        logger.info("Ретенция: удалено (%s) — старше %s", result.as_text(), cutoff.date())
    return result


# ---------------------------------------------------------------- активность
async def activity_info(session: AsyncSession, user: User) -> ActivityInfo:
    """Когда пользователь последний раз «жил» в сервисе и сколько у него заказов.

    Активность — это любое касание сервиса: запуск бота, заказ, оплата, заметка
    модератора, действующая подписка, отметка о подписке на канал. Берём
    **максимум** из дат: если человек молчал, но у него действует оплаченный
    доступ, он не «неактивен».
    """
    moments: list[datetime] = [user.created_at] if user.created_at else []

    for value in (
        await session.scalar(select(func.max(Event.created_at)).where(Event.user_id == user.id)),
        await session.scalar(select(func.max(Order.created_at)).where(Order.user_id == user.id)),
        await session.scalar(select(func.max(UserNote.created_at)).where(UserNote.user_id == user.id)),
    ):
        if value is not None:
            moments.append(value)

    if user.channel_verified_at is not None:
        moments.append(user.channel_verified_at)
    if user.last_lifecycle_at is not None:
        moments.append(user.last_lifecycle_at)

    sub = await session.scalar(select(Subscription).where(Subscription.user_id == user.id))
    if sub is not None and sub.expires_at is not None:
        moments.append(sub.expires_at)

    if not moments:
        moments.append(datetime.now(timezone.utc))

    orders = int(
        await session.scalar(select(func.count(Order.id)).where(Order.user_id == user.id)) or 0
    )
    return ActivityInfo(
        last_activity_at=max(moments),
        expires_at=sub.expires_at if sub is not None else None,
        orders=orders,
    )


# ------------------------------------------------------------------ удаление
@dataclass(slots=True)
class EraseResult:
    """Итог удаления персональных данных по запросу."""

    erased: bool = False
    already: bool = False
    orders: int = 0
    events: int = 0
    notes: int = 0
    fields: list[str] = field(default_factory=list)


async def erase_user(
    session: AsyncSession,
    user: User,
    *,
    reason: str = "",
    system: bool = False,
) -> EraseResult:
    """Удалить персональные данные пользователя (анонимизация с сохранением денег).

    Что делаем:

    * обнуляем имя, @username, метки, промокод, персональную ссылку и отметку о
      подписке на канал;
    * заменяем ``tg_id`` на синтетический отрицательный — связать строку с
      человеком в Telegram больше нельзя;
    * удаляем заметки модераторов и события клиента: это свободный текст, в
      котором встречаются имена и детали обращений;
    * **не трогаем** заказы, платежи и партнёрские начисления: их хранение
      обязательно 4 года (пункт 6.2 Политики), суммы и статусы остаются в отчётах.

    Повторное нажатие безопасно: если ``anonymized_at`` уже стоит, функция
    ничего не меняет и возвращает ``already=True`` — новую запись в аудит
    писать не нужно, данные уже удалены.
    """
    if user.anonymized_at is not None:
        return EraseResult(already=True)

    info = await activity_info(session, user)

    deleted_notes = await session.execute(delete(UserNote).where(UserNote.user_id == user.id))
    deleted_events = await session.execute(delete(Event).where(Event.user_id == user.id))

    fields: list[str] = []
    if user.first_name:
        fields.append("first_name")
    if user.username:
        fields.append("username")
    if user.tags:
        fields.append("tags")
    if user.promo_code:
        fields.append("promo_code")
    if user.source_detail:
        fields.append("source_detail")

    user.first_name = None
    user.username = None
    user.tags = ""
    user.promo_code = None
    user.source_detail = ""
    user.personal_link_id = None
    user.channel_verified_at = None
    user.tg_id = synthetic_tg_id(user.id)
    user.anonymized_at = datetime.now(timezone.utc)

    await session.flush()
    await events.log_event(
        session,
        events.USER_ANONYMIZED,
        user_id=user.id,
        payload={
            "reason": reason or ("retention" if system else "request"),
            "orders_kept": info.orders,
            "events_deleted": int(deleted_events.rowcount or 0),
            "notes_deleted": int(deleted_notes.rowcount or 0),
            "fields": fields,
            "automatic": system,
        },
    )
    logger.info(
        "Персональные данные удалены (пользователь #%s): записей журнала %s, заметок %s, "
        "заказы сохранены (%s)",
        user.id,
        deleted_events.rowcount or 0,
        deleted_notes.rowcount or 0,
        info.orders,
    )
    return EraseResult(
        erased=True,
        orders=info.orders,
        events=int(deleted_events.rowcount or 0),
        notes=int(deleted_notes.rowcount or 0),
        fields=fields,
    )


async def anonymize_inactive_users(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = ANONYMIZE_BATCH,
) -> list[int]:
    """Анонимизировать пользователей без активности ``INACTIVITY_MONTHS``.

    Не трогаем:

    * тех, у кого персональные данные уже удалены (``anonymized_at`` стоит) —
      иначе задача каждый день писала бы новую запись в аудит;
    * тех, у кого подписка ещё действует: оплаченный доступ — это не «молчание».

    :returns: id анонимизированных пользователей.
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = _months_ago(moment, INACTIVITY_MONTHS)

    candidates = list(
        (
            await session.scalars(
                select(User).where(User.anonymized_at.is_(None)).order_by(User.id).limit(limit)
            )
        ).all()
    )

    erased: list[int] = []
    for user in candidates:
        info = await activity_info(session, user)
        if not info.is_inactive(now=moment):
            continue
        if info.expires_at is not None and info.expires_at > moment:
            continue
        # Границу держим в одном месте: cutoff нужен только для сравнения,
        # activity_info отдаёт точную дату последней активности.
        if info.last_activity_at > cutoff:
            continue
        await erase_user(session, user, reason="retention", system=True)
        erased.append(user.id)
    if erased:
        logger.info("Анонимизировано без активности: %s", erased)
    return erased


async def retention_report(session: AsyncSession, *, now: datetime | None = None) -> dict:
    """Короткая сводка для алерта/уведомления: что уже накоплено и когда чистим."""
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(days=EVENTS_RETENTION_DAYS)
    return {
        "events_old": int(
            await session.scalar(select(func.count(Event.id)).where(Event.created_at < cutoff)) or 0
        ),
        "alerts_old": int(
            await session.scalar(select(func.count(Alert.id)).where(Alert.created_at < cutoff)) or 0
        ),
        "broadcasts_old": int(
            await session.scalar(select(func.count(Broadcast.id)).where(Broadcast.created_at < cutoff)) or 0
        ),
        "anonymized": int(
            await session.scalar(select(func.count(User.id)).where(User.anonymized_at.is_not(None))) or 0
        ),
    }
