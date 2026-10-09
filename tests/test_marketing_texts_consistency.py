"""Тексты бота должны совпадать с тем, что реально настроено.

Зачем отдельный тест. Условия программы живут в `.env`, а формулировки — в
`app/bot/texts.py`. Когда 08.10.2026 условия поменяли (скидка 50 % → 30 %,
награда 30 дней → 14 + 14 за продление), заголовок экрана ещё обещал «месяц
бесплатно» — то есть клиент видел не то, что получал. Такой рассинхрон дороже
любой ошибки в коде: он бьёт по доверию и приводит людей в поддержку.

Тест ловит это автоматически: проверяет, что ключевые обещания берутся из
настроек, а не вписаны руками.
"""

from __future__ import annotations

import re

from app.bot import texts
from app.config import get_settings
from app.services import gift

settings = get_settings()


def render_referral(**overrides) -> str:
    """Собрать экран «Пригласить друга» так, как это делает бот."""
    params = {
        "percent": settings.referral_discount_percent,
        "referrer_days": settings.referral_bonus_days_referrer,
        "renewal_days": settings.referral_bonus_days_renewal,
        "invited_days": settings.referral_bonus_days_invited,
        "max_rewards": settings.referral_max_rewards_per_month,
        "link": "https://t.me/bot?start=ref_AB12CD34",
        "code": "KOMETA-AB12CD34",
        "balance_line": "",
        "invited": 2,
        "paid": 1,
        "earned_days": 14,
    }
    params.update(overrides)
    return texts.REFERRAL.format(**params)


def plain(text: str) -> str:
    """Текст без разметки — так его видит человек в Telegram."""
    return re.sub("<[^>]+>", "", text)


def test_referral_headline_matches_settings():
    """Заголовок обещает ровно ту награду, которая настроена."""
    text = plain(render_referral())

    assert f"Пригласи друга — {settings.referral_bonus_days_referrer} дней тебе" in text
    assert f"скидку {settings.referral_discount_percent}%" in text


def test_referral_screen_mentions_renewal_bonus():
    """Про награду за продление человек узнаёт из экрана, а не от поддержки."""
    text = plain(render_referral())

    assert f"+{settings.referral_bonus_days_renewal} дней" in text
    assert "продлен" in text or "продлевает" in text


def test_referral_screen_has_no_stale_promises():
    """Старые формулировки не должны вернуться при правках текста."""
    text = plain(render_referral()).lower()

    # «Месяц бесплатно» было верно при награде 30 дней; при 14 днях это обман.
    assert "месяц бесплатно" not in text
    assert "скидку 50" not in text
    assert "+30 дней" not in text


def test_greeting_shows_configured_discount_and_bonus():
    """Приветствие приглашённому тоже считается из настроек."""
    text = plain(
        texts.REFERRAL_GREETING.format(
            referrer="Аня",
            percent=settings.referral_discount_percent,
            examples="• 1 месяц — 120 ₽ → 84 ₽",
            invited_days=settings.referral_bonus_days_invited,
            code="KOMETA-AB12CD34",
        )
    )

    assert f"Скидка {settings.referral_discount_percent}%" in text
    assert f"+{settings.referral_bonus_days_invited} дн." in text
    # Пригласивший не должен «терять род»: экран говорит о нём в мужском роде
    # («у него»), а имя может быть женским. Формулировка обязана быть нейтральной.
    assert "у него для тебя подарок" not in text


def test_menu_button_does_not_promise_a_month():
    """Кнопка в меню не обещает месяц, если награда меньше."""
    from app.bot import keyboards

    label = keyboards.BTN_REFERRAL
    assert "месяц" not in label.lower()
    assert "пригласить" in label.lower()


def test_gift_certificate_text_matches_settings():
    """Открытка подарка называет реальные сроки и бонус покупателю."""
    text = gift.certificate_text(
        "https://t.me/bot?start=gift_KOMETA-GIFT-AB12CD34",
        "KOMETA-GIFT-AB12CD34",
        "1 месяц",
    )

    assert f"{settings.gift_valid_days} дней" in text
    assert f"+{settings.gift_buyer_bonus_days} дней" in text
    assert "KOMETA-GIFT-AB12CD34" in text


def test_lifecycle_texts_match_settings():
    """Автосценарии не обещают цифр, которых нет в настройках."""
    from app.services import lifecycle

    by_kind = {scenario.kind: scenario.text for scenario in lifecycle.scenarios()}
    referral_text = by_kind[lifecycle.KIND_REFERRAL]

    assert f"{settings.referral_discount_percent} %" in referral_text
    assert f"{settings.referral_bonus_days_referrer} дней" in referral_text
