"""Тесты гейта обязательной подписки на канал.

Проверяем поведение снаружи: гейт стоит в middleware, поэтому важно, что он
закрывает и кнопки, и текст — но при этом не трогает тех, у кого подписка на
сервис активна, не мешает поддержке и не теряет оплату звёздами.
"""

from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramBadRequest

from app.bot import texts
from app.config import get_settings
from app.bot import view
from app.services import channel_gate, orders, subscriptions
from tests.fakes import make_stars_payment_update, make_update

CHANNEL = "@kometa_test"
CHANNEL_URL = "https://t.me/kometa_test"
#: Начало экрана подписки — по нему и отличаем «гейт сработал» от меню.
GATE_MARK = "Остался один шаг"
#: Метка открытого меню. Новый человек видит hero: фото + подпись, поэтому
#: проверяем не «Главное меню», а сам факт, что бот пустил и показал меню.
MENU_MARK = texts.MENU_NO_SUB


@pytest.fixture
def gate_on(monkeypatch):
    """Включённый гейт: в тестах он выключен всегда (см. tests/conftest.py)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "channel_gate_enabled", True)
    monkeypatch.setattr(settings, "channel_id", CHANNEL)
    monkeypatch.setattr(settings, "channel_url", CHANNEL_URL)
    monkeypatch.setattr(settings, "channel_gate_cache_hours", 12)
    monkeypatch.setattr(settings, "channel_gate_fail_open", True)
    monkeypatch.setattr(channel_gate, "_alerted_at", 0.0)
    monkeypatch.setattr(channel_gate, "_not_subscribed_at", {})
    monkeypatch.setattr(channel_gate, "_unavailable_until", 0.0)
    return settings


def subscribe(bot, *user_ids: int) -> None:
    """«Подписать» людей на канал в заглушке Telegram."""
    for user_id in user_ids:
        bot.session.chat_members[user_id] = "member"


def telegram_silent(bot, message: str = "Bad Request: chat not found") -> None:
    """Проверка подписки не работает: бот не админ канала или сеть легла."""
    bot.session.chat_member_error = TelegramBadRequest(method="GetChatMember", message=message)


def shown(bot) -> str:
    """Всё, что бот отправил: сообщения, подписи к фото, кнопки и алерты."""
    return bot.session.all_text()


def _menu_shown(bot) -> bool:
    """Показал ли бот меню (а не экран подписки).

    Новому человеку меню уходит hero-сообщением: у фото нет ``text``, только
    подпись. Поэтому метка — либо текст меню, либо строка из hero-подписи.
    """
    sent = shown(bot)
    marks = (texts.MENU_NO_SUB, "3 дня бесплатно", texts.MENU_ACTIVE, texts.MENU_EXPIRED)
    return any(mark in sent for mark in marks)


async def user_subscription(session, tg_id: int):  # noqa: ANN001
    user = await subscriptions.get_user_by_tg(session, tg_id)
    return user, (await subscriptions.get_subscription(session, user.id) if user else None)


# ------------------------------------------------------------------ выключенный
async def test_gate_off_keeps_old_behaviour(bot, dispatcher, session):
    """Без гейта /start показывает меню и не спрашивает Telegram про канал."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=8001))

    assert GATE_MARK not in shown(bot)
    assert "пробн" in shown(bot).lower()
    assert not bot.session.by_name("GetChatMember")


# ------------------------------------------------------------------ гейт включён
async def test_start_asks_to_subscribe(bot, dispatcher, session, gate_on):
    await dispatcher.feed_update(bot, make_update("/start", user_id=8101))

    assert GATE_MARK in shown(bot)
    assert "Подписаться на канал" in shown(bot)
    assert CHANNEL_URL in bot.session.button_urls()
    assert bot.session.by_name("GetChatMember"), "подписку даже не проверили"


async def test_gate_blocks_buttons_until_subscribed(bot, dispatcher, session, gate_on):
    """Пока подписки нет, кнопки бота не работают: доступ не выдаётся."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=8201))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="trial:start", user_id=8201))

    user, sub = await user_subscription(session, 8201)
    assert user is not None
    assert sub is None, "доступ выдан человеку, который не подписался на канал"
    assert GATE_MARK in shown(bot)


async def test_button_after_subscribing_opens_the_bot(bot, dispatcher, session, gate_on):
    """Главный сценарий: подписался → нажал кнопку → пользуется ботом."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=8301))
    assert GATE_MARK in shown(bot)

    subscribe(bot, 8301)
    bot.session.clear()
    await dispatcher.feed_update(
        bot, make_update(callback_data=channel_gate.CALLBACK_CHECK, user_id=8301)
    )

    assert "Подписка подтверждена" in shown(bot)
    assert _menu_shown(bot)

    # и дальше бот работает как обычно — пробный доступ выдаётся
    await dispatcher.feed_update(bot, make_update(callback_data="trial:start", user_id=8301))
    _, sub = await user_subscription(session, 8301)
    assert sub is not None and sub.status == "trial"


