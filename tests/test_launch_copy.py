"""Тексты «до старта»: лимиты Telegram, факты из настроек, отсутствие вранья.

Зачем тест, а не памятка. Описание бота и описание канала живут вне кода —
в BotFather и в настройках канала, — поэтому расходятся с реальностью молча.
08.10.2026 в обоих стояли «2 локации: Германия и Нидерланды», а на сервере
работали три: клиент читал одно, получал другое. BotFather лимиты не проверяет
вовсе: текст длиннее 512 знаков просто обрезается, и обещание «вернём деньги»
может остаться за кадром.

Проверяем ровно то, что обещано в ``docs/КАНАЛ.md`` и ``docs/ЖЁСТКОЕ-РЕВЬЮ-ТЕКСТОВ.md``:

* лимиты: описание бота ≤ 512, «о боте» ≤ 120, описание канала ≤ 255;
* обязательные факты: локации, цена, пробный доступ, контакт поддержки;
* числа берутся из настроек (``TRIAL_DAYS``, ``LOCATION_LIST``), а не вписаны
  руками — иначе правка ``.env`` не доедет до описания;
* обещаний, которых нет в коде, в текстах нет: «ТВ», «2 локации», «безлимит
  устройств», «месяц бесплатно».
"""

from __future__ import annotations

import re

import pytest

from app.bot import launch_copy

#: Обещания, которые уже были неправдой или станут ею. Появится снова — тест упадёт.
STALE_PROMISES: tuple[tuple[str, str], ...] = (
    ("2 локации", "локаций три: 🇩🇪 🇫🇮 🇳🇱 — в описании стояло «2 локации»"),
    ("две локации", "то же самое словами"),
    ("и ТВ", "приложения для Apple TV у нас нет: обещание возвращается в поддержку"),
    ("Apple TV", "то же самое"),
    ("без тормозов даже вечером", "обещание без доказательства"),
    ("месяц бесплатно", "пробный доступ — 3 дня, а не месяц"),
    ("неограниченное число устройств", "у тарифов лимит 3 устройства"),
)


def _plain(text: str) -> str:
    """Текст без HTML-разметки — так его видит человек."""
    return re.sub("<[^>]+>", "", text)


def _reload_settings(monkeypatch, **env: str):
    """Подменить поля настроек на время теста — без сброса кэша.

    ``get_settings`` кэширован на процесс (``lru_cache``), поэтому «поменять
    окружение и перечитать настройки» кажется очевидным. Но ``cache_clear()``
    создаёт **второй** объект настроек: другие файлы тестов держат ссылку на
    первый (``settings = get_settings()`` на уровне модуля, то есть в момент
    сбора тестов) и подменяют поля именно у него. После сброса приложение
    читает новый объект — с пустыми платёжными доступами из ``conftest``, —
    и ``tests/test_wata.py`` падает с 503 «провайдер не настроен» просто
    потому, что шёл в файле позже (найдено 08.10.2026).

    Поэтому правим поля у того же самого объекта: и текст видит подмену, и
    чужие ссылки остаются валидными. ``monkeypatch`` вернёт значения после теста.
    """
    from app.config import get_settings

    settings = get_settings()
    for name, value in env.items():
        field = name.lower()
        current = getattr(settings, field, None)
        monkeypatch.setattr(settings, field, int(value) if isinstance(current, int) else value)
    return settings


def test_lint_is_clean():
    """Тексты проходят собственную проверку инструмента — лимиты и факты."""
    report = launch_copy.lint()
    assert report.ok, report.as_text()


def test_limits_match_telegram():
    """Лимиты в коде совпадают с лимитами BotFather.

    Если Telegram поменяет правила, менять надо одно место — и тест напомнит,
    что рядом с ним живут ещё доки и инструмент.
    """
    assert launch_copy.DESCRIPTION_LIMIT == 512
    assert launch_copy.SHORT_DESCRIPTION_LIMIT == 120
    assert launch_copy.CHANNEL_DESCRIPTION_LIMIT == 255


