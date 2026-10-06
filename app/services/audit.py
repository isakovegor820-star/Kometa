"""Аудит действий администраторов.

Зачем: панель — общий инструмент на несколько человек. На вопрос «кто выключил
ноду», «кто заблокировал клиента», «почему у человека минус 30 дней» должен
отвечать журнал, а не память модератора.

Пишем в тот же журнал ``events`` (kind с префиксом ``admin.``), поэтому история
клиента и история действий команды лежат рядом и ищутся одним запросом.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event

#: Источники действий: панель, бот, фоновая задача, внешний вебхук.
SOURCE_WEB = "web"
SOURCE_BOT = "bot"
SOURCE_AUTO = "auto"


@dataclass(frozen=True, slots=True)
class Actor:
    """Кто выполняет действие."""

    name: str
    role: str
    tg_id: int | None = None
    ip: str | None = None
    source: str = SOURCE_WEB

    @property
    def label(self) -> str:
        return self.name or "система"


#: Действия, которые пишем в журнал. Список закрытый: опечатка в строке
#: не должна незаметно создать новый вид события.
ACTION_PREFIX = "admin."


async def log_action(
    session: AsyncSession,
    action: str,
    *,
    actor: Actor | None = None,
    user_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> Event:
    """Записать действие администратора.

    :param action: код действия, например ``admin.order_confirm``.
    :param user_id: клиент, над которым действие выполнено (если есть).
    :param payload: подробности — id заказа, сумма, сколько дней, причина.
    """
    kind = action if action.startswith(ACTION_PREFIX) else f"{ACTION_PREFIX}{action}"
    event = Event(
        user_id=user_id,
        kind=kind,
        payload=json.dumps(payload, ensure_ascii=False, default=str) if payload else None,
        actor_name=(actor.label if actor else "система")[:64],
        actor_role=(actor.role if actor else SOURCE_AUTO)[:16],
        actor_tg_id=actor.tg_id if actor else None,
        source=(actor.source if actor else SOURCE_AUTO)[:8],
        ip=(actor.ip if actor else None),
    )
    session.add(event)
    return event


async def recent_actions(
    session: AsyncSession,
    *,
    limit: int = 100,
    offset: int = 0,
    actor: str = "",
    action: str = "",
    only_admin: bool = True,
) -> list[Event]:
    """Последние действия команды — от новых к старым."""
    stmt = select(Event).order_by(Event.id.desc()).limit(limit).offset(offset)
    if only_admin:
        stmt = stmt.where(Event.kind.like(f"{ACTION_PREFIX}%"))
    if actor:
        stmt = stmt.where(Event.actor_name == actor)
    if action:
        stmt = stmt.where(Event.kind == action)
    return list((await session.scalars(stmt)).all())


async def count_actions(session: AsyncSession, *, actor: str = "", action: str = "", only_admin: bool = True) -> int:
    from sqlalchemy import func

    stmt = select(func.count(Event.id))
    if only_admin:
        stmt = stmt.where(Event.kind.like(f"{ACTION_PREFIX}%"))
    if actor:
        stmt = stmt.where(Event.actor_name == actor)
    if action:
        stmt = stmt.where(Event.kind == action)
    return int(await session.scalar(stmt) or 0)
