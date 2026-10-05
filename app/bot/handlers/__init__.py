"""Сборка всех роутеров бота.

ВАЖНО: порядок важен — админский роутер подключается последним, у него
собственный catch-all, который не должен перехватывать пользовательские кнопки.
"""

from __future__ import annotations

from aiogram import Router

from app.bot.handlers import admin, buy, referral, start, subscription, trial


def build_router() -> Router:
    root = Router(name="root")
    root.include_router(start.router)
    root.include_router(trial.router)
    root.include_router(buy.router)
    root.include_router(subscription.router)
    root.include_router(referral.router)
    root.include_router(admin.router)
    return root
