"""Компенсация простоя: продлить подписки за дни, когда связь не работала.

Зачем: когда у оператора ограничения, клиент платит за дни, в которые сервис
не работал. Честная компенсация снимает возвраты и удерживает клиента — но она
деньги, поэтому считается по факту, а не «на глаз», и начисляется один раз.

Порядок работы:

1. Админ открывает период: ``start(session, note="...")`` — когда заметил, что
   связь у абонентов пропала.
2. Когда связь вернулась: ``finish(session)`` — сервис считает **полные сутки**
   простоя, ограничивает их потолком и продлевает все активные подписки.
3. Если считать нечего (меньше суток) — период закрывается без начислений.

Повторный ``finish`` ничего не начислит: открытого периода уже нет, а у
закрытого стоит ``granted_at``. Ручное начисление без периода — ``grant``.

Кого продлеваем: подписки в статусе ``trial``/``active``. Истёкшие не трогаем
сознательно: иначе компенсация превращается в бесплатный доступ тем, кто и так
не платит.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Downtime, Subscription, User
from app.panels.base import PanelClient
from app.services import events, subscriptions

#: Потолок компенсации за один период: защита от опечатки в датах и от
#: «простоя», который забыли закрыть на месяц.
MAX_GRANT_DAYS = 30

#: Статусы, которые продлеваем: пробный и оплаченный доступ.
GRANT_STATUSES = ("trial", "active")

SECONDS_PER_DAY = 86_400


@dataclass(slots=True)
class GrantResult:
    """Итог начисления: сколько дней, скольким клиентам и кому именно.

    ``tg_ids`` нужны боту: после начисления клиентам уходит сообщение, и
    повторно искать их запросом не нужно.
    """

    ok: bool
    days: int = 0
    users: int = 0
    reason: str = ""
    tg_ids: list[int] = field(default_factory=list)

    def as_text(self) -> str:
        if not self.ok:
            return self.reason or "начислять нечего"
        if self.days <= 0:
            return "период закрыт без начисления: прошло меньше суток"
        return f"начислено {self.days} дн. клиентам: {self.users}"


async def current(session: AsyncSession) -> Downtime | None:
    """Открытый период простоя (он в системе может быть только один)."""
    return await session.scalar(
        select(Downtime).where(Downtime.ended_at.is_(None)).order_by(Downtime.id.desc())
    )


async def recent(session: AsyncSession, limit: int = 5) -> list[Downtime]:
    """Последние периоды — от новых к старым: видно, что и когда начисляли."""
    return list(
        (
            await session.scalars(select(Downtime).order_by(Downtime.id.desc()).limit(limit))
        ).all()
    )


async def start(
    session: AsyncSession,
    *,
    note: str = "",
    actor: str = "",
) -> tuple[Downtime, bool]:
    """Открыть период простоя.

    :returns: (период, создан ли новый). Если период уже открыт — возвращаем
        его же: повторная команда не должна плодить записи.
    """
    opened = await current(session)
    if opened is not None:
        return opened, False

    period = Downtime(
        started_at=datetime.now(timezone.utc),
        note=(note or "").strip()[:160],
        created_by=(actor or "")[:64],
    )
    session.add(period)
    await session.flush()
    await events.log_event(
        session,
        events.DOWNTIME_STARTED,
        payload={"id": period.id, "note": period.note, "actor": actor},
    )
    return period, True


async def finish(
    session: AsyncSession,
    *,
    actor: str = "",
    days: int | None = None,
    panels: list[PanelClient] | None = None,
) -> GrantResult:
    """Закрыть открытый период и начислить компенсацию.

    :param days: сколько дней начислить (по умолчанию — полные сутки простоя).
    :param panels: панели для продления; пусто — берём все активные.
    """
    period = await current(session)
    if period is None:
        return GrantResult(ok=False, reason="открытого периода простоя нет")

    now = datetime.now(timezone.utc)
    period.ended_at = now
    period.days = _days_for(period.started_at, now, days)
    period.granted_at = now
    period.granted_by = (actor or "")[:64]
    await session.flush()

    result = await _grant(session, period.days, panels=panels, source=f"period:{period.id}")
    await events.log_event(
        session,
        events.DOWNTIME_FINISHED,
        payload={
            "id": period.id,
            "days": period.days,
            "users": result.users,
            "actor": actor,
        },
    )
    if period.days <= 0:
        return GrantResult(ok=True, days=0, users=0)
    return result


async def grant(
    session: AsyncSession,
    days: int,
    *,
    note: str = "",
    actor: str = "",
    panels: list[PanelClient] | None = None,
) -> GrantResult:
    """Начислить дни вручную, без периода (разовая компенсация).

    Запись в историю создаётся сразу закрытой: видно, кто, когда и сколько
    начислил, — это деньги, и след должен остаться.
    """
    days = max(0, min(int(days), MAX_GRANT_DAYS))
    if days <= 0:
        return GrantResult(ok=False, reason="нужно указать положительное число дней")

    now = datetime.now(timezone.utc)
    period = Downtime(
        started_at=now,
        ended_at=now,
        days=days,
        granted_at=now,
        granted_by=(actor or "")[:64],
        note=(note or "").strip()[:160],
        created_by=(actor or "")[:64],
    )
    session.add(period)
    await session.flush()

    result = await _grant(session, days, panels=panels, source=f"manual:{period.id}")
    await events.log_event(
        session,
        events.DOWNTIME_GRANTED,
        payload={"id": period.id, "days": days, "users": result.users, "actor": actor},
    )
    return result


def _days_for(started_at: datetime | None, ended_at: datetime, explicit: int | None) -> int:
    """Сколько полных суток начислить: считаем по факту, но не больше потолка."""
    if explicit is not None:
        return max(0, min(int(explicit), MAX_GRANT_DAYS))
    if started_at is None:
        return 0
    start = started_at.replace(tzinfo=timezone.utc) if started_at.tzinfo is None else started_at
    elapsed = ended_at - start
    return max(0, min(int(elapsed // timedelta(days=1)), MAX_GRANT_DAYS))


async def _grant(
    session: AsyncSession,
    days: int,
    *,
    panels: list[PanelClient] | None,
    source: str,
) -> GrantResult:
    """Продлить все активные подписки на ``days`` дней."""
    if days <= 0:
        return GrantResult(ok=True, days=0, users=0)

    panel_list = panels if panels is not None else await subscriptions.all_user_panels(session)
    subs = list(
        (
            await session.scalars(
                select(Subscription).where(
                    Subscription.status.in_(GRANT_STATUSES),
                    Subscription.panel_user_uuid.is_not(None),
                )
            )
        ).all()
    )

    extended = 0
    tg_ids: list[int] = []
    for sub in subs:
        user = await session.get(User, sub.user_id)
        if user is None:
            continue
        result = await subscriptions.extend_days(session, user, days, panel_list, reason="downtime")
        if result is not None:
            extended += 1
            tg_ids.append(user.tg_id)

    return GrantResult(ok=True, days=days, users=extended, reason=source, tg_ids=tg_ids)
