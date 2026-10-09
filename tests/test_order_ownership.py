"""Регрессии: кнопки заказа принадлежат владельцу заказа.

Повод — находка 09.10.2026. Колбэки `order:cancel:`, `order:manual:` и
`order:check:` брали заказ по id из `callback_data` и **не сверяли владельца**.
Номера заказов последовательные, а кнопки заказа намеренно не закрыты гейтом
подписки (человек мог оплатить, когда подписка уже кончилась). Поэтому
посторонний мог:

* отменить чужой заказ (деньги за счёт не придут);
* отправить владельцу заявку «я оплатил» по чужому заказу под своим именем;
* и самое дорогое — нажатием «Проверить оплату» по чужому **оплаченному** заказу
  получить в личку ссылку на чужую подписку: `finalize_order` отправлял её тому,
  кто нажал, а не тому, чей заказ.

Тесты держат два независимых слоя, потому что чинились они в разных местах:

1. **Авторизация** — чужой заказ не обслуживается вообще (`own_order`).
2. **Доставка** — доступ уходит владельцу заказа; `finalize_order` больше не
   принимает человека снаружи, поэтому «передать не того» стало нельзя
   в принципе (тест `test_finalize_order_delivers_to_order_owner` на старом коде
   падал бы с TypeError — это и есть структурная гарантия).
"""

from __future__ import annotations

import pytest

from app.db.models import User
from app.payments.base import PaymentCheck, PaymentStatus
from app.payments.registry import payments
from app.services import orders, subscriptions
from tests.fakes import make_update

VICTIM = 77001
ATTACKER = 77002

#: Код провайдера-заглушки: проверка платежа всегда говорит «оплачено».
PAID_STUB = "paid_stub"


class _PaidStub:
    """Провайдер, который на любой запрос отвечает «оплачено».

    Нужен, чтобы воспроизвести окно находки: заказ **оплачен у провайдера**,
    но ещё не финализирован (это реальное окно — опрос Platega раз в 30 секунд,
    крипта раз в 2 минуты, поздние подтверждения до 15 минут).
    """

    code = PAID_STUB
    title = "Заглушка «оплачено»"
    manual = False

    async def check_payment(self, external_id: str) -> PaymentCheck:  # noqa: ARG002
        return PaymentCheck(status=PaymentStatus.PAID)

    async def close(self) -> None:
        return None


@pytest.fixture
def paid_provider():
    """Подсунуть реестру провайдера-заглушку (после init(), иначе перезапишется)."""
    stub = _PaidStub()
    payments._providers[PAID_STUB] = stub  # noqa: SLF001 - тесту нужен доступ к реестру
    yield stub
    payments._providers.pop(PAID_STUB, None)  # noqa: SLF001


async def _register(bot, dispatcher, session, tg_id: int) -> User:  # noqa: ANN001
    await dispatcher.feed_update(bot, make_update("/start", user_id=tg_id))
    await dispatcher.feed_update(bot, make_update(callback_data="trial:start", user_id=tg_id))
    bot.session.clear()
    user = await subscriptions.get_user_by_tg(session, tg_id)
    assert user is not None
    return user


@pytest.fixture
async def pair(bot, dispatcher, session):  # noqa: ANN001
    """Жертва и посторонний, у каждого свой заказ."""
    victim = await _register(bot, dispatcher, session, VICTIM)
    attacker = await _register(bot, dispatcher, session, ATTACKER)
    plan = next(p for p in await orders.list_plans(session) if p.code == "m1")

    # Заказ жертвы — через провайдера-заглушку: так воспроизводится настоящее
    # окно находки, когда платёж у провайдера уже прошёл, а выдача ещё нет.
    victim_order = await orders.create_order(session, victim, plan, provider=PAID_STUB)
    attacker_order = await orders.create_order(session, attacker, plan, provider="manual")
    ids = (victim_order.id, attacker_order.id)
    # Коммитим: хендлеры пишут через свою сессию, иначе SQLite отдаёт
    # «database is locked», а тест проверял бы не то.
    await session.commit()
    return victim, attacker, ids[0], ids[1]


async def _status(session, order_id: int) -> str:  # noqa: ANN001
    session.expire_all()
    order = await orders.get_order(session, order_id)
    assert order is not None
    return order.status


# ---------------------------------------------------------------- слой 1: доступ

async def test_stranger_cannot_cancel_someone_elses_order(bot, dispatcher, session, pair):  # noqa: ANN001
    """Чужой заказ не отменяется: раньше отменялся любой."""
    _victim, _attacker, victim_order_id, _attacker_order_id = pair

    await dispatcher.feed_update(
        bot, make_update(callback_data=f"order:cancel:{victim_order_id}", user_id=ATTACKER)
    )

    assert await _status(session, victim_order_id) == "pending"


