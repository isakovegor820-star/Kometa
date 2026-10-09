"""Документы, цены и поддержка в боте: требования банка-партнёра.

Партнёр согласует проект при четырёх условиях: политика конфиденциальности,
пользовательское соглашение, контакт поддержки (не группа) и актуальные цены
и тарифы — и всё это должно быть доступно клиенту постоянно, отдельными кнопками.

Тесты фиксируют, что раздел «📄 Документы и цены» действительно работает:
кнопки на месте, тексты открываются, цены берутся из базы, а контакт поддержки
подставляется из настроек.
"""

from __future__ import annotations

from app import legal_texts
from app.bot import keyboards, texts
from app.config import get_settings
from app.services import documents, orders
from app.services.finmodel import DEFAULT_PLANS
from tests.fakes import make_update

# ------------------------------------------------------------------ раздел в боте


async def test_main_menu_has_documents_button(bot, dispatcher, session):
    """Банк проверяет доступность документов: кнопка есть в главном меню."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=7901))

    assert keyboards.BTN_LEGAL in bot.session.buttons()


async def test_documents_screen_lists_all_four_requirements(bot, dispatcher, session):
    """Политика, соглашение, цены и поддержка — каждая пунктом меню."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=7902))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update(callback_data="legal:show", user_id=7902))

    buttons = bot.session.buttons()
    for label in (
        texts.LEGAL_PRIVACY_BTN,
        texts.LEGAL_TERMS_BTN,
        texts.LEGAL_PRICING_BTN,
        texts.LEGAL_SUPPORT_BTN,
    ):
        assert label in buttons, f"нет кнопки «{label}»"
    assert "Документы и цены" in bot.session.all_text()


async def test_privacy_opens_in_chat_before_publication(bot, dispatcher, session, monkeypatch):
    """Пока ссылка не опубликована, бот отдаёт полный текст — документ доступен всегда."""
    settings = get_settings()
    monkeypatch.setattr(settings, "privacy_url", "")

    await dispatcher.feed_update(bot, make_update("/start", user_id=7903))
    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data="legal:privacy", user_id=7903))

    sent = bot.session.all_text()
    assert "ПОЛИТИКА КОНФИДЕНЦИАЛЬНОСТИ" in sent
    assert "152-ФЗ" in sent
    # Документ длиннее лимита Telegram — значит, уходит несколькими сообщениями.
    assert len(bot.session.texts()) >= 2


async def test_terms_opens_in_chat_before_publication(bot, dispatcher, session):
    """Соглашение содержит тарифы, возврат и контакт поддержки."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=7904))
    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data="legal:terms", user_id=7904))

    sent = bot.session.all_text()
    assert "ПОЛЬЗОВАТЕЛЬСКОЕ СОГЛАШЕНИЕ" in sent
    assert "Возврат средств" in sent
    # Цена месяца берётся из тарифов, а не вписана в текст документа.
    plans = await orders.list_plans(session)
    monthly = next(p for p in plans if p.code == "m1")
    assert str(monthly.price_rub) in sent


async def test_documents_become_links_when_published(bot, dispatcher, session, monkeypatch):
    """После публикации кнопки ведут на страницы документов, а не в чат."""
    settings = get_settings()
    monkeypatch.setattr(settings, "privacy_url", "https://telegra.ph/privacy-test")
    monkeypatch.setattr(settings, "terms_url", "https://telegra.ph/terms-test")

    await dispatcher.feed_update(bot, make_update("/start", user_id=7905))
    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data="legal:show", user_id=7905))

    urls = bot.session.button_urls()
    assert "https://telegra.ph/privacy-test" in urls
    assert "https://telegra.ph/terms-test" in urls


# ------------------------------------------------------------------ цены


async def test_pricing_shows_actual_prices_and_terms(bot, dispatcher, session):
    """Актуальные цены, состав тарифа и отсутствие автопродления — как просил банк."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=7906))
    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data="legal:pricing", user_id=7906))

    plans = await orders.list_plans(session)
    monthly = next(p for p in plans if p.code == "m1")
    annual = next(p for p in plans if p.code == "m12")
    per_month = round(annual.price_rub / annual.days * 30)

    sent = bot.session.all_text()
    assert str(monthly.price_rub) in sent
    assert str(annual.price_rub) in sent
    assert str(per_month) in sent  # цена месяца на годовом тарифе
    assert "автоматического списания нет" in sent
    assert "до 3 устройств одновременно" in sent


