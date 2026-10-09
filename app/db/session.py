"""Подключение к БД и создание схемы."""

from __future__ import annotations

import logging

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.models import Base

logger = logging.getLogger(__name__)

_settings = get_settings()


class UnsupportedDialectError(RuntimeError):
    """Понятная ошибка вместо тихой поломки на не-SQLite базе."""


#: PRAGMA, которые включаем на КАЖДОМ соединении SQLite.
#:
#: Зачем каждый раз, а не один: ``journal_mode`` живёт в файле базы, а
#: ``busy_timeout``/``foreign_keys``/``synchronous`` — свойства соединения, и
#: новое соединение из пула получает значения по умолчанию. Без этого второй
#: писатель падал с «database is locked» через 5 секунд, а ссылочная
#: целостность не проверялась вовсе.
SQLITE_PRAGMAS: tuple[tuple[str, str], ...] = (
    # WAL: читатели не блокируют писателя, отчёты в панели не «замирают».
    ("journal_mode", "WAL"),
    # Ждать блокировку 15 секунд вместо «database is locked» на пятой.
    ("busy_timeout", "15000"),
    # WAL + NORMAL: коммит быстрый, при падении процесса данные не теряются
    # (рискует только последняя транзакция при отключении питания).
    ("synchronous", "NORMAL"),
    # Ссылочная целостность: «сирота» (заказ удалённого пользователя) должен
    # ловиться вставкой, а не находиться через полгода в отчётах.
    ("foreign_keys", "ON"),
)


def sqlite_pragmas(dialect_name: str) -> tuple[tuple[str, str], ...]:
    """PRAGMA для диалекта или понятная ошибка для не-SQLite.

    Приложение работает на SQLite; для PostgreSQL эти настройки задаются на
    сервере. Молча их игнорировать нельзя: поведение в бою оказалось бы не тем,
    что проверяли в тестах.
    """
    if dialect_name != "sqlite":
        raise UnsupportedDialectError(
            f"Диалект {dialect_name!r}: PRAGMA journal_mode/busy_timeout/foreign_keys "
            "применимы только к SQLite. Для PostgreSQL включи foreign keys и таймауты "
            "блокировок на сервере и заведи миграции Alembic: лёгкие миграции из "
            "app/db/session.py работают только с SQLite."
        )
    return SQLITE_PRAGMAS


engine = create_async_engine(_settings.resolved_db_url, echo=False, future=True)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _record) -> None:  # noqa: ANN001
    """Настроить соединение сразу после открытия.

    Обработчик синхронный — так требует SQLAlchemy для события ``connect``.
    aiosqlite выполняет его в потоке соединения, поэтому вставки в очередь нет.
    """
    dialect = getattr(dbapi_connection, "dialect", None)
    name = getattr(dialect, "name", None) or engine.dialect.name
    if name != "sqlite":
        logger.warning(
            "Соединение %s: PRAGMA SQLite не применяются — настрой foreign keys и "
            "таймауты блокировок на стороне сервера БД.",
            name,
        )
        return
    cursor = dbapi_connection.cursor()
    try:
        for pragma, value in SQLITE_PRAGMAS:
            cursor.execute(f"PRAGMA {pragma}={value}")
    finally:
        cursor.close()


SessionMaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Создаёт таблицы, добавляет недостающие колонки и наполняет справочник тарифов."""
    async with engine.begin() as conn:
        if conn.dialect.name != "sqlite":
            # Явная ошибка: без неё база на другом диалекте осталась бы без схемы.
            raise sqlite_pragmas(conn.dialect.name)
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
    # Снимок условий тарифа на момент заказа: выдача обязана дать ровно то, что
    # оплатили, даже если прайс поправили в окне заказа (находка 09.10.2026).
    # NULL — снимка нет (заказы до этого дня), берём живой тариф, как раньше.
    ("orders", "plan_days", "INTEGER"),
    ("orders", "plan_devices_limit", "INTEGER"),
    ("orders", "plan_traffic_gb", "INTEGER"),
    # Захват выдачи: пока он стоит, фоновая задача не зовёт панель второй раз по
    # тому же платежу. Истекает сам, если процесс упал.
    ("orders", "grant_claimed_at", "DATETIME"),
    ("users", "promo_code", "VARCHAR(32)"),
    ("users", "bonus_days_balance", "INTEGER DEFAULT 0"),
    ("users", "tags", "VARCHAR(128) DEFAULT ''"),
    # Обязательная подписка на канал: когда проверку проходили в последний раз.
    ("users", "channel_verified_at", "DATETIME"),
    # Ретенция: когда персональные данные удалены (по запросу или через 12 мес.).
    ("users", "anonymized_at", "DATETIME"),
    # Версия админ-сессий: выход, смена пароля/роли, деактивация гасят старые cookie.
    ("admin_accounts", "session_version", "INTEGER DEFAULT 1"),
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
    # Уникальность копеек среди открытых заказов: два клиента на одну сумму не
    # должны получить одинаковую подпись (H3). Индекс частичный: у заказов без
    # ручной оплаты копеек нет (pay_kopecks = 0), и общий UNIQUE запретил бы
    # два счёта Stars/Platega на одну сумму.
    (
        "uq_orders_pending_kopeck",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_pending_kopeck "
        "ON orders (amount_rub, pay_kopecks) WHERE status = 'pending' AND pay_kopecks > 0",
    ),
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
    for name, ddl in _EXTRA_INDEXES:
        try:
            await conn.exec_driver_sql(ddl)
        except Exception as exc:  # noqa: BLE001 - таблицы может ещё не быть в старой базе
            # Молча пропускать нельзя: если уникальный индекс копеек не встал,
            # значит в базе уже есть дубли и автоплатёж будет путать заказы.
            logger.warning(
                "Индекс %s не создан (%s). Если это uq_orders_pending_kopeck — в базе есть "
                "pending-заказы с одинаковыми суммой и копейками: разбери их вручную "
                "(SELECT amount_rub, pay_kopecks, COUNT(*) FROM orders WHERE status='pending' "
                "GROUP BY 1,2 HAVING COUNT(*) > 1) и перезапусти бота.",
                name,
                exc,
            )


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
