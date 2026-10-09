"""Публикация поста в канал: текст, кнопка, сухой прогон, описание канала.

Почему тест, а не аккуратность. Пост уходит в публичный канал живым людям, и
«отправил не то» откатывается только вручную: удалить сообщение можно, вернуть
прочитавшим — нет. Поэтому проверяем ровно те места, где ошибка стоит дорого:

* текст проходит санитайзер публичных формулировок (ч. 18 ст. 14.3 КоАП);
* цены, пробный доступ и «карта не нужна» на месте — и совпадают с первым постом;
* старые обещания («2 локации», «и ТВ») не возвращаются;
* без ``--apply`` инструмент не обращается к Telegram вообще;
* описание канала проверяется на расхождение с текстами, а не «на глаз».
"""

from __future__ import annotations

import pytest

from app.tools import post_channel
from tests.test_launch_copy import STALE_PROMISES, _plain
from tests.test_public_texts_clean import scan


def _scan_text(text: str) -> list[str]:
    return scan([(number, line) for number, line in enumerate(text.splitlines(), 1)])


def test_offer_post_is_clean_for_sanitizer():
    """Пост не содержит запрещённых публичных формулировок."""
    hits = _scan_text(post_channel.channel_post("offer"))
    assert not hits, f"санитайзер публичных текстов: {hits}"


def test_offer_post_has_prices_trial_and_contacts():
    """В посте есть цена, пробный доступ, «карта не нужна», ссылка и поддержка."""
    text = _plain(post_channel.channel_post("offer"))
    for price in ("120", "299", "539", "959"):
        assert price in text, f"в посте нет цены {price} ₽"
    assert "бесплатно" in text
    assert "карта не нужна" in text
    assert "t.me/" in text
    assert "@" in text


def test_both_posts_agree_on_prices():
    """Цены в коротком посте — те же, что в первом: иначе спор с клиентом."""
    offer = post_channel.channel_post("offer")
    first = post_channel.channel_post("first")
    for price in ("120 ₽", "299 ₽", "539 ₽", "959 ₽"):
        assert price in offer and price in first


def test_posts_do_not_return_stale_promises():
    """Ни «2 локации», ни «и ТВ», ни «месяц бесплатно» в постах не возвращаются."""
    haystack = " ".join(
        _plain(post_channel.channel_post(kind)) for kind in sorted(post_channel.POSTS)
    ).lower()
    for phrase, why in STALE_PROMISES:
        assert phrase.lower() not in haystack, f"вернулось «{phrase}»: {why}"


def test_unknown_post_name_is_a_clear_error():
    """Опечатка в имени поста не должна отправлять в канал пустое сообщение."""
    with pytest.raises(SystemExit):
        post_channel.channel_post("offerr")


def test_lint_flags_empty_and_overlong_text():
    """Пустой текст и текст длиннее лимита Telegram не публикуем."""
    assert post_channel.lint_post("   ") == ["пустой текст"]
    problems = post_channel.lint_post("я" * (post_channel.POST_LIMIT + 1))
    assert any("лимит" in problem for problem in problems)


def test_dry_run_does_not_touch_telegram(monkeypatch):
    """Без --apply инструмент ничего не отправляет.

    Подменяем отправку на исключение: если сухой прогон дойдёт до сети, тест
    упадёт — «посмотрел, что уйдёт» и «уже ушло» не должны быть одним действием.
    """

    async def boom(**_kwargs):  # pragma: no cover - вызывается только при ошибке
        raise AssertionError("сухой прогон не должен обращаться к Telegram")

    monkeypatch.setattr(post_channel, "_apply", boom)
    monkeypatch.setattr(post_channel, "_check", boom)
    assert post_channel.main(["--text", "offer"]) == 0


def test_file_post_strips_code_fences(tmp_path):
    """Текст из docs копируется с забором ``` — забор в канал не уходит."""
    file = tmp_path / "post.txt"
    file.write_text("```\n💎 Тестовый пост\n\nСтрока вторая\n```\n", encoding="utf-8")
    assert post_channel.read_post_file(file) == "💎 Тестовый пост\n\nСтрока вторая"


def test_missing_file_is_a_clear_error(tmp_path):
    """Файла нет — говорим об этом, а не отправляем то, что было в --text."""
    with pytest.raises(SystemExit):
        post_channel.read_post_file(tmp_path / "nope.txt")


def test_dry_run_with_file_and_long_text(tmp_path):
    """Свой текст файлом: длиннее лимита — отказ, нормальный — сухой прогон."""
    good = tmp_path / "good.txt"
    good.write_text("🏆 Конкурс месяца: приведи больше всех\n\nИтоги — 31 октября.", encoding="utf-8")
    assert post_channel.main(["--file", str(good)]) == 0

    bad = tmp_path / "bad.txt"
    bad.write_text("я" * (post_channel.POST_LIMIT + 5), encoding="utf-8")
    assert post_channel.main(["--file", str(bad)]) == 1


def test_message_link_for_public_and_private_channel():
    """Ссылка на отправленный пост — чтобы было что открыть и проверить."""
    assert post_channel.message_link("@KometaVPN888", 42) == "https://t.me/KometaVPN888/42"
    assert post_channel.message_link("-1004314256186", 42) == "https://t.me/c/4314256186/42"


def test_description_check_catches_stale_channel_description():
    """Живой случай 08.10.2026: в канале «2 локации» без названий, работают три."""
    stale = (
        "Сервис защищённого подключения Kometa: новости, статус, инструкции и поддержка. "
        "Безлимит, до 3 устройств, 2 локации. Первые 3 дня бесплатно, тарифы от 120 ₽. "
        "Документы и цены — в боте: t.me/kometavpnservise_bot"
    )
    problems = post_channel.description_problems(stale)
    assert any("локаци" in problem for problem in problems), problems


def test_description_check_is_quiet_on_current_text():
    """Актуальное описание канала проходит проверку без замечаний."""
    from app.bot import launch_copy

    assert post_channel.description_problems(launch_copy.channel_description()) == []


def test_location_words_come_from_settings():
    """Названия локаций берём из launch_copy, а не вписываем руками в проверку."""
    assert "Германия" in post_channel.location_words()
    assert all("🇩🇪" not in word for word in post_channel.location_words())


def test_pin_problem_tells_what_to_do():
    """Нет права закреплять — сообщение должно называть право и путь в Telegram.

    09.10.2026 бот был администратором канала без ``can_pin_messages``, и
    ``--apply --pin`` падал уже ПОСЛЕ отправки поста: выглядело как «ничего не
    вышло», хотя пост ушёл.
    """
    text = post_channel.pin_problem_text(RuntimeError("not enough rights"))

    assert "can_pin_messages" in text
    assert "Закреплять сообщения" in text
    assert "not enough rights" in text
    assert "--apply --pin" in text
