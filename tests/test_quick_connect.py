"""Тесты быстрого подключения: диплинки приложений и копирование ссылки.

Смысл: клиент должен подключаться в один тап, а не искать, куда вставлять ссылку.
Диплинки сверены с документацией клиентов (Happ, v2rayNG, Hiddify).
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from app.bot import keyboards, texts
from tests.fakes import make_update

SUB = "http://203.0.113.10:8090/sub/abc123"


def button_urls(markup) -> dict[str, str]:
    return {b.text: b.url for row in markup.inline_keyboard for b in row if getattr(b, "url", None)}


def test_connect_kb_has_deeplinks_for_three_apps():
    urls = button_urls(keyboards.connect_kb(SUB))

    assert urls["🟢 Подключить в Happ"] == f"happ://add/{SUB}"
    assert urls["🟣 Подключить в Hiddify"] == f"hiddify://install-config/?url={SUB}"

    v2rayng = urls["🔵 Подключить в v2rayNG"]
    assert v2rayng.startswith("v2rayng://install-sub/?url=")
    # ссылка закодирована, а имя профиля добавлено через %23 (#)
    parsed = urlparse(v2rayng.replace("v2rayng://install-sub/?", "https://x/?"))
    query = parse_qs(parsed.query)
    assert query["url"][0].endswith("#Kometa")
    assert query["url"][0].startswith("http://203.0.113.10:8090/sub/abc123")


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
    assert "happ://add/" in str(bot.session.requests) or "Подключить в Happ" in str(bot.session.requests)


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