async def test_not_subscribed_yet_is_told_honestly(bot, dispatcher, session, gate_on):
    """Нажал «Я подписался», а подписки нет — говорим об этом, а не пускаем."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=8401))
    bot.session.clear()

    await dispatcher.feed_update(
        bot, make_update(callback_data=channel_gate.CALLBACK_CHECK, user_id=8401)
    )

    assert "Подписки пока не видно" in shown(bot)
    assert GATE_MARK in shown(bot), "экран подписки должен остаться на месте"


async def test_success_is_cached(bot, dispatcher, session, gate_on):
    """Успешную проверку запоминаем: иначе каждый апдейт — запрос к Telegram."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=8501))
    subscribe(bot, 8501)
    await dispatcher.feed_update(
        bot, make_update(callback_data=channel_gate.CALLBACK_CHECK, user_id=8501)
    )
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update("/start", user_id=8501))

    assert not bot.session.by_name("GetChatMember"), "проверку не закэшировали"
    assert GATE_MARK not in shown(bot)


async def test_second_tap_does_not_ask_telegram_again(bot, dispatcher, session, gate_on):
    """Человек ходит по экрану подписки: не дёргаем API на каждое нажатие."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=9301))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="plans", user_id=9301))

    assert GATE_MARK in shown(bot)
    assert not bot.session.by_name("GetChatMember"), "повторная проверка через секунду не нужна"


async def test_check_button_ignores_caches(bot, dispatcher, session, gate_on):
    """«Я подписался» проверяет заново, а не отдаёт недавнее «не подписан»."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=9401))
    subscribe(bot, 9401)
    bot.session.clear()

    await dispatcher.feed_update(
        bot, make_update(callback_data=channel_gate.CALLBACK_CHECK, user_id=9401)
    )

    assert bot.session.by_name("GetChatMember"), "кнопка обязана проверять заново"
    assert "Подписка подтверждена" in shown(bot)


async def test_restricted_member_counts_as_subscriber(bot, dispatcher, session, gate_on):
    """Человек в канале, но с ограничениями на запись — он всё равно подписан."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=9501))
    bot.session.chat_members[9501] = "restricted"
    bot.session.clear()

    await dispatcher.feed_update(
        bot, make_update(callback_data=channel_gate.CALLBACK_CHECK, user_id=9501)
    )

    assert "Подписка подтверждена" in shown(bot)


async def test_stale_check_button_with_gate_off_opens_menu(bot, dispatcher, session):
    """Кнопка осталась от старого сообщения, а гейт выключили — не пугаем сбоем."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=9601))
    bot.session.clear()

    await dispatcher.feed_update(
        bot, make_update(callback_data=channel_gate.CALLBACK_CHECK, user_id=9601)
    )

    assert "Не получилось проверить" not in shown(bot)
    assert _menu_shown(bot)


# ------------------------------------------------------- кого гейт не трогает
async def test_active_subscription_is_not_gated(bot, dispatcher, session, gate_on):
    """У кого доступ активен, гейт не мешает — даже если он не в канале."""
    user, _ = await subscriptions.get_or_create_user(session, tg_id=8601)
    await subscriptions.start_trial(session, user, await subscriptions.all_user_panels(session))
    await session.commit()
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update("/start", user_id=8601))

    assert GATE_MARK not in shown(bot)
    assert not bot.session.by_name("GetChatMember"), "активного клиента даже не проверяем"


