"""Интеграционный тест сценария бота: /start → пробный доступ → меню → оплата.

Проверяем реальную сборку Dispatcher + middleware + хендлеров, но без сети:
запросы к Telegram API перехватывает FakeSession.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User as TgUser

from app.bot.handlers import build_router
from app.bot.middlewares import DbSessionMiddleware, UserMiddleware
from app.panels.registry import registry
from app.services import subscriptions

BOT_TOKEN = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


class FakeSession(BaseSession):
    """Заглушка Telegram API: запоминает методы и отдаёт правдоподобные ответы."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod] = []

    async def close(self) -> None:  # pragma: no cover
        return None

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        self.requests.append(method)
        name = type(method).__name__
        if name in {"SendMessage", "EditMessageText"}:
            return Message(
                message_id=len(self.requests),
                date=datetime.now(timezone.utc),
                chat=Chat(id=1, type="private"),
                text=getattr(method, "text", "") or "edited",
            )
        if name == "GetMe":
            return TgUser(id=1, is_bot=True, first_name="Kometa", username="kometa_test_bot")
        return True

    def texts(self) -> list[str]:
        result: list[str] = []
        for request in self.requests:
            text = getattr(request, "text", None)
            if isinstance(text, str) and text:
                result.append(text)
        return result

    def buttons(self) -> list[str]:
        """Тексты кнопок: цены и варианты живут именно в клавиатурах."""
        result: list[str] = []
        for request in self.requests:
            markup = getattr(request, "reply_markup", None)
            if markup is None:
                continue
            for row in getattr(markup, "inline_keyboard", None) or []:
                result.extend(button.text for button in row)
            for row in getattr(markup, "keyboard", None) or []:
                result.extend(button.text for button in row)
        return result

    def all_text(self) -> str:
        return " ".join(self.texts() + self.buttons())

    def clear(self) -> None:
        self.requests.clear()


@pytest.fixture
async def bot():
    bot = Bot(token=BOT_TOKEN, session=FakeSession())
    yield bot
    await bot.session.close()


@pytest.fixture(scope="session")
def dispatcher():
    """Роутеры — модульные синглтоны, поэтому Dispatcher собираем один раз."""
    dp = Dispatcher()
    for observer in (dp.message, dp.callback_query):
        observer.middleware(DbSessionMiddleware())
        observer.middleware(UserMiddleware())
    dp.include_router(build_router())
    return dp


def make_update(text: str | None = None, callback_data: str | None = None, user_id: int = 42) -> Update:
    tg_user = TgUser(id=user_id, is_bot=False, first_name="Тест", username="tester")
    chat = Chat(id=user_id, type="private")
    if callback_data is not None:
        message = Message(message_id=1, date=datetime.now(timezone.utc), chat=chat, text="меню")
        return Update(
            update_id=1,
            callback_query=CallbackQuery(
                id="cb1",
                from_user=tg_user,
                chat_instance="ci1",
                data=callback_data,
                message=message,
            ),
        )
    return Update(
        update_id=2,
        message=Message(message_id=2, date=datetime.now(timezone.utc), chat=chat, from_user=tg_user, text=text),
    )


async def test_start_creates_user_and_shows_menu(bot, dispatcher, session):
    await dispatcher.feed_update(bot, make_update("/start", user_id=7001))

    user = await subscriptions.get_user_by_tg(session, 7001)
    assert user is not None
    assert user.referral_code

    texts_sent = bot.session.all_text()
    assert "Kometa" in texts_sent
    assert "Попробовать бесплатно" in texts_sent or "пробн" in texts_sent.lower()


async def test_start_with_referral_payload_links_users(bot, dispatcher, session):
    inviter, _ = await subscriptions.get_or_create_user(session, tg_id=7100, username="inviter")
    await session.commit()

    await dispatcher.feed_update(bot, make_update(f"/start ref_{inviter.referral_code}", user_id=7101))

    invited = await subscriptions.get_user_by_tg(session, 7101)
    assert invited is not None
    assert invited.referred_by == inviter.id
    assert "пригласил" in bot.session.all_text().lower()


async def test_trial_button_creates_subscription_and_gives_link(bot, dispatcher, session):
    await dispatcher.feed_update(bot, make_update("/start", user_id=7201))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="trial:start", user_id=7201))

    user = await subscriptions.get_user_by_tg(session, 7201)
    assert user is not None
    sub = await subscriptions.get_subscription(session, user.id)
    assert sub is not None and sub.status == "trial"
    assert sub.panel_user_uuid

    panel = registry.primary()
    panel_user = await panel.get_user(sub.panel_user_uuid)
    assert panel_user is not None and panel_user.enabled

    sent = bot.session.all_text()
    assert "/sub/" in sent  # ссылка-подписка ушла пользователю


async def test_trial_is_not_given_twice(bot, dispatcher, session):
    await dispatcher.feed_update(bot, make_update("/start", user_id=7301))
    await dispatcher.feed_update(bot, make_update(callback_data="trial:start", user_id=7301))
    await dispatcher.feed_update(bot, make_update(callback_data="trial:start", user_id=7301))

    sent = bot.session.all_text().lower()
    assert "уже использован" in sent


async def test_plans_are_listed_with_prices(bot, dispatcher, session):
    await dispatcher.feed_update(bot, make_update("/start", user_id=7401))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="plans", user_id=7401))

    sent = bot.session.all_text()
    assert "199" in sent and "1590" in sent


async def test_unknown_text_gets_helpful_answer(bot, dispatcher, session):
    await dispatcher.feed_update(bot, make_update("/start", user_id=7501))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update("привет, а как это работает?", user_id=7501))

    assert bot.session.texts(), "пользователь не должен оставаться без ответа"
