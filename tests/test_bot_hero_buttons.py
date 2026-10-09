"""Кнопки под hero-экраном должны открывать экраны.

Боевой случай 09.10.2026: главный экран отправляется **фото с подписью**, а
хендлеры правили его через ``edit_text``. Telegram отвечает на это
``Bad Request: there is no text in the message to edit`` — кнопки «Пригласить
друга», «Профиль и подписка», «Помощь», «Тарифы» выглядели мёртвыми, а в лог
уходили пять ошибок подряд.

Заглушка Telegram (``tests/fakes.py``) раньше такое пропускала: она принимала
любой ``EditMessageText``. Теперь она ведёт себя как настоящий API — фото-текст
не редактируется, а повторная правка тем же содержимым даёт «message is not
modified». Эти тесты проверяют, что бот на это не спотыкается.
"""

from __future__ import annotations

import pytest

from tests.fakes import make_photo_callback_update, make_update

#: Кнопки главного меню, которые перерисовывают экран на месте.
SCREENS_IN_PLACE = ("trial:start", "plans", "profile:show", "ref:show", "help")
#: Кнопки-инструкции: они по замыслу приходят новым сообщением.
SCREENS_AS_NEW_MESSAGE = ("sub:howto", "sub:reserve")


@pytest.mark.parametrize("callback", SCREENS_IN_PLACE)
async def test_menu_button_opens_screen_from_hero_photo(bot, dispatcher, session, callback):
    """Каждая кнопка под фото обязана перерисовать экран, а не упасть."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=9200))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_photo_callback_update(callback, user_id=9200))

    assert bot.session.by_name("EditMessageCaption"), (
        f"кнопка {callback} не открыла экран — похоже, снова правим текст фото-сообщения"
    )
    assert not bot.session.by_name("EditMessageText"), (
        f"кнопка {callback} правит текст фото-сообщения — Telegram это запрещает"
    )


@pytest.mark.parametrize("callback", SCREENS_AS_NEW_MESSAGE)
async def test_instruction_button_sends_new_message(bot, dispatcher, session, callback):
    """Инструкции приходят отдельным сообщением — и тоже не должны падать."""
    await dispatcher.feed_update(bot, make_update("/start", user_id=9210))
    bot.session.clear()

    await dispatcher.feed_update(bot, make_photo_callback_update(callback, user_id=9210))

    assert bot.session.by_name("SendMessage"), f"кнопка {callback} ничего не показала"
    assert not bot.session.by_name("EditMessageText")


async def test_hero_screen_button_pressed_twice_does_not_break(bot, dispatcher, session):
    """Повторное нажатие той же кнопки: Telegram отвечает «message is not modified».

    Это не ошибка — экран уже такой. Раньше исключение уходило в обработчик
    ошибок и админам падало уведомление (первый скриншот владельца, 00:43).
    """
    await dispatcher.feed_update(bot, make_update("/start", user_id=9201))
    bot.session.clear()

    first = make_photo_callback_update("help", user_id=9201)
    await dispatcher.feed_update(bot, first)
    # То же самое нажатие ещё раз — содержимое и клавиатура не меняются.
    await dispatcher.feed_update(bot, make_photo_callback_update("help", user_id=9201))

    assert len(bot.session.by_name("EditMessageCaption")) == 2


async def test_gift_screen_opens_from_hero_photo_for_active_client(bot, dispatcher, session, panel):
    """«Подарить подписку» — кнопка меню действующего клиента, тоже на фото."""
    from app.services import orders, subscriptions

    await dispatcher.feed_update(bot, make_update("/start", user_id=9220))
    user = await subscriptions.get_user_by_tg(session, 9220)
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="manual")
    await orders.mark_paid(session, order, panel)
    await session.commit()
    bot.session.clear()

    await dispatcher.feed_update(bot, make_photo_callback_update("gift:show", user_id=9220))

    assert bot.session.by_name("EditMessageCaption"), "экран подарка не открылся из фото-сообщения"
    assert not bot.session.by_name("EditMessageText")
