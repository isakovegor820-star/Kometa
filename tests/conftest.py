"""Общие фикстуры тестов.

ВАЖНО: переменные окружения выставляются ДО импорта приложения, потому что
настройки и движок БД создаются на уровне модулей.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP_DB = Path(tempfile.mkdtemp(prefix="kometa-tests-")) / "test.db"
os.environ["DB_URL"] = f"sqlite+aiosqlite:///{_TMP_DB}"
os.environ.setdefault("BOT_TOKEN", "000000:TEST_TOKEN")
os.environ["PANEL_TYPE"] = "fake"
os.environ["ADMIN_IDS"] = "1"
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ["TRIAL_DAYS"] = "3"
os.environ["TRIAL_GB"] = "10"
os.environ["TRIAL_DEVICES"] = "1"
os.environ["STARS_ENABLED"] = "true"


@pytest.fixture(autouse=True)
def reset_panel_registry():
    """Реестр панелей — процессный кэш; между тестами его нужно очищать,
    иначе пользователи «перетекают» из теста в тест."""
    from app.panels.registry import registry

    registry._cache.clear()
    yield
    registry._cache.clear()


@pytest.fixture
async def bot():
    """Bot с заглушкой Telegram API; платежи инициализируются как в проде."""
    from aiogram import Bot

    from app.payments.registry import payments
    from tests.fakes import BOT_TOKEN, FakeSession

    bot = Bot(token=BOT_TOKEN, session=FakeSession())
    payments.init(bot)  # регистрирует Stars (нужен живой Bot)
    yield bot
    await payments.close()
    await bot.session.close()


@pytest.fixture(scope="session")
def dispatcher():
    """Dispatcher собирается ОДИН раз: роутеры — модульные синглтоны."""
    from aiogram import Dispatcher

    from app.bot.handlers import build_router
    from app.bot.middlewares import DbSessionMiddleware, UserMiddleware

    dp = Dispatcher()
    for observer in (dp.message, dp.callback_query, dp.pre_checkout_query):
        observer.middleware(DbSessionMiddleware())
        observer.middleware(UserMiddleware())
    dp.include_router(build_router())
    return dp


@pytest.fixture
async def session():
    """Чистая БД на каждый тест + справочник тарифов."""
    from app.db.models import Base
    from app.db.session import SessionMaker, engine, seed_plans

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await seed_plans()

    async with SessionMaker() as s:
        yield s


@pytest.fixture
def panel():
    """Панель-заглушка — тот же экземпляр, что видит приложение.

    Важно: берём его из реестра, иначе тест работал бы с одной панелью,
    а хендлеры — с другой, и пользователи «терялись».
    """
    from app.panels.registry import registry

    return registry.primary()
