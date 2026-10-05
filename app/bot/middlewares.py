"""Middleware: сессия БД, антифлуд, подгрузка пользователя."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot import texts
from app.config import get_settings
from app.db.session import SessionMaker
from app.services import subscriptions

settings = get_settings()


class DbSessionMiddleware(BaseMiddleware):
    """Одна сессия БД на апдейт + коммит в конце обработки."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with SessionMaker() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise


class UserMiddleware(BaseMiddleware):
    """Подкладывает в data нашего User (создаёт при первом обращении)."""

    async def __call__(self, handler, event, data):
        tg_user = data.get("event_from_user")
        session = data.get("session")
        if tg_user is not None and session is not None:
            user, _ = await subscriptions.get_or_create_user(
                session,
                tg_id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
            )
            data["user"] = user
        return await handler(event, data)


class ThrottlingMiddleware(BaseMiddleware):
    """Простая защита от флуда: не больше N действий в окно."""

    def __init__(self, rate: int = 5, window: float = 3.0) -> None:
        self.rate = rate
        self.window = window
        self._hits: dict[int, list[float]] = {}

    async def __call__(self, handler, event, data):
        tg_user = data.get("event_from_user")
        if tg_user is None or tg_user.id in settings.admin_id_list:
            return await handler(event, data)

        now = time.monotonic()
        hits = [t for t in self._hits.get(tg_user.id, []) if now - t < self.window]
        if len(hits) >= self.rate:
            self._hits[tg_user.id] = hits
            if isinstance(event, Message):
                await event.answer(texts.RATE_LIMITED)
            elif isinstance(event, CallbackQuery):
                await event.answer(texts.RATE_LIMITED, show_alert=False)
            return None

        hits.append(now)
        self._hits[tg_user.id] = hits
        return await handler(event, data)
