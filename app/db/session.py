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
#: Новые таблицы (промокоды, алерты, роли) create_all создаёт сам — здесь только колонки.
_EXTRA_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("orders", "pay_kopecks", "INTEGER DEFAULT 0"),
    ("orders", "base_amount_rub", "INTEGER DEFAULT 0"),
    ("orders", "discount_rub", "INTEGER DEFAULT 0"),
    ("orders", "promo_code", "VARCHAR(32)"),
    ("orders", "stars_amount", "INTEGER DEFAULT 0"),
    ("orders", "refunded_at", "DATETIME"),
    ("orders", "refunded_by", "VARCHAR(64)"),
    ("orders", "refund_note", "TEXT"),
    # Оплата по закрытому заказу: когда об этом сообщили админам (один раз).
    ("orders", "payment_alerted_at", "DATETIME"),
    # Выдача доступа после оплаты: цель, факт, число попыток и текст ошибки.
    # Нужны, чтобы «деньги приняты, доступа нет» не терялось: заказ с пустым
    # granted_at подхватывает фоновая задача и повторяет выдачу.
    ("orders", "grant_target_at", "DATETIME"),
    ("orders", "granted_at", "DATETIME"),
    ("orders", "grant_attempts", "INTEGER DEFAULT 0"),
    ("orders", "grant_last_error", "VARCHAR(300) DEFAULT ''"),
    ("users", "promo_code", "VARCHAR(32)"),
    ("users", "bonus_days_balance", "INTEGER DEFAULT 0"),
    ("users", "tags", "VARCHAR(128) DEFAULT ''"),
    # Обязательная подписка на канал: когда проверку проходили в последний раз.
    ("users", "channel_verified_at", "DATETIME"),
    # Источник привлечения: из какого канала/размещения пришёл человек.
    # Без этих полей нельзя посчитать CAC по каналам (docs/МАРКЕТИНГ-ЭКОНОМИКА.md).
    ("users", "source", "VARCHAR(32) DEFAULT ''"),
    ("users", "source_detail", "VARCHAR(64) DEFAULT ''"),
    ("users", "source_at", "DATETIME"),
    # Автосценарии: когда и что последний раз отправляли, чтобы не спамить.
    ("users", "last_lifecycle_at", "DATETIME"),
    ("users", "last_lifecycle_kind", "VARCHAR(32) DEFAULT ''"),
    # Партнёры: ссылка ``?start=src_<код>``, выплата за платежи, история расчётов.
    ("users", "partner_id", "INTEGER"),
    ("orders", "partner_id", "INTEGER"),
    ("orders", "partner_reward_rub", "FLOAT DEFAULT 0"),
    ("promo_codes", "partner_id", "INTEGER"),
    # Многоразовый промокод: работает и после первой использованной скидки.
    ("promo_codes", "repeatable", "BOOLEAN DEFAULT 0"),
    # Персональные ссылки под конкретного человека: своя скидка и свой срок.
    ("users", "personal_link_id", "INTEGER"),
    ("promo_codes", "personal_link_id", "INTEGER"),
    ("referrals", "rewarded_at", "DATETIME"),
    # Награда за продления друга: рефералка, привязанная к удержанию.
    ("referrals", "renewal_bonus_days", "INTEGER DEFAULT 0"),
    ("referrals", "renewals_count", "INTEGER DEFAULT 0"),
    ("referrals", "renewal_rewarded_at", "DATETIME"),
    # Подарочный сертификат: покупка в подарок с активацией позже.
    ("orders", "gift_token", "VARCHAR(32)"),
    ("orders", "gift_recipient_tg_id", "BIGINT"),
    ("orders", "gift_message", "VARCHAR(200)"),
    ("orders", "gift_activated_at", "DATETIME"),
    ("orders", "gift_activated_by", "INTEGER"),
    # Аудит действий администратора — в том же журнале событий.
    ("events", "actor_name", "VARCHAR(64)"),
    ("events", "actor_role", "VARCHAR(16)"),
    ("events", "actor_tg_id", "BIGINT"),
    ("events", "source", "VARCHAR(8)"),
    ("events", "ip", "VARCHAR(45)"),
    # Аварийный уровень: канал ноды (main/reserve/cdn) и замер пинга
    # «глазами клиента» — TCP/TLS-проба до инбаунда.
    ("nodes", "sub_base", "VARCHAR(255) DEFAULT ''"),
    ("nodes", "channel", "VARCHAR(8) DEFAULT 'main'"),
    ("nodes", "test_url", "VARCHAR(255) DEFAULT ''"),
    ("nodes", "last_probe_at", "DATETIME"),
    ("nodes", "last_probe_ok", "BOOLEAN DEFAULT 0"),
    ("nodes", "last_probe_ms", "INTEGER DEFAULT 0"),
    # Причины последних неудач: без них админка не отличала «порт закрыт» от
    # «панель не отдала инбаунды» и показывала неверное действие.
    ("nodes", "last_check_error", "VARCHAR(300) DEFAULT ''"),
    ("nodes", "last_probe_error", "VARCHAR(300) DEFAULT ''"),
    # Шаг последней пробы: tcp/tls — порт проверен, panel/config — не состоялась.
    ("nodes", "last_probe_stage", "VARCHAR(16) DEFAULT ''"),
    # Замер по каждому TCP-порту (JSON): доказательство для диагноза
    # «какой именно порт не пускает», когда другой открыт.
    ("nodes", "last_probe_ports", "TEXT DEFAULT ''"),
)

#: Индексы для запросов панели: create_all создаёт их только на новых базах.
_EXTRA_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_orders_status_created", "CREATE INDEX IF NOT EXISTS ix_orders_status_created ON orders (status, created_at)"),
    ("ix_events_kind_created", "CREATE INDEX IF NOT EXISTS ix_events_kind_created ON events (kind, created_at)"),
    ("ix_alerts_status_created", "CREATE INDEX IF NOT EXISTS ix_alerts_status_created ON alerts (status, created_at)"),
)


async def _apply_light_migrations(conn) -> None:  # noqa: ANN001 - AsyncConnection
    """Лёгкие миграции для SQLite: добавляет недостающие колонки и индексы.

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
    for _name, ddl in _EXTRA_INDEXES:
        try:
            await conn.exec_driver_sql(ddl)
        except Exception:  # noqa: BLE001 - таблицы может ещё не быть в старой базе
            pass


async def seed_plans() -> None:
    """Идемпотентно создаёт тарифы (цены можно менять в БД).

    Важно: сид только ДОБАВЛЯЕТ отсутствующие тарифы и никогда не перезаписывает
    цену существующих. Смена цен — через админку (/admin) или SQL; правка этих
    констант влияет лишь на новые установки.
    """
    from sqlalchemy import select

    from app.db.models import Plan

    defaults = [
        dict(code="m1", title="1 месяц", days=30, price_rub=120, price_stars=110, devices_limit=3, sort_order=1),
        dict(code="m3", title="3 месяца", days=90, price_rub=299, price_stars=270, devices_limit=3, sort_order=2),
        dict(code="m6", title="6 месяцев", days=180, price_rub=539, price_stars=485, devices_limit=3, sort_order=3),
        dict(code="m12", title="12 месяцев", days=365, price_rub=959, price_stars=860, devices_limit=3, sort_order=4),
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
