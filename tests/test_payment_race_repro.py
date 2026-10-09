"""Воспроизведение инцидента 09.10.2026: одна оплата — две выдачи доступа.

Что произошло на проде (заказ #8, 60 ₽, СБП Platega):

1. 06:24:24 клиент нажал «Проверить оплату» — хендлер прочитал заказ
   (``status="pending"``) и ушёл в медленную проверку транзакции в Platega
   (в бою такой GET отвечал ~10 секунд).
2. 06:24:31 фоновый опрос ``job_check_platega`` успел подтвердить ту же
   транзакцию, выдал доступ и закоммитил заказ как ``paid``.
3. 06:24:34 хендлер кнопки вернулся из Platega с «оплачено» и вызвал
   ``mark_paid`` **со старым объектом заказа из памяти сессии**, где статус
   всё ещё ``pending``: проверка идемпотентности ``if order.status == "paid"``
   ничего не заметила, и подписка продлилась второй раз.

Тест повторяет шаг 3: заказ читается «давно» (до выдачи), выдача происходит
в другой сессии, а затем старый объект отдаётся в ``mark_paid``.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import Referral
from app.db.session import SessionMaker
from app.services import orders, referral, subscriptions


async def _paid_subscription_expiry(session, user_id: int):
    sub = await subscriptions.get_subscription(session, user_id)
    assert sub is not None
    return sub.expires_at


async def test_stale_order_object_does_not_grant_twice(session, panel):
    """Подтверждение оплаты по устаревшему объекту заказа не продлевает срок."""
    user, _ = await subscriptions.get_or_create_user(
        session, tg_id=9001, username="stale", first_name="Stale"
    )
    await subscriptions.start_trial(session, user, panel)
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="platega_sbp")
    await session.commit()
    order_id = order.id
    user_id = user.id

    # --- Путь A: «Проверить оплату». Заказ прочитан, пока он ещё pending, и
    #     дальше идёт долгая проверка платежа (в бою ~10 секунд).
    stale_session = SessionMaker()
    stale_order = await orders.get_order(stale_session, order_id)
    assert stale_order.status == "pending"

    # --- Путь B: фоновый опрос успел подтвердить оплату и выдать доступ.
    async with SessionMaker() as fast_session:
        fresh_order = await orders.get_order(fast_session, order_id)
        sub, already = await orders.mark_paid(fast_session, fresh_order, panel)
        await fast_session.commit()
        assert already is False
        expires_after_first_grant = sub.expires_at

    # --- Путь A возвращается с подтверждением и отдаёт заказ дальше.
    sub_again, already_again = await orders.mark_paid(stale_session, stale_order, panel)
    await stale_session.commit()
    await stale_session.close()

    assert already_again is True, "второй вызов mark_paid должен быть пустым (заказ уже оплачен)"
    assert sub_again is not None
    assert sub_again.expires_at == expires_after_first_grant, "срок подписки продлился второй раз"


async def test_referral_is_not_rewarded_twice_for_one_order(session, panel):
    """Одна оплата приглашённого — одна награда пригласившему (не «продление»)."""
    referrer, _ = await subscriptions.get_or_create_user(
        session, tg_id=9002, username="ref", first_name="Ref"
    )
    invited, _ = await subscriptions.get_or_create_user(
        session, tg_id=9003, username="inv", first_name="Inv"
    )
    await session.flush()
    await referral.attach_referrer(session, invited, referrer.referral_code)
    await subscriptions.start_trial(session, invited, panel)
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, invited, plan, provider="platega_sbp")
    await session.commit()
    order_id = order.id

    # Путь A читает заказ заранее (как кнопка «Проверить оплату» до ответа Platega).
    stale_session = SessionMaker()
    stale_order = await orders.get_order(stale_session, order_id)
    assert stale_order.status == "pending"

    async with SessionMaker() as fast_session:
        fresh_order = await orders.get_order(fast_session, order_id)
        await orders.mark_paid(fast_session, fresh_order, panel)
        await fast_session.commit()

    # Повторная выдача по тому же заказу (баг) не должна считаться «продлением».
    await orders.mark_paid(stale_session, stale_order, panel)
    await stale_session.commit()
    await stale_session.close()

    ref = await session.scalar(select(Referral).where(Referral.invited_id == invited.id))
    assert ref.renewals_count == 0, "повторная выдача не должна засчитываться как продление"
    assert ref.renewal_bonus_days == 0