def test_description_mentions_price_before_start():
    """Цена видна ДО нажатия «Начать» — это первое возражение человека."""
    text = _plain(launch_copy.bot_description())
    assert "₽" in text
    assert "в месяц" in text


def test_description_mentions_trial_without_card():
    """Пробный доступ и «карта не нужна» — снятие главного риска."""
    text = _plain(launch_copy.bot_description())
    assert "бесплатно" in text
    assert "карта не нужна" in text
    assert "автосписаний нет" in text


def test_description_lists_every_location():
    """В описании перечислены ВСЕ локации, а не «несколько».

    Это и был инцидент 08.10.2026: в описании стояли две, работали три.
    """
    text = _plain(launch_copy.bot_description())
    for country in ("Германия", "Финляндия", "Нидерланды"):
        assert country in text, f"в описании бота нет локации {country}"


def test_locations_come_from_settings(monkeypatch):
    """Список локаций можно переопределить в .env — текст едет за настройкой."""
    _reload_settings(monkeypatch, LOCATION_LIST="🇰🇿 Казахстан")
    assert "Казахстан" in launch_copy.locations()


def test_trial_days_come_from_settings(monkeypatch):
    """Срок пробного доступа берётся из TRIAL_DAYS, а не вписан руками."""
    _reload_settings(monkeypatch, TRIAL_DAYS="7")
    text = _plain(launch_copy.bot_description())
    assert "Первые 7 дней" in text


@pytest.mark.parametrize(
    "days,expected",
    [(1, "Первый 1 день бесплатно"), (3, "Первые 3 дня бесплатно"), (7, "Первые 7 дней бесплатно")],
)
def test_trial_phrase_agrees_with_number(monkeypatch, days, expected):
    """Русский согласуется: «Первые 3 дня», а не «Первый 3 дня».

    Ошибка здесь стоит последней строкой в описании — то есть читается лучше
    всего остального текста.
    """
    _reload_settings(monkeypatch, TRIAL_DAYS=str(days))
    assert launch_copy.trial_phrase() == expected


def test_channel_description_has_documents_and_support():
    """Банк смотрит публичную точку входа: документы, цены, поддержка — в описании."""
    text = _plain(launch_copy.channel_description())
    assert "Документы и цены" in text
    assert "@" in text, "нет ссылки на бота или контакта поддержки"
    assert "₽" in text


def test_channel_description_fits_limit():
    """248 знаков из 255 — запас есть, но не «до обрезки»."""
    text = launch_copy.channel_description()
    assert len(text) <= launch_copy.CHANNEL_DESCRIPTION_LIMIT
    assert launch_copy.CHANNEL_DESCRIPTION_LIMIT - len(text) < 40


def test_no_stale_promises_in_launch_texts():
    """Старые обещания не возвращаются: ни в описании, ни в канале, ни в посте."""
    haystack = " ".join(
        _plain(text)
        for text in (
            launch_copy.bot_description(),
            launch_copy.bot_short_description(),
            launch_copy.channel_description(),
            launch_copy.channel_first_post(),
        )
    ).lower()
    for phrase, why in STALE_PROMISES:
        assert phrase.lower() not in haystack, f"вернулось «{phrase}»: {why}"


def test_channel_post_prices_match_plans():
    """Цены в первом посте — те же, что в базе тарифов.

    Расхождение цен в публичном посте и в боте — прямой путь к спору с клиентом
    и к возврату: он видел 120 ₽, а в боте 199 ₽.
    """
    post = launch_copy.channel_first_post()
    for price in ("120", "299", "539", "959"):
        assert price in post, f"в посте нет цены {price} ₽"


def test_first_post_has_working_link_and_support():
    """В посте есть ссылка на бота и контакт поддержки."""
    post = launch_copy.channel_first_post()
    assert "t.me/" in post
    assert "@" in post
