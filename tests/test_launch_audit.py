"""Тесты гейта витрины: живое состояние против кода.

Сеть здесь не нужна: ``collect()`` читает Telegram и страницы, а ``audit()``
сравнивает готовый слепок. Поэтому проверяем именно сравнение — на слепках,
собранных руками, включая те расхождения, которые уже случались на проде.
"""

from __future__ import annotations

from app.bot import launch_copy
from app.tools import launch_audit
from app.tools.apply_launch_copy import COMMANDS


def _clean(**overrides) -> launch_audit.Snapshot:  # noqa: ANN003
    """Слепок, полностью совпадающий с кодом."""
    base: dict = {
        "bot_description": launch_copy.bot_description(),
        "bot_short_description": launch_copy.bot_short_description(),
        "bot_commands": tuple(COMMANDS),
        "channel_description": launch_copy.channel_description(),
        "channel_admin": True,
        "can_pin_messages": True,
        "channel_pinned": True,
        "legal_pages": {"https://example.test/privacy": "Оператор: Иван Иванов, ИНН 123456789012"},
        "legal_name": "Иван Иванов",
        "legal_inn": "123456789012",
    }
    base.update(overrides)
    return launch_audit.Snapshot(**base)


def _check(checks, name):  # noqa: ANN001
    found = [c for c in checks if c.name == name]
    assert found, f"нет пункта «{name}»"
    return found[0]


def test_clean_showcase_passes():
    """Совпадающий слепок не даёт ни одного замечания."""
    checks = launch_audit.audit(_clean())

    failed = [c.name for c in checks if not c.ok]
    assert failed == [], f"ложные срабатывания: {failed}"


def test_stale_bot_description_is_caught():
    """Живое расхождение 09.10.2026: бот обещал оплату картой, которой нет.

    В коде строка «Оплата: СБП, крипта, Telegram Stars», а в Telegram лежала
    старая версия со словом «карта». Ни один тест этого не видел, потому что
    тесты сравнивают код с кодом.
    """
    stale = launch_copy.bot_description().replace("Оплата: СБП, крипта", "Оплата: СБП, карта, крипта")
    assert stale != launch_copy.bot_description()

    check = _check(launch_audit.audit(_clean(bot_description=stale)), "описание бота")

    assert check.ok is False
    assert "apply_launch_copy" in check.fix


def test_stale_channel_description_is_caught():
    """Живое расхождение: в канале описание без документов и поддержки."""
    check = _check(
        launch_audit.audit(_clean(channel_description="Kometa VPN — защищённое подключение")),
        "описание канала",
    )

    assert check.ok is False
    assert "post_channel" in check.fix


def test_missing_pin_right_is_caught():
    """Без can_pin_messages закреп физически невозможен — это и проверяем."""
    check = _check(launch_audit.audit(_clean(can_pin_messages=False)), "право закреплять сообщения")

    assert check.ok is False
    assert "Закреплять сообщения" in check.fix


def test_missing_pinned_post_is_caught():
    """Закрепа нет — первый экран канала пуст."""
    check = _check(launch_audit.audit(_clean(channel_pinned=False)), "закреплённый пост в канале")

    assert check.ok is False


def test_placeholder_in_legal_page_is_caught():
    """Живое расхождение: клиент читал «[ИСПОЛНИТЕЛЬ], ИНН [ИНН]»."""
    page = "1.2. Оператор персональных данных: [ИСПОЛНИТЕЛЬ], ИНН [ИНН]"
    checks = launch_audit.audit(
        _clean(legal_pages={"https://example.test/privacy": page}, legal_name="", legal_inn="")
    )

    assert _check(checks, "документы без заглушек").ok is False
    assert _check(checks, "реквизиты исполнителя заполнены").ok is False


def test_empty_short_description_is_caught():
    """Короткое описание пусто — в профиле бота нечего прочитать."""
    check = _check(launch_audit.audit(_clean(bot_short_description="")), "короткое описание бота")

    assert check.ok is False


def test_render_says_not_accepted_and_shows_the_fix():
    """Отчёт обязан назвать провал и подсказать команду, а не просто «ок»."""
    snapshot = _clean(can_pin_messages=False, channel_pinned=False)
    checks = launch_audit.audit(snapshot)
    text = launch_audit.render(checks, [])

    assert "НЕ ПРИНЯТО" in text
    assert "провалено 2" in text
    assert "post_channel" in text


def test_render_says_accepted_on_clean_snapshot():
    text = launch_audit.render(launch_audit.audit(_clean()), [])

    assert "ПРИНЯТО" in text
    assert "НЕ ПРИНЯТО" not in text


def test_unreadable_state_counts_as_failure():
    """Что не удалось прочитать — тоже расхождение: молча пропускать нельзя."""
    text = launch_audit.render(launch_audit.audit(_clean()), ["канал: Chat not found"])

    assert "Не удалось прочитать" in text
    assert "канал: Chat not found" in text
