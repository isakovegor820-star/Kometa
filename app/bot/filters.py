"""Фильтры бота."""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject

from app.config import get_settings

settings = get_settings()


class IsAdmin(BaseFilter):
        """Пропускает только администраторов из ADMIN_IDS."""

        async def __call__(self, event: TelegramObject) -> bool:
                user = getattr(event, "from_user", None)
                return bool(user and user.id in settings.admin_id_list)
