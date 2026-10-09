"""Тесты быстрого подключения: кнопки-приложения и копирование ссылки.

Смысл: клиент должен подключаться в один тап. Схемы приложений
(``happ://``, ``v2rayng://``, ``hiddify://``) **нельзя** ставить в inline-кнопки
Telegram — он отвечает «Bad Request: Unsupported URL protocol» и клавиатура не
уходит вовсе. Поэтому кнопки ведут на нашу https-страницу ``/connect/<token>``,
а она открывает приложение по схеме.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from app.bot import keyboards, texts
from tests.fakes import make_update

SUB = "http://203.0.113.10:8090/sub/abc123"


def button_urls(markup) -> dict[str, str]:
    return {b.text: b.url for row in markup.inline_keyboard for b in row if getattr(b, "url", None)}


def test_connect_kb_buttons_are_telegram_safe():
    """Кнопки обязаны быть http(s): иначе Telegram не отправит клавиатуру."""
    urls = button_urls(keyboards.connect_kb(SUB))

    for text, url in urls.items():
        assert url.startswith(("http://", "https://")), f"недопустимая схема в «{text}»: {url}"

    # ведём на страницу подключения и подсказываем, какое приложение открыть
    assert urls["Подключить в Happ"].endswith("/connect/abc123?app=happ")
    assert urls["Подключить в v2rayNG"].endswith("/connect/abc123?app=v2rayng")
    assert urls["Подключить в Hiddify"].endswith("/connect/abc123?app=hiddify")


def test_bot_keyboards_have_no_custom_url_schemes():
    """Предохранитель: схемы приложений живут только на веб-странице.

    Если кто-то снова положит ``happ://`` в inline-кнопку, бот начнёт падать
    на отправке сообщения («Unsupported URL protocol») — поэтому проверяем не
    текст модуля, а именно значения ``url=...`` в клавиатурах.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path(keyboards.__file__).read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "url":
            value = getattr(node.value, "value", "")
            if isinstance(value, str) and "://" in value and not value.startswith(("http://", "https://")):
                bad.append(value)

    assert bad == [], f"Telegram отклонит такие кнопки: {bad}"


def test_connect_kb_has_copy_button():
    markup = keyboards.connect_kb(SUB)
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]

    assert "sub:copy" in callbacks


def test_connect_kb_without_link_shows_only_menu():
    """Если ссылки нет, кривых кнопок быть не должно."""
    urls = button_urls(keyboards.connect_kb(""))

    assert urls == {}


async def test_trial_message_contains_connect_buttons(bot, dispatcher, session):
    await dispatcher.feed_update(bot, make_update("/start", user_id=8801))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="trial:start", user_id=8801))

    sent = bot.session.all_text()
    assert "Подключить в Happ" in sent
    # кнопка ведёт на https-страницу подключения, а не на схему приложения
    urls = bot.session.button_urls()
    assert urls and all(url.startswith(("http://", "https://")) for url in urls)


async def test_copy_callback_sends_plain_link(bot, dispatcher, session):
    await dispatcher.feed_update(bot, make_update("/start", user_id=8802))
    await dispatcher.feed_update(bot, make_update(callback_data="trial:start", user_id=8802))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="sub:copy", user_id=8802))

    sent = bot.session.all_text()
    assert "/sub/" in sent
    assert "Скопировать" in texts.SUBSCRIPTION_COPY or "скопировать" in sent.lower()


async def test_hint_mentions_two_steps(bot):
    """Текст должен объяснять путь: нажать кнопку → включить VPN."""
    text = texts.SUBSCRIPTION_LINK_HINT.format(link=SUB)

    assert "Нажми кнопку" in text
    assert "включи VPN" in text
    assert SUB in text


def test_connect_buttons_point_to_connect_page(monkeypatch):
    """Кнопки ведут на страницу подключения — она и открывает приложение."""
    urls = button_urls(keyboards.connect_kb(SUB))

    assert urls["Подключить в Happ"].endswith("/connect/abc123?app=happ")
    assert urls["Подключить в v2rayNG"].endswith("/connect/abc123?app=v2rayng")
    assert urls["Подключить в Hiddify"].endswith("/connect/abc123?app=hiddify")


def test_bot_shows_forever_subscription_without_huge_days_left():
    """У бессрочной подписки в карточке «бессрочно», а не «Осталось: 26000 дн.»."""
    from datetime import datetime, timedelta, timezone

    from app.bot.handlers.subscription import _is_forever

    assert _is_forever(datetime.now(timezone.utc) + timedelta(days=365 * 30)) is True
    assert _is_forever(datetime.now(timezone.utc) + timedelta(days=3)) is False
    assert _is_forever(None) is False
