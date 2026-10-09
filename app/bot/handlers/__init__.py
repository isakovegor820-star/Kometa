"""Сборка всех роутеров бота.

Порядок ВАЖЕН:
    1) пользовательские роутеры (start → channel → trial → buy → subscription →
          referral → profile → gift → legal);
    2) админский роутер (у него собственный catch-all с фильтром IsAdmin);
    3) пользовательский фолбэк — последним, чтобы не съедать чужие кнопки.
"""

from __future__ import annotations

from aiogram import Router

from app.bot.handlers import (
                admin,
                buy,
                channel,
                fallback,
                gift,
                legal,
                profile,
                referral,
                start,
                subscription,
                trial,
)


def build_router() -> Router:
                root = Router(name="root")
                root.include_router(start.router)
                root.include_router(channel.router)
                root.include_router(trial.router)
                root.include_router(buy.router)
                root.include_router(subscription.router)
                root.include_router(referral.router)
                root.include_router(profile.router)
                root.include_router(gift.router)
                root.include_router(legal.router)
                root.include_router(admin.router)
                root.include_router(fallback.router)
                return root
