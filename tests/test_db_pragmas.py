"""Тесты настроек SQLite-соединения (H5).

Почему это не «мелочь в конфиге». В аудите 08.10.2026 замеры на живом движке
показали: ``journal_mode = delete``, ``foreign_keys = 0``, ``busy_timeout =
5000``. Следствия в бою: второй писатель падал с «database is locked» (в вебхуке
это HTTP 500 и потерянный платёж), а «сироты» в данных не ловились вовсе.

Здесь проверяем три вещи:

* PRAGMA читаются с соединения (WAL, busy_timeout 15 с, NORMAL, FK включены);
* два конкурентных писателя договариваются ожиданием, а не ошибкой;
* на не-SQLite диалект приложение отвечает понятной ошибкой, а не молчит.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from app.db.models import Event, Subscription, User
from app.db.session import SessionMaker, UnsupportedDialectError, engine, sqlite_pragmas
from app.services import subscriptions


async def _pragma(name: str) -> str | int:
    async with engine.connect() as conn:
        value = await conn.scalar(text(f"PRAGMA {name}"))
    return value  # type: ignore[return-value]


async def _make_user(session, tg_id: int) -> User:  # noqa: ANN001
    user, _ = await subscriptions.get_or_create_user(session, tg_id=tg_id, username=f"p{tg_id}")
    await session.flush()
    return user


async def test_pragmas_are_read_from_connection():
    """WAL, busy_timeout, synchronous и foreign_keys — как в согласованном плане."""
    assert await _pragma("journal_mode") == "wal"
    assert int(await _pragma("busy_timeout")) == 15_000
    assert int(await _pragma("synchronous")) == 1  # 1 = NORMAL
    assert int(await _pragma("foreign_keys")) == 1


async def test_pragmas_are_applied_to_every_new_connection():
    """Настройки не «одноразовые»: новое соединение из пула тоже настроено.

    busy_timeout и foreign_keys — свойства соединения, а не файла базы. Если
    ставить их один раз на старте, после первого же переподключения они
    сбрасываются на значения по умолчанию.
    """
    values = []
    async with engine.connect() as conn:
        values.append(int(await conn.scalar(text("PRAGMA busy_timeout"))))
    async with engine.connect() as conn:
        values.append(int(await conn.scalar(text("PRAGMA foreign_keys"))))
    assert values == [15_000, 1]


async def test_two_concurrent_writers_wait_instead_of_locked(session):
    """Второй писатель не получает «database is locked» на двух конкурентных апдейтах.

    Воспроизведение аудита: писатель №1 держит транзакцию, писатель №2 коммитит
    через 5,2 секунды и падает. Теперь второй ждёт освобождения (busy_timeout
    15 с) и завершает работу успешно.
    """
    user = await _make_user(session, 9301)
    await session.commit()

    async def slow_writer() -> None:
        async with SessionMaker() as other:
            await other.execute(update(User).where(User.id == user.id).values(first_name="Первый"))
            await asyncio.sleep(0.5)  # держим блокировку дольше прежнего таймаута 0,5 с
            await other.commit()

    async def waiting_writer() -> None:
        await asyncio.sleep(0.1)
        async with SessionMaker() as other:
            await other.execute(update(User).where(User.id == user.id).values(username="vtoroy"))
            await other.commit()

    await asyncio.gather(slow_writer(), waiting_writer())
    await session.refresh(user)
    assert user.first_name == "Первый"
    assert user.username == "vtoroy"


async def test_foreign_keys_are_enforced(session):
    """FK включены: подписка без пользователя больше не вставляется молча."""
    session.add(
        Subscription(
            user_id=987654,
            status="active",
            subscription_token="fk-orphan",
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_valid_rows_still_insert(session):
    """С включёнными FK нормальные строки вставляются как раньше."""
    user = await _make_user(session, 9302)
    session.add(Event(kind="start", user_id=user.id))
    await session.commit()

    assert await session.scalar(select(Event).where(Event.user_id == user.id)) is not None


def test_non_sqlite_dialect_gets_a_clear_error():
    """Для PostgreSQL — понятный текст: что включить и почему PRAGMA не работают."""
    assert dict(sqlite_pragmas("sqlite"))["journal_mode"] == "WAL"

    with pytest.raises(UnsupportedDialectError) as exc:
        sqlite_pragmas("postgresql")

    message = str(exc.value)
    assert "postgresql" in message
    assert "SQLite" in message
    assert "Alembic" in message


async def test_init_db_creates_schema_on_sqlite(tmp_path, monkeypatch):
    """init_db на SQLite проходит и создаёт таблицы (обратная сторона проверки диалекта)."""
    from app.db import session as db_session

    created: list[str] = []

    class _Result:
        def fetchall(self) -> list:  # noqa: ANN202
            return []

    class _FakeConn:
        dialect = type("D", (), {"name": "sqlite"})()

        async def run_sync(self, fn):  # noqa: ANN001, ANN202
            created.append("create_all")
            return None

        async def exec_driver_sql(self, ddl):  # noqa: ANN001, ANN202
            created.append(ddl)
            return _Result()

    class _Begin:
        async def __aenter__(self):  # noqa: ANN202
            return _FakeConn()

        async def __aexit__(self, *exc):  # noqa: ANN002, ANN202
            return False

    class _FakeEngine:
        def begin(self):  # noqa: ANN202
            return _Begin()

    async def _noop_seed() -> None:
        created.append("seed")

    monkeypatch.setattr(db_session, "engine", _FakeEngine())
    monkeypatch.setattr(db_session, "seed_plans", _noop_seed)

    await db_session.init_db()

    assert "create_all" in created and "seed" in created
