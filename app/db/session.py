"""Подключение к БД и создание схемы."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.models import Base

_settings = get_settings()
engine = create_async_engine(_settings.resolved_db_url, echo=False, future=True)
SessionMaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Создаёт таблицы и наполняет справочник тарифов."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await seed_plans()


async def seed_plans() -> None:
    """Идемпотентно создаёт тарифы из ТЗ (цены можно менять в БД)."""
    from sqlalchemy import select

    from app.db.models import Plan

    defaults = [
        dict(code="m1", title="1 месяц", days=30, price_rub=199, price_stars=150, devices_limit=3, sort_order=1),
        dict(code="m3", title="3 месяца", days=90, price_rub=499, price_stars=350, devices_limit=3, sort_order=2),
        dict(code="m6", title="6 месяцев", days=180, price_rub=890, price_stars=650, devices_limit=3, sort_order=3),
        dict(code="m12", title="12 месяцев", days=365, price_rub=1590, price_stars=1100, devices_limit=3, sort_order=4),
    ]
    async with SessionMaker() as session:
        for item in defaults:
            existing = await session.scalar(select(Plan).where(Plan.code == item["code"]))
            if existing is None:
                session.add(Plan(**item))
        await session.commit()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionMaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