async def test_stranger_cannot_claim_payment_for_someone_elses_order(bot, dispatcher, session, pair):  # noqa: ANN001
    """По чужому заказу не уходит заявка в чат команды."""
    _victim, _attacker, victim_order_id, _attacker_order_id = pair
    bot.session.clear()

    await dispatcher.feed_update(
        bot, make_update(callback_data=f"order:manual:{victim_order_id}", user_id=ATTACKER)
    )

    # Заявка админам — это SendMessage в чат команды с номером чужого заказа.
    admin_messages = [
        request
        for request in bot.session.by_name("SendMessage")
        if f"#{victim_order_id}" in (getattr(request, "text", "") or "")
    ]
    assert admin_messages == [], "по чужому заказу ушла заявка на подтверждение оплаты"


async def test_stranger_cannot_check_someone_elses_order(bot, dispatcher, session, pair, paid_provider):  # noqa: ANN001
    """Чужой оплаченный заказ не обрабатывается по нажатию постороннего."""
    _victim, _attacker, victim_order_id, _attacker_order_id = pair
    bot.session.clear()

    await dispatcher.feed_update(
        bot, make_update(callback_data=f"order:check:{victim_order_id}", user_id=ATTACKER)
    )

    assert await _status(session, victim_order_id) == "pending", "чужой заказ выдан по чужому нажатию"
    assert "/sub/" not in bot.session.all_text()


# ------------------------------------------------------------ слой 2: получатель

async def test_owner_still_gets_link_when_checking_own_paid_order(bot, dispatcher, session, pair, paid_provider):  # noqa: ANN001
    """Своя кнопка работает как раньше — защита не сломала обычный путь."""
    victim, _attacker, victim_order_id, _attacker_order_id = pair
    # tg_id забираем до чтения статуса: _status сбрасывает кэш сессии, и после
    # этого обращение к полю ORM-объекта полезло бы в БД синхронно.
    owner_tg = victim.tg_id
    bot.session.clear()

    await dispatcher.feed_update(
        bot, make_update(callback_data=f"order:check:{victim_order_id}", user_id=VICTIM)
    )

    recipients = [
        request.chat_id
        for request in bot.session.by_name("SendMessage")
        if "/sub/" in (getattr(request, "text", "") or "")
    ]
    assert recipients == [owner_tg], f"ссылка ушла не владельцу: {recipients}"
    assert await _status(session, victim_order_id) == "paid"


async def test_link_goes_to_order_owner_not_to_presser(bot, dispatcher, session, pair, paid_provider):  # noqa: ANN001
    """Ключевая регрессия находки: посторонний не получает чужую подписку.

    Воспроизводит сценарий атаки целиком: заказ жертвы оплачен у провайдера,
    посторонний жмёт «Проверить оплату». На старом коде он получал в личку
    ссылку на подписку жертвы, а жертва — ничего.
    """
    victim, attacker, victim_order_id, _attacker_order_id = pair
    attacker_tg = attacker.tg_id  # до expire_all — см. комментарий выше
    bot.session.clear()

    await dispatcher.feed_update(
        bot, make_update(callback_data=f"order:check:{victim_order_id}", user_id=ATTACKER)
    )

    leaked = [
        request.chat_id
        for request in bot.session.by_name("SendMessage")
        if "/sub/" in (getattr(request, "text", "") or "")
    ]
    assert attacker_tg not in leaked, "посторонний получил ссылку на чужую подписку"
    assert leaked == [], f"по чужому заказу что-то ушло: {leaked}"

    # И заказ остаётся невыданным: по чужому нажатию доступ не появляется.
    assert await _status(session, victim_order_id) == "pending", "по чужому нажатию выдан доступ"


async def test_finalize_order_delivers_to_order_owner(bot, session):  # noqa: ANN001
    """Доставку определяет заказ, а не инициатор.

    Вызываем выдачу так, как это делает фоновый опрос. Получателя взять снаружи
    больше нельзя — сигнатуры с «пользователем» нет, поэтому перепутать
    человека физически нечем.
    """
    from app.bot.handlers.buy import finalize_order

    # Пользователя создаём напрямую: dispatcher здесь не нужен, проверяем доставку.
    owner, _ = await subscriptions.get_or_create_user(session, tg_id=VICTIM, username="victim")
    plan = next(p for p in await orders.list_plans(session) if p.code == "m1")
    order = await orders.create_order(session, owner, plan, provider="manual")
    await session.commit()

    bot.session.clear()
    await finalize_order(session, order, bot)

    recipients = [
        request.chat_id
        for request in bot.session.by_name("SendMessage")
        if "/sub/" in (getattr(request, "text", "") or "")
    ]
    assert recipients == [owner.tg_id], f"ссылка ушла не владельцу заказа: {recipients}"
