"""Подарочные сертификаты: покупка в подарок с активацией позже.

Проверяем обещания, которые видит клиент:
  * цена подарка выше обычной подписки (это не скидка, а отдельный продукт);
  * сертификат живёт долго и активируется в любой момент;
  * покупатель не может активировать свой же подарок;
  * один сертификат нельзя «погасить» дважды;
  * покупателю капают дни, когда подарок активировали.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.db.models import Order, User
from app.services import gift, orders, subscriptions
from tests.fakes import make_update

settings = get_settings()


async def make_user(session, tg_id: int = 111) -> User:
    user, _ = await subscriptions.get_or_create_user(
        session, tg_id=tg_id, username=f"user{tg_id}", first_name=f"User{tg_id}"
    )
    await session.flush()
    return user


async def first_plan(session):
    return (await orders.list_plans(session))[0]


async def make_gift_order(session, buyer: User, *, message: str = "") -> Order:
    """Оплаченный подарочный заказ — как после реальной покупки."""
    plan = await first_plan(session)
    order = await orders.create_order(
        session, buyer, plan, provider="manual", kind=orders.KIND_GIFT, with_discount=False
    )
    order.amount_rub = gift.gift_price_rub(plan.price_rub)
    order.base_amount_rub = order.amount_rub
    await gift.attach_gift(session, order, message=message)
    order.status = "paid"
    await session.flush()
    return order


# ------------------------------------------------------------------ цена и токен
def test_gift_costs_more_than_regular_subscription():
    """Наценка за подарок: подписка 120 ₽ → сертификат 150 ₽."""
    assert gift.gift_price_rub(120) == 120 + round(120 * settings.gift_markup_percent / 100)
    assert gift.gift_price_rub(120) > 120
    assert gift.gift_price_stars(110) > 110


def test_gift_token_looks_like_a_code():
    token = gift.new_gift_token()
    assert token.startswith(gift.TOKEN_PREFIX)
    assert len(token) == len(gift.TOKEN_PREFIX) + 8
    # Без похожих символов: код диктуют голосом и переписывают руками.
    assert not set("01ILO") & set(token[len(gift.TOKEN_PREFIX):])


def test_token_normalization_is_forgiving():
    """Код принимается в любом виде: с префиксом, без него, в нижнем регистре."""
    token = gift.new_gift_token()
    body = token[len(gift.TOKEN_PREFIX):]
    assert gift.normalize_token(token.lower()) == token
    assert gift.normalize_token(body) == token
    assert gift.normalize_token(f"gift-{body.lower()}") == token
    assert gift.normalize_token(f"  {body}  ") == token
    assert gift.normalize_token("") == ""


def test_gift_validity_window_follows_settings():
    from datetime import datetime, timezone

    bought = datetime(2026, 10, 8, tzinfo=timezone.utc)
    expires = gift.gift_expires_at(bought)
    assert (expires - bought).days == settings.gift_valid_days


# ------------------------------------------------------------------ активация
async def test_gift_gives_days_to_balance_without_subscription(session, panel):
    """Получатель без подписки: дни в запас и применятся к первой оплате."""
    buyer = await make_user(session, 5001)
    recipient = await make_user(session, 5002)
    order = await make_gift_order(session, buyer, message="С праздником!")

    activation = await gift.redeem(session, order, recipient, panel)

    plan = await first_plan(session)
    assert activation.days == plan.days
    assert recipient.bonus_days_balance == plan.days
    assert activation.applied_to_active_subscription is False
    assert order.gift_activated_at is not None
    assert order.gift_activated_by == recipient.id


async def test_gift_extends_active_subscription(session, panel):
    """У получателя есть подписка — срок продлевается сразу."""
    buyer = await make_user(session, 5101)
    recipient = await make_user(session, 5102)
    sub, _ = await subscriptions.start_trial(session, recipient, panel)
    before = sub.expires_at
    order = await make_gift_order(session, buyer)

    activation = await gift.redeem(session, order, recipient, panel)
    await session.refresh(sub)

    assert activation.applied_to_active_subscription is True
    assert sub.expires_at > before
    assert (sub.expires_at - before).days >= activation.days - 1


async def test_buyer_cannot_activate_own_gift(session, panel):
    """Свой подарок себе оставить нельзя — иначе смысл подарка теряется."""
    buyer = await make_user(session, 5201)
    order = await make_gift_order(session, buyer)

    with pytest.raises(gift.GiftError) as exc:
        await gift.redeem(session, order, buyer, panel)

    assert exc.value.reason == "self_activation"
    assert order.gift_activated_at is None


async def test_gift_cannot_be_activated_twice(session, panel):
    """Второй человек по той же ссылке подарок не получает."""
    buyer = await make_user(session, 5301)
    first = await make_user(session, 5302)
    second = await make_user(session, 5303)
    order = await make_gift_order(session, buyer)

    await gift.redeem(session, order, first, panel)

    with pytest.raises(gift.GiftError) as exc:
        await gift.redeem(session, order, second, panel)

    assert exc.value.reason == "already_activated"
    assert second.bonus_days_balance == 0


async def test_repeated_activation_by_same_person_is_idempotent(session, panel):
    """Перезапуск ссылки тем же человеком не выдаёт дни второй раз."""
    buyer = await make_user(session, 5401)
    recipient = await make_user(session, 5402)
    order = await make_gift_order(session, buyer)

    await gift.redeem(session, order, recipient, panel)
    balance_after_first = recipient.bonus_days_balance

    again = await gift.redeem(session, order, recipient, panel)

    assert again.already_activated is True
    assert recipient.bonus_days_balance == balance_after_first


async def test_unpaid_gift_cannot_be_activated(session, panel):
    """Сертификат до оплаты не работает."""
    buyer = await make_user(session, 5501)
    recipient = await make_user(session, 5502)
    plan = await first_plan(session)
    order = await orders.create_order(
        session, buyer, plan, provider="manual", kind=orders.KIND_GIFT, with_discount=False
    )
    await gift.attach_gift(session, order)

    with pytest.raises(gift.GiftError) as exc:
        await gift.redeem(session, order, recipient, panel)

    assert exc.value.reason == "not_paid"


async def test_buyer_gets_thank_you_days(session, panel):
    """Подарок радует дважды: покупателю капают дни после активации."""
    buyer = await make_user(session, 5601)
    await subscriptions.start_trial(session, buyer, panel)
    buyer_sub = await subscriptions.get_subscription(session, buyer.id)
    before = buyer_sub.expires_at
    recipient = await make_user(session, 5602)
    order = await make_gift_order(session, buyer)

    activation = await gift.redeem(session, order, recipient, panel)
    await session.refresh(buyer_sub)

    assert activation.buyer_days == settings.gift_buyer_bonus_days
    assert buyer_sub.expires_at > before


async def test_gift_order_does_not_change_buyer_subscription(session, panel):
    """Оплата подарка не должна продлевать подписку покупателя."""
    buyer = await make_user(session, 5701)
    await subscriptions.start_trial(session, buyer, panel)
    sub = await subscriptions.get_subscription(session, buyer.id)
    before = sub.expires_at
    order = await make_gift_order(session, buyer)

    await orders.mark_paid(session, order, panel)
    await session.refresh(sub)

    assert sub.expires_at == before


async def test_mark_paid_gift_does_not_activate_plan(session, panel):
    """mark_paid на подарочном заказе не создаёт подписку покупателю."""
    buyer = await make_user(session, 5801)
    plan = await first_plan(session)
    order = await orders.create_order(
        session, buyer, plan, provider="manual", kind=orders.KIND_GIFT, with_discount=False
    )
    order.amount_rub = gift.gift_price_rub(plan.price_rub)
    await gift.attach_gift(session, order)
    await session.flush()

    sub, already = await orders.mark_paid(session, order, panel)

    assert sub is None
    assert already is False
    assert order.status == "paid"
    assert await subscriptions.get_subscription(session, buyer.id) is None


# ------------------------------------------------------------------ поиск и статистика
async def test_gift_lookup_by_token_variants(session, panel):
    buyer = await make_user(session, 5901)
    order = await make_gift_order(session, buyer)
    token = order.gift_token

    assert (await gift.get_by_token(session, token)).id == order.id
    assert (await gift.get_by_token(session, token.lower())).id == order.id
    assert (await gift.get_by_token(session, token[len(gift.TOKEN_PREFIX):])).id == order.id
    assert await gift.get_by_token(session, "KOMETA-GIFT-XXXXXXXX") is None


async def test_gift_stats_count_bought_paid_activated(session, panel):
    buyer = await make_user(session, 6001)
    recipient = await make_user(session, 6002)
    first = await make_gift_order(session, buyer)
    await make_gift_order(session, buyer)

    await gift.redeem(session, first, recipient, panel)
    stats = await gift.gift_stats(session)

    assert stats["bought"] >= 2
    assert stats["paid"] >= 2
    assert stats["activated"] >= 1


async def test_paid_gift_reachable_by_link(session):
    """get_by_token отдаёт только оплаченные сертификаты."""
    buyer = await make_user(session, 6101)
    plan = await first_plan(session)
    order = await orders.create_order(
        session, buyer, plan, provider="manual", kind=orders.KIND_GIFT, with_discount=False
    )
    await gift.attach_gift(session, order)
    await session.flush()
    token = order.gift_token

    assert await gift.get_by_token(session, token) is None  # ещё не оплачен

    order.status = "paid"
    await session.flush()
    assert (await gift.get_by_token(session, token)).id == order.id


# ------------------------------------------------------------------ бот
async def test_gift_link_activates_in_bot(session, panel, bot, dispatcher):
    """Полный путь: получатель открывает ссылку-подарок и получает дни."""
    buyer = await make_user(session, 6201)
    recipient = await make_user(session, 6202)
    order = await make_gift_order(session, buyer, message="Держи, полезная вещь")
    await session.commit()
    bot.session.clear()

    await dispatcher.feed_update(
        bot, make_update(f"/start gift_{order.gift_token}", user_id=6202)
    )
    text = bot.session.all_text()
    await session.refresh(recipient)

    assert "Тебе подарок" in text
    assert recipient.bonus_days_balance > 0


async def test_gift_wrong_code_explains_what_to_do(session, bot, dispatcher):
    await make_user(session, 6301)
    await session.commit()
    bot.session.clear()

    await dispatcher.feed_update(bot, make_update("/start gift_KOMETA-GIFT-ZZZZZZZZ", user_id=6301))

    assert "Не нашёл такой подарок" in bot.session.all_text()


async def test_buyer_without_subscription_gets_days_in_balance(session, panel):
    """Покупатель без подписки: спасибо днями ложится в запас, а не теряется."""
    buyer = await make_user(session, 6401)
    recipient = await make_user(session, 6402)
    order = await make_gift_order(session, buyer)

    activation = await gift.redeem(session, order, recipient, panel)

    assert activation.buyer_days == settings.gift_buyer_bonus_days
    assert buyer.bonus_days_balance == settings.gift_buyer_bonus_days


async def test_buyer_thanks_text_matches_what_was_granted(session, panel):
    """Сообщение покупателю не обещает больше, чем начислено."""
    buyer = await make_user(session, 6501)
    recipient = await make_user(session, 6502)
    order = await make_gift_order(session, buyer)

    activation = await gift.redeem(session, order, recipient, panel)
    text = gift.buyer_thanks_text(activation)

    assert f"+{activation.buyer_days} дней" in text


# ------------------------------------------------------------------ документы
def test_agreement_describes_gift_terms():
    """Подарок — новый продукт: его условия обязаны быть в соглашении.

    Клиент покупает доступ третьему лицу, активация отложенная, обмену на деньги
    не подлежит. Если этого нет в договоре, спор с покупателем решать нечем.
    """
    from app.services.documents import terms_text

    text = terms_text()

    assert "Подарочный сертификат" in text
    assert f"{settings.gift_valid_days} дней" in text
    assert "не подлежит обмену на денежные средства" in text
    # Бонусные дни — тоже обещание: они не имеют денежной стоимости.
    assert "Бонусные дни" in text
