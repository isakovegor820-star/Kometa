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
    """Создаёт таблицы, добавляет недостающие колонки и наполняет справочник тарифов."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _apply_light_migrations(conn)
    await seed_plans()


#: Колонки, добавленные после первого релиза: (таблица, колонка, DDL).
#: create_all не меняет существующие таблицы, поэтому дописываем вручную.
_EXTRA_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("orders", "pay_kopecks", "INTEGER DEFAULT 0"),
)


async def _apply_light_migrations(conn) -> None:  # noqa: ANN001 - AsyncConnection
    """Лёгкие миграции для SQLite: добавляет недостающие колонки.

    Для PostgreSQL используем Alembic — здесь только чтобы существующая
    база разработчика не отвалилась после обновления кода.
    """
    if conn.dialect.name != "sqlite":
        return
    for table, column, ddl in _EXTRA_COLUMNS:
        result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
        existing = {row[1] for row in result.fetchall()}
        if existing and column not in existing:
            await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


async def seed_plans() -> None:
    """Идемпотентно создаёт тарифы из ТЗ (цены можно менять в БД)."""
    from sqlalchemy import select

    from app.db.models import Plan

    defaults = [
        dict(code="m1", title="1 месяц", days=30, price_rub=199, price_stars=180, devices_limit=3, sort_order=1),
        dict(code="m3", title="3 месяца", days=90, price_rub=499, price_stars=450, devices_limit=3, sort_order=2),
        dict(code="m6", title="6 месяцев", days=180, price_rub=890, price_stars=800, devices_limit=3, sort_order=3),
        dict(code="m12", title="12 месяцев", days=365, price_rub=1590, price_stars=1420, devices_limit=3, sort_order=4),
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
