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
    from app.panels.fake import FakePanel

    return FakePanel()
