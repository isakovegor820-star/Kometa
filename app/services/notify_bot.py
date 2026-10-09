"""Бот уведомлений: отдельный Telegram-бот для оперативных сообщений команде.

Зачем отдельный бот, а не основной:

* в личке бота продаж оперативная сводка (оплаты, заявки, алерты нод) тонет
  среди переписки с клиентами, и её легко пропустить;
* уведомления можно увести в отдельную группу или канал команды
  (``NOTIFY_CHAT_IDS``), не смешивая с клиентским ботом;
* отзыв токена и падение уведомлений не задевают продажи: клиентский бот
  продолжает работать.

Здесь живёт только транспорт «бот → команда»: сам бот, его команды
(``/status``, ``/money``, ``/digest``, ``/nodes``) и кнопки подтверждения
заявок. Сообщения **клиентам** всегда уходят основным ботом — иначе клиент
получил бы письмо от бота, которого не знает.

Токен не задан (``NOTIFY_BOT_TOKEN`` пуст) — модуль молчит, а уведомления
идут основным ботом, как раньше: старые настройки продолжают работать.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import ErrorEvent

from app.bot.middlewares import DbSessionMiddleware
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

#: Экземпляр бота уведомлений (None — не настроен или не поднялся).
_bot: Bot | None = None
#: Основной (клиентский) бот. Нужен командам бота уведомлений: подтверждая
#: заявку из чата команды, клиенту пишем от того бота, которого он знает.
#: Ставится на старте из ``app.main``.
_customer_bot: Bot | None = None
#: Юзернейм бота уведомлений — для стартового сообщения и подписи.
_username: str = ""
#: Почему отдельный бот недоступен (пусто — всё в порядке).
_problem: str = ""
_dispatcher: Dispatcher | None = None


def configured() -> bool:
    """Задан ли отдельный бот уведомлений."""
    return bool(settings.notify_bot_token.strip())


def bot() -> Bot | None:
    return _bot


def set_customer_bot(instance: Bot | None) -> None:
    """Запомнить основной бот: им уходят сообщения клиентам."""
    global _customer_bot
    _customer_bot = instance


def customer_bot() -> Bot | None:
    """Основной бот для сообщений клиентам (None — ещё не поднят)."""
    return _customer_bot


def namespace() -> str:
    """Пространство имён кнопок решения по заявке.

    Сообщение должно попасть к тому боту, который его отправил: кнопки из чата
    уведомлений обрабатывает бот уведомлений (``adm:…``), кнопки из лички
    основного бота — основной бот (``admin:…``). Иначе тап выглядит как
    «кнопка не работает».
    """
    return "adm" if _bot is not None else "admin"


def username() -> str:
    """``@имя`` бота уведомлений (пусто, если он не поднялся)."""
    return f"@{_username}" if _username else ""


def problem() -> str:
    """Причина, по которой отдельный бот недоступен (для честного сообщения)."""
    return _problem


def set_bot(instance: Bot | None, *, name: str = "") -> None:
    """Подменить бота: тесты и разовые скрипты. В проде не нужен."""
    global _bot, _username, _problem
    _bot = instance
    if name:
        _username = name
    if instance is not None:
        _problem = ""


async def start() -> Bot | None:
    """Поднять бота уведомлений. Неудача не должна ронять сервис.

    Проверяем токен через ``getMe`` сразу на старте: узнать об отозванном
    токене в момент первой оплаты («уведомление не пришло») — худший вариант.
    """
    global _bot, _username, _problem
    if not configured():
        _bot = None
        _username = ""
        _problem = ""
        return None

    instance = Bot(
        token=settings.notify_bot_token.strip(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        me = await instance.get_me()
    except Exception as exc:  # noqa: BLE001 - сеть, отозванный токен, опечатка
        with contextlib.suppress(Exception):
            await instance.session.close()
        _bot = None
        _username = ""
        _problem = f"Telegram не принял токен: {exc}"
        logger.error("Бот уведомлений не поднялся: %s", exc)
        return None

    _bot = instance
    _username = me.username or ""
    _problem = ""
    logger.info(
        "Бот уведомлений: %s → получатели %s",
        username() or "(без юзернейма)",
        settings.notify_chat_id_list or "не заданы",
    )
    return instance


def build_dispatcher() -> Dispatcher:
    """Диспетчер бота уведомлений: команды команды и кнопки заявок."""
    global _dispatcher
    if _dispatcher is None:
        from app.bot.handlers.notify_admin import build_router

        dispatcher = Dispatcher()
        # Гейт подписки и пользовательские middleware здесь не нужны: бот
        # служебный, клиентов в нём нет. Сессия БД нужна — команды читают базу.
        for observer in (dispatcher.message, dispatcher.callback_query):
            observer.middleware(DbSessionMiddleware())
        dispatcher.include_router(build_router())
        dispatcher.errors.register(_on_error)
        _dispatcher = dispatcher
    return _dispatcher


async def _on_error(event: ErrorEvent) -> None:
    """Ошибка в самом боте уведомлений: пишем в лог, не зацикливаем пересылку."""
    logger.warning("Ошибка в боте уведомлений: %s", event.exception)


async def run_polling() -> None:
    """Держать long polling. Сбои сети и конфликты — не повод терять уведомления."""
    instance = _bot
    if instance is None:
        return
    dispatcher = build_dispatcher()
    delay = 5
    while True:
        try:
            # Вебхук на этом токене оставил бы getUpdates пустым: команды
            # перестали бы работать, а причина была бы не видна в логах.
            with contextlib.suppress(Exception):
                await instance.delete_webhook(drop_pending_updates=False)
            await dispatcher.start_polling(
                instance,
                allowed_updates=["message", "callback_query"],
                handle_signals=False,
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - цикл обязан жить дальше
            logger.warning("Polling бота уведомлений прерван: %s (повтор через %s с)", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)


async def start_polling() -> asyncio.Task[None] | None:
    """Запустить опрос в фоне. Возвращает задачу, чтобы её можно было отменить."""
    if _bot is None:
        return None
    return asyncio.create_task(run_polling(), name="notify-bot-polling")


async def close() -> None:
    """Закрыть сессию бота уведомлений."""
    global _bot
    if _bot is not None:
        with contextlib.suppress(Exception):
            await _bot.session.close()
        _bot = None