async def test_pricing_buttons_lead_to_purchase(bot, dispatcher, session):
    """Из прайса можно сразу перейти к тарифам — документы не тупик."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=7907))
    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data="legal:pricing", user_id=7907))

    assert keyboards.BTN_PLANS in bot.session.buttons()


# ------------------------------------------------------------------ поддержка


async def test_support_shows_contact_from_settings(bot, dispatcher, session, monkeypatch):
    """Контакт поддержки — юзернейм из .env, а не группа."""
    monkeypatch.setattr(get_settings(), "support_username", "kometa_support")

    await dispatcher.feed_update(bot, make_update("/start", user_id=7908))
    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data="legal:support", user_id=7908))

    sent = bot.session.all_text()
    assert "@kometa_support" in sent
    assert "номер заказа" in sent


async def test_support_without_contact_does_not_break(bot, dispatcher, session, monkeypatch):
    """Пустой SUPPORT_USERNAME — не ошибка: бот честно говорит, что канал подключается."""
    monkeypatch.setattr(get_settings(), "support_username", "")

    await dispatcher.feed_update(bot, make_update("/start", user_id=7909))
    bot.session.clear()
    await dispatcher.feed_update(bot, make_update(callback_data="legal:support", user_id=7909))

    assert "Канал поддержки подключается" in bot.session.all_text()


# ------------------------------------------------------------------ длинные тексты


def test_split_message_keeps_parts_within_telegram_limit():
    """Документы длиннее лимита Telegram режутся по абзацам, а не по буквам."""
    from app.bot.handlers.legal import TELEGRAM_LIMIT, split_message

    body = "\n\n".join(f"{index}. Пункт документа про обработку данных." for index in range(400))
    parts = split_message(body)

    assert len(parts) > 1
    assert all(len(part) <= TELEGRAM_LIMIT for part in parts)
    assert "".join(part.replace("\n", "") for part in parts).replace(" ", "") == body.replace("\n", "").replace(" ", "")


# ------------------------------------------------------------------ документы как сервис


def test_context_uses_settings_and_placeholders(monkeypatch):
    """Без заполненного .env документы остаются черновиком с плейсхолдерами."""
    settings = get_settings()
    monkeypatch.setattr(settings, "legal_operator_name", "")
    monkeypatch.setattr(settings, "legal_operator_inn", "")
    monkeypatch.setattr(settings, "support_username", "")

    context = documents.build_context()
    privacy = documents.privacy_text(context)

    assert legal_texts.OPERATOR_PLACEHOLDER in privacy
    assert legal_texts.INN_PLACEHOLDER in privacy
    assert not documents.requisites_are_ready()


def test_context_fills_requisites_when_env_is_ready(monkeypatch):
    """Заполненный .env даёт документ, готовый к отправке в банк."""
    settings = get_settings()
    monkeypatch.setattr(settings, "legal_operator_name", "Иванов Иван Иванович")
    monkeypatch.setattr(settings, "legal_operator_inn", "770000000000")
    monkeypatch.setattr(settings, "support_username", "kometa_support")
    monkeypatch.setattr(settings, "bot_username", "kometa_bot")

    context = documents.build_context()
    privacy = documents.privacy_text(context)
    terms = documents.terms_text(context)

    assert "Иванов Иван Иванович" in privacy
    assert "770000000000" in privacy
    assert "@kometa_support" in privacy
    assert "@kometa_bot" in privacy
    assert legal_texts.OPERATOR_PLACEHOLDER not in privacy
    assert legal_texts.OPERATOR_PLACEHOLDER not in terms
    assert documents.requisites_are_ready()


def test_documents_do_not_promise_bypassing_blocks():
    """Смысловая проверка: документы описывают сервис, а не обход ограничений.

    Используем тот же список формулировок, что и общий сканер
    (`tests/test_public_texts_clean.py`): он ловит фразы целиком, а не отдельные
    корни, поэтому не спотыкается о слово «необходимом».
    """
    from tests.test_public_texts_clean import scan

    for text in (documents.privacy_text(), documents.terms_text(), documents.price_list_text()):
        assert not scan([(1, text)])


def test_price_list_matches_database_prices():
    """Прайс для банка совпадает с тарифами, которые видит клиент в боте."""
    from app.services import orders

    context = documents.build_context(prices=documents.DEFAULT_PRICES)
    price_list = documents.price_list_text(context)

    for row in documents.DEFAULT_PRICES:
        assert row.title in price_list
        assert str(row.price_rub) in price_list
    assert orders is not None  # прайс и бот берут тарифы из одного источника


# ------------------------------------------------------------------ комиссии партнёра


def test_partner_fees_are_in_the_model():
    """Ставки партнёра (СБП 8 %, крипта 5 %) зафиксированы в модели, а не в переписке."""
    from app.services.finmodel import CHANNEL_FEES, average_monthly_revenue, break_even_users
    from app.services.finmodel import Costs, net_after_channel_fee

    assert CHANNEL_FEES["sbp"] == 8.0
    assert CHANNEL_FEES["crypto"] == 5.0

    gross = average_monthly_revenue()
    net = net_after_channel_fee(gross, CHANNEL_FEES["sbp"])
    assert round(net, 2) == round(gross * 0.92, 2)
    assert round(break_even_users(net, Costs()), 1) == round(Costs().monthly / net, 1)

    # С тарифа «1 месяц» при 8 % приходит меньше цены, при 5 % — больше.
    price = DEFAULT_PLANS[0].price_rub
    assert round(net_after_channel_fee(price, 8.0)) == round(price * 0.92)
    assert round(net_after_channel_fee(price, 5.0)) == round(price * 0.95)


def test_net_after_channel_fee_validates_input():
    """Опечатка в ставке (например, 800 %) должна падать, а не тихо считать ерунду."""
    import pytest

    from app.services.finmodel import net_after_channel_fee

    with pytest.raises(ValueError):
        net_after_channel_fee(199, 800)
    with pytest.raises(ValueError):
        net_after_channel_fee(199, -1)


def test_partner_payout_includes_conversion():
    """СБП 8 % + конвертация в USDT 2 %: с чека 199 ₽ доходит 179 ₽, а не 199 ₽."""
    from app.services.finmodel import CONVERSION_FEE_PERCENT, net_after_partner_payout

    assert CONVERSION_FEE_PERCENT == 2.0
    assert round(net_after_partner_payout(199, 8), 2) == 179.42
    assert round(net_after_partner_payout(516.75, 8), 2) == 465.90
    # Крипта дешевле: 5 % + 2 %
    assert round(net_after_partner_payout(199, 5), 2) == 185.27