async def test_admin_is_not_gated(bot, dispatcher, session, gate_on):
    """Админов гейт не касается: иначе владелец запрётся в своём же боте."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=1))

    assert GATE_MARK not in shown(bot)


async def test_support_works_under_gate(bot, dispatcher, session, gate_on):
    """Поддержка нужна именно тогда, когда ничего не выходит."""
    await dispatcher.feed_update(bot, make_update("/support", user_id=8701))

    assert GATE_MARK not in shown(bot)
    assert shown(bot), "поддержка не ответила"


async def test_referral_survives_the_gate(bot, dispatcher, session, gate_on):
    """Пригласившего записываем до гейта: бонус за друга не теряется."""
    inviter, _ = await subscriptions.get_or_create_user(session, tg_id=8800, username="inviter")
    await session.commit()

    await dispatcher.feed_update(
        bot, make_update(f"/start ref_{inviter.referral_code}", user_id=8801)
    )

    invited = await subscriptions.get_user_by_tg(session, 8801)
    assert invited is not None and invited.referred_by == inviter.id
    assert GATE_MARK in shown(bot)


async def test_referral_greeting_reaches_the_invited_under_the_gate(
    bot, dispatcher, session, gate_on
):
    """Друг видит обещанный подарок, даже если гейт не пустил его в бот.

    Пригласившего ``attach_referrer`` отдаёт только в момент привязки — на этом
    же ``/start``. Если показать приветствие после проверки подписки, его не
    увидит никто: повторный ``/start`` вернёт None.
    """
    inviter, _ = await subscriptions.get_or_create_user(session, tg_id=8810, username="inviter2")
    await session.commit()

    await dispatcher.feed_update(
        bot, make_update(f"/start ref_{inviter.referral_code}", user_id=8811)
    )

    assert "пригласил" in shown(bot), "приглашённый не увидел приветствие со скидкой"
    assert GATE_MARK in shown(bot)


async def test_pending_manual_order_button_works_under_the_gate(
    bot, dispatcher, session, gate_on
):
    """«Оплатил, но доступа нет» не упирается в экран подписки.

    Подписка могла кончиться, пока человек оплачивал по СБП (окно заказа —
    30 минут). Без исключения админ не узнавал бы об оплате: заказ остаётся
    ``pending``, а автоподтверждение по умолчанию выключено.
    """
    user, _ = await subscriptions.get_or_create_user(session, tg_id=8820)
    plan = next(p for p in await orders.list_plans(session) if p.code == "m1")
    order = await orders.create_order(session, user, plan, provider="sbp")
    await session.commit()
    bot.session.clear()

    await dispatcher.feed_update(
        bot, make_update(callback_data=f"order:manual:{order.id}", user_id=8820)
    )

    assert "отправлена на проверку" in shown(bot), "кнопка оплаты упёрлась в гейт"
    assert not bot.session.by_name("GetChatMember"), "заказ проверять подпиской не нужно"
    await session.refresh(order)
    assert order.status == "pending"
    assert "Новая заявка на оплату" in shown(bot), "админ не узнал об оплате"


async def test_pending_crypto_order_check_works_under_the_gate(
    bot, dispatcher, session, gate_on
):
    """«Проверить оплату» по крипте тоже обязана работать без подписки."""
    user, _ = await subscriptions.get_or_create_user(session, tg_id=8830)
    plan = next(p for p in await orders.list_plans(session) if p.code == "m1")
    order = await orders.create_order(session, user, plan, provider="crypto")
    await session.commit()
    bot.session.clear()

    await dispatcher.feed_update(
        bot, make_update(callback_data=f"order:check:{order.id}", user_id=8830)
    )

    assert GATE_MARK not in shown(bot), "гейт перекрыл проверку оплаченного заказа"
    assert not bot.session.by_name("GetChatMember")


async def test_stars_payment_is_not_blocked_by_gate(bot, dispatcher, session, gate_on):
    """Оплата звёздами приходит сообщением: гейт не должен её съесть."""
    user, _ = await subscriptions.get_or_create_user(session, tg_id=8901)
    plan = next(p for p in await orders.list_plans(session) if p.code == "m1")
    order = await orders.create_order(session, user, plan, provider="stars")
    await session.commit()

    await dispatcher.feed_update(
        bot, make_stars_payment_update(order.id, order.stars_amount, user_id=8901)
    )

    await session.refresh(order)
    assert order.status == "paid", "оплаченный заказ не подтвердился"
    _, sub = await user_subscription(session, 8901)
    assert sub is not None and sub.is_active
    assert f"/sub/{sub.subscription_token}" in shown(bot)


# ------------------------------------------------------- Telegram не отвечает
async def test_broken_gate_is_reported_on_a_fresh_host(
    bot, dispatcher, session, gate_on, monkeypatch
):
    """Хост поднят минуту назад: предупреждение админам всё равно уходит.

    Часы берём у ``time.monotonic()`` — на только что стартовавшем контейнере он
    меньше часа, и ``_alerted_at = 0.0`` съедал первое предупреждение целиком.
    """
    monkeypatch.setattr(channel_gate, "_alerted_at", None)
    monkeypatch.setattr(channel_gate.time, "monotonic", lambda: 60.0)
    telegram_silent(bot)

    await dispatcher.feed_update(bot, make_update("/start", user_id=9901))

    assert "не проверяется" in shown(bot), "админ так и не узнал, что гейт сломан"


async def test_broken_gate_warns_admins_only_once_an_hour(
    bot, dispatcher, session, gate_on, monkeypatch
):
    """Сломанный гейт не должен превращаться в рассылку админам на каждый апдейт."""
    monkeypatch.setattr(channel_gate, "_alerted_at", None)
    monkeypatch.setattr(channel_gate.time, "monotonic", lambda: 4000.0)
    telegram_silent(bot)

    def warnings() -> int:
        return sum(
            1
            for request in bot.session.by_name("SendMessage")
            if "не проверяется" in (getattr(request, "text", "") or "")
        )

    await dispatcher.feed_update(bot, make_update("/start", user_id=9902))
    assert warnings() == 1

    await dispatcher.feed_update(bot, make_update("/start", user_id=9903))
    assert warnings() == 1, "второе предупреждение ушло раньше часа ожидания"


async def test_api_failure_lets_people_in(bot, dispatcher, session, gate_on):
    """Сломанная проверка не должна закрывать бота всем сразу."""
    telegram_silent(bot)

    await dispatcher.feed_update(bot, make_update("/start", user_id=9001))

    assert GATE_MARK not in shown(bot)
    assert "пробн" in shown(bot).lower()


async def test_api_failure_blocks_when_fail_open_is_off(
    bot, dispatcher, session, gate_on, monkeypatch
):
    """Строгий режим: владелец решил, что лучше экран подписки, чем гейт-пустышка."""
    monkeypatch.setattr(gate_on, "channel_gate_fail_open", False)
    telegram_silent(bot)

    await dispatcher.feed_update(bot, make_update("/start", user_id=9101))

    assert GATE_MARK in shown(bot)


async def test_missing_user_in_chat_means_not_subscribed(bot, dispatcher, session, gate_on):
    """«user not found» — это ответ «не подписан», а не сбой проверки."""
    telegram_silent(bot, "Bad Request: user not found")

    await dispatcher.feed_update(bot, make_update("/start", user_id=9201))

    assert GATE_MARK in shown(bot)


async def test_repeated_failures_do_not_hammer_telegram(bot, dispatcher, session, gate_on):
    """Проверка сломана (бот не админ): не долбим API на каждом апдейте."""
    telegram_silent(bot)

    await dispatcher.feed_update(bot, make_update("/start", user_id=9701))
    assert len(bot.session.by_name("GetChatMember")) == 1

    await dispatcher.feed_update(bot, make_update("/start", user_id=9702))

    assert len(bot.session.by_name("GetChatMember")) == 1, "после сбоя нужна пауза"


async def test_check_button_tries_again_after_failure(bot, dispatcher, session, gate_on):
    """Кнопку жмёт живой человек: пауза после сбоя её не касается."""
    telegram_silent(bot)
    await dispatcher.feed_update(bot, make_update("/start", user_id=9801))
    bot.session.clear()

    await dispatcher.feed_update(
        bot, make_update(callback_data=channel_gate.CALLBACK_CHECK, user_id=9801)
    )

    assert bot.session.by_name("GetChatMember"), "по кнопке проверяем заново"


# ------------------------------------------------------------------ настройки
async def test_startup_check_catches_missing_admin_rights(bot, gate_on):
    """«Включил гейт, а бота в канал админом не добавил» — ловим на старте."""
    bot.session.chat_members[1] = "left"  # id бота в заглушке Telegram — 1

    ok, detail = await channel_gate.admin_check(bot)

    assert not ok and "администратор" in detail


async def test_startup_check_passes_for_channel_admin(bot, gate_on):
    bot.session.chat_members[1] = "administrator"

    ok, detail = await channel_gate.admin_check(bot)

    assert ok and "администратор" in detail


async def test_startup_check_reports_telegram_refusal(bot, gate_on):
    """Telegram отказывает («member list is inaccessible») — так и говорим."""
    telegram_silent(bot, "Bad Request: member list is inaccessible")

    ok, detail = await channel_gate.admin_check(bot)

    assert not ok and "member list is inaccessible" in detail


def test_channel_id_comes_from_public_link(monkeypatch):
    """Достаточно публичной ссылки: отдельный CHANNEL_ID не обязателен."""
    settings = get_settings()
    monkeypatch.setattr(settings, "channel_id", "")
    monkeypatch.setattr(settings, "channel_url", "https://t.me/KometaVPN888")

    assert settings.resolved_channel_id == "@KometaVPN888"
    assert settings.channel_link == "https://t.me/KometaVPN888"


def test_numeric_channel_id_is_kept_as_is(monkeypatch):
    """Приватный канал проверяем по числовому id: ссылку-приглашение не проверить."""
    settings = get_settings()
    monkeypatch.setattr(settings, "channel_id", "-1001234567890")
    monkeypatch.setattr(settings, "channel_url", "https://t.me/+AbCdEfGh")

    assert settings.resolved_channel_id == "-1001234567890"


def test_invite_link_without_id_disables_the_gate(monkeypatch):
    """Нечего проверять — гейт не включаем: лучше пустить всех, чем запереть."""
    settings = get_settings()
    monkeypatch.setattr(settings, "channel_id", "")
    monkeypatch.setattr(settings, "channel_url", "https://t.me/+AbCdEfGh")
    monkeypatch.setattr(settings, "channel_gate_enabled", True)

    assert settings.resolved_channel_id == ""
    assert not channel_gate.configured()
