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
os.environ["MANUAL_PAYMENT_DETAILS"] = "СБП: +7 900 000-00-00 (тест)"
# Платёжные доступы в тестах пустые. Боевые ключи лежат в .env, и без этого
# тест, забывший подменить транспорт, ушёл бы в реальный API платёжной системы.
# Кому нужен настроенный провайдер — выставляет значения у себя (см.
# tests/test_platega_webhook.py, tests/test_platega_polling.py).
os.environ["PLATEGA_MERCHANT_ID"] = ""
os.environ["PLATEGA_SECRET"] = ""
os.environ["CRYPTOBOT_TOKEN"] = ""
# Продажи в тестах открыты по умолчанию: боевой .env может держать их
# закрытыми до готовности ноды, но это не должно ломать сценарии покупки.
os.environ["SALES_ENABLED"] = "true"
# Гейт подписки на канал в тестах выключен: сценарии бота проверяются без
# Telegram API. Кто проверяет сам гейт — включает его у себя (test_channel_gate).
os.environ["CHANNEL_GATE_ENABLED"] = "false"
os.environ["CHANNEL_ID"] = ""
# Админ-панель: пароль обязателен, иначе все страницы /admin отвечают
# «Панель выключена». Раньше значения приходили из боевого .env (кэш настроек
# не сбрасывался), и тесты были зелёными по случайности, а не по замыслу.
os.environ["ADMIN_PANEL_PASSWORD"] = "test-admin-password"
os.environ["ADMIN_PANEL_SECRET"] = "test-admin-secret"
# Панель открыта для тестовых адресов: запросы идут с подставных IP
# (203.0.113.x), а боевой режим «только localhost» их не пускает.
# Сам запрет проверяется отдельно и явно — в tests/test_admin_access.py.
os.environ["ADMIN_LOCAL_ONLY"] = "false"


@pytest.fixture(autouse=True)
def reset_panel_registry():
    """Реестр панелей — процессный кэш; между тестами его нужно очищать,
    иначе пользователи «перетекают» из теста в тест."""
    from app.panels.registry import registry

    registry._cache.clear()
    yield
    registry._cache.clear()


@pytest.fixture(autouse=True)
def isolate_settings_from_env_file():
    """Тесты не читают боевой ``.env`` — только переменные окружения.

    Зачем. ``Settings`` объявлен с ``env_file=BASE_DIR / ".env"``, а ``get_settings``
    кэширован. Пока кэш жив, тесты видят окружение из шапки этого файла (оно
    выставлено до импорта приложения). Но стоит любому тесту вызвать
    ``get_settings.cache_clear()`` — и настройки перечитываются уже **с диска**,
    то есть с боевого ``.env``: в тестах появляются ``PANEL_TYPE=xui``,
    ``SALES_ENABLED=false``, ``PLATEGA_METHODS``, токены. Дальше падают чужие
    тесты, и причина не видна: одиночный прогон зелёный, полный — красный
    (08.10.2026 таких падений было 40, почти все платёжные).

    Решение: на время теста ``Settings`` перестаёт читать файл, а кэш настроек
    сбрасывается до и после каждого теста. Так тест, поменявший окружение или
    настройку, не оставляет значение соседу — раньше это давало «плавающие»
    падения в зависимости от случайного порядка.

    Проверять сам ``.env`` (парсер, запись, чтение) нужно отдельными тестами с
    временным файлом — так и делает ``tests/test_configure_env.py``.
    """
    from app import config as config_module

    original_config = dict(config_module.Settings.model_config)
    # env_file убираем ДО первого создания настроек: иначе любой тест, сбросивший
    # кэш, перечитает боевой .env с диска.
    config_module.Settings.model_config = config_module.SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
    )
    # Кэш настроек остаётся живым на весь прогон: тесты (и модули вроде
    # tests/test_admin_autopay.py) держат ссылку на ЭКЗЕМПЛЯР настроек и правят
    # его поля. Если сбрасывать кэш между тестами, они правили бы старый объект,
    # а код читал новый — и тест падал бы по причине, которой нет в коде.
    settings_instance = config_module.get_settings()
    snapshot = {
        name: getattr(settings_instance, name)
        for name in (*config_module.Settings.model_fields, *config_module.Settings.model_computed_fields)
    }
    try:
        yield
    finally:
        # Возвращаем настройки к состоянию до теста: monkeypatch снимает свои
        # патчи, но прямое присваивание (`settings.x = ...`) осталось бы соседу.
        for name, value in snapshot.items():
            try:
                setattr(settings_instance, name, value)
            except (AttributeError, ValueError):  # pragma: no cover - защитная ветка
                continue
        config_module.Settings.model_config = original_config  # type: ignore[assignment]


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
    from app.bot.middlewares import ChannelGateMiddleware, DbSessionMiddleware, UserMiddleware

    dp = Dispatcher()
    for observer in (dp.message, dp.callback_query):
        observer.middleware(DbSessionMiddleware())
        observer.middleware(UserMiddleware())
        # Порядок как в проде (app/main.py): гейт — после пользователя,
        # иначе ему нечего проверять.
        observer.middleware(ChannelGateMiddleware())
    dp.pre_checkout_query.middleware(DbSessionMiddleware())
    dp.pre_checkout_query.middleware(UserMiddleware())
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
