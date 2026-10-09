"""PoC-регрессии ревью 09.10.2026 (B1–B4).

Каждый тест — это воспроизведение дыры, найденной на проде или аудитом, а не
«тест ради покрытия». Правило: сначала шаг из PoC, потом проверка, что деньги и
доступ сходятся.

Что здесь:

* **B1** — вебхук, подтверждающий оплату, не выдаёт доступ за чужую сумму
  (PoC: заказ на 959 ₽ подтверждался платежом на 10 ₽ через WATA).
* **B2** — «деньги приняты — доступ не выдан и не будет выдан»: при недоступной
  панели заказ остаётся в очереди на выдачу, а не теряется.
* **B3** — автоплатёж не подтверждает платёж по чужому заказу, когда сумма
  совпала без копеек.
* **B4** — одна оплата даёт ровно одну выдачу, даже если подтверждение пришло
  двумя параллельными путями.

Приёмка всего ТЗ: ``bash scripts/accept_review.sh``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import Alert, Event, Order, Subscription
from app.panels.base import PanelError, PanelUser, UserSpec
from app.panels.fake import FakePanel
from app.payments.base import PaymentCheck, PaymentStatus
from app.payments.registry import payments
from app.payments.statements import IncomingPayment
from app.services import orders, subscriptions
from app.web.sub import build_app

settings = get_settings()
MERCHANT = "1a021d91-9b26-4762-b303-5d4aac74e921"
SECRET = "test-platega-secret"


class DeadPanel(FakePanel):
    """Панель, которая не отвечает: воспроизводит «упала в момент оплаты» (B2)."""

    name = "dead-panel"

    async def create_user(self, spec: UserSpec) -> PanelUser:
        raise PanelError("панель не отвечает (PoC B2)")

    async def update_user(self, uuid: str, **kwargs) -> PanelUser:  # noqa: ANN003
        raise PanelError("панель не отвечает (PoC B2)")


class ShortGrantPanel(FakePanel):
    """Панель отвечает «успехом», но срок не продлевает — расхождение с целью (B2)."""

    name = "short-grant-panel"

    async def create_user(self, spec: UserSpec) -> PanelUser:
        user = await super().create_user(spec)
        user.expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        return user


@pytest.fixture(autouse=True)
def platega_settings(monkeypatch):
    """Настроенный Platega: вебхук без провайдера отвечает 503 и ничего не значит."""
    monkeypatch.setattr(settings, "platega_merchant_id", MERCHANT)
    monkeypatch.setattr(settings, "platega_secret", SECRET)
    monkeypatch.setattr(settings, "platega_methods", "2,10")
    payments.reload()
    yield
    payments.reload()


@pytest.fixture
async def client(session):
    app = await build_app(bot=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def _headers(merchant: str = MERCHANT, secret: str = SECRET) -> dict[str, str]:
    return {"X-MerchantId": merchant, "X-Secret": secret}


def _body(order_id: int, amount: float = 199.0, status: str = "CONFIRMED") -> bytes:
    return json.dumps(
        {
            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "amount": amount,
            "currency": "RUB",
            "status": status,
            "paymentMethod": 2,
            "payload": f"order:{order_id}",
        }
    ).encode()


def _stub_check(monkeypatch, provider, status: PaymentStatus, amount: int | None):  # noqa: ANN001
    async def fake_check(external_id: str) -> PaymentCheck:
        return PaymentCheck(status=status, amount=amount, raw={"id": external_id, "status": status.value})

    monkeypatch.setattr(provider, "check_payment", fake_check)


async def _make_order(session, tg_id: int, provider: str = "platega_sbp"):  # noqa: ANN001
    user, _ = await subscriptions.get_or_create_user(session, tg_id=tg_id, username=f"rev{tg_id}")
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider=provider)
    await session.commit()
    return user, order


# ------------------------------------------------------------------------- B1
async def test_wata_webhook_does_not_exist(client: httpx.AsyncClient):
    """B1: небезопасного вебхука WATA больше нет — маршрут отвечает 404.

    Через него заказ на 959 ₽ подтверждался платежом на 10 ₽: сумма не
    сверялась, а тело подписано общим для всех мерчантов ключом.
    """
    response = await client.post("/payments/wata/webhook", content=b"{}")

    assert response.status_code == 404


async def test_paid_webhook_with_wrong_amount_does_not_grant(
    client: httpx.AsyncClient, session, monkeypatch
):  # noqa: ANN001
    """B1: подтверждённая, но дешёвая транзакция не выдаёт дорогой заказ."""
    _, order = await _make_order(session, 9901)
    provider = next(p for p in payments.available() if getattr(p, "merchant_id", None))
    _stub_check(monkeypatch, provider, PaymentStatus.PAID, amount=10)

    response = await client.post("/payments/platega/webhook", content=_body(order.id), headers=_headers())

    assert response.status_code == 200
    assert response.json().get("skipped") == "amount mismatch"
    await session.refresh(order)
    assert order.status == "pending"
    assert await session.scalar(select(Subscription).where(Subscription.user_id == order.user_id)) is None


# ------------------------------------------------------------------------- B2
async def test_panel_outage_keeps_paid_order_in_grant_queue(session):
    """B2: панель упала при оплате — заказ остаётся оплаченным и ждёт выдачи.

    PoC аудита: ``order.status`` становился ``paid`` до обращения к панели,
    ``PanelError`` гасился, вызывающий коммитил — и заказ выпадал из всех
    очередей ретрая. Клиент платил и не получал ничего.
    """
    user, order = await _make_order(session, 9902, provider="manual")

    sub, already = await orders.mark_paid(session, order, DeadPanel())
    await session.commit()
    await session.refresh(order)

    assert sub is None and already is False
    assert order.status == "paid"
    assert order.paid_at is not None
    assert order.granted_at is None
    assert order.grant_target_at is not None
    assert "панель не отвечает" in order.grant_last_error

    alert = await session.scalar(select(Alert).where(Alert.fingerprint == f"grant:order:{order.id}"))
    assert alert is not None and alert.status == "open"
    assert await session.scalar(select(Subscription).where(Subscription.user_id == user.id)) is None


async def test_grant_is_retried_until_panel_gives_access(session, panel):
    """B2: после восстановления панели фоновая задача выдаёт доступ сама."""
    user, order = await _make_order(session, 9903, provider="manual")
    await orders.mark_paid(session, order, DeadPanel())
    await session.commit()
    await session.refresh(order)
    target = order.grant_target_at

    granted = await orders.grant_ungranted_orders(session, [panel])
    await session.commit()
    await session.refresh(order)

    assert granted == [order.id]
    assert order.granted_at is not None
    assert order.grant_last_error == ""
    sub = await subscriptions.get_subscription(session, user.id)
    assert sub is not None and sub.expires_at >= target

    alert = await session.scalar(select(Alert).where(Alert.fingerprint == f"grant:order:{order.id}"))
    assert alert is None or alert.status == "resolved"


async def test_short_grant_is_treated_as_failure(session):
    """B2: «успех» панели без продления срока — расхождение, а не выдача.

    Панель могла ответить 200 и не продлить клиента (ручная правка, чужой
    uuid). Сверяем факт: срок должен быть не меньше цели выдачи.
    """
    _, order = await _make_order(session, 9904, provider="manual")

    sub, already = await orders.mark_paid(session, order, ShortGrantPanel())
    await session.commit()
    await session.refresh(order)

    assert already is False
    assert order.granted_at is None
    assert "ожидали минимум" in order.grant_last_error or "срок подписки" in order.grant_last_error
    assert sub is not None  # подписка есть, но факт выдачи не подтверждён — ретрай доведёт


# ------------------------------------------------------------------------- B3
class _StaticSource:
    """Источник выписки для теста: отдаёт заранее заданные поступления."""

    name = "static"

    def __init__(self, payments: list) -> None:  # noqa: ANN001
        self._payments = payments

    async def fetch(self, since: datetime) -> list:  # noqa: ANN001
        return list(self._payments)

    async def close(self) -> None:  # pragma: no cover
        return None


@pytest.fixture
def autopay_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "autopay_enabled", True)
    monkeypatch.setattr(settings, "statement_state_file", str(tmp_path / "state.json"))
    monkeypatch.setattr(settings, "autopay_tolerance_kopecks", 0)
    yield


def _payment(amount_kopecks: int, *, comment: str = "", key: str = "op-1") -> IncomingPayment:
    return IncomingPayment(
        amount_kopecks=amount_kopecks,
        received_at=datetime.now(timezone.utc),
        comment=comment,
        source="static",
        external_id=key,
    )


async def test_amount_without_kopecks_is_not_guessed_between_two_orders(session, autopay_env):
    """B3: два заказа на одну сумму — платёж без копеек не достаётся никому.

    PoC аудита: при совпадении суммы без копеек брался первый по ``created_at``
    pending-заказ, и платёж клиента №2 подтверждал заказ №1.
    """
    from app.services import autopay

    user_a, order_a = await _make_order(session, 9906, provider="manual")
    user_b, order_b = await _make_order(session, 9907, provider="manual")
    await session.commit()

    order, reason = await autopay.find_order_for_payment(
        session, _payment(order_a.amount_rub * 100, comment="перевод")
    )

    assert order is None
    assert reason == autopay.AMBIGUOUS_MATCH

    # И на уровне всего цикла: доступ не выдаётся никому, админ получает алерт.
    result = await autopay.reconcile(
        session, None, None, sources=[_StaticSource([_payment(order_a.amount_rub * 100, comment="перевод")])]
    )
    await session.commit()

    assert result.confirmed == []
    assert result.unmatched
    assert await session.scalar(select(Subscription).where(Subscription.user_id.in_([user_a.id, user_b.id]))) is None
    alert = await session.scalar(select(Alert).where(Alert.kind == "payment_unmatched"))
    assert alert is not None


async def test_amount_without_kopecks_matches_the_only_candidate(session, autopay_env):
    """B3: если кандидат один — платёж без копеек подтверждает именно его."""
    from app.services import autopay

    _, order = await _make_order(session, 9908, provider="manual")
    await session.commit()

    found, reason = await autopay.find_order_for_payment(session, _payment(order.amount_rub * 100))

    assert found is not None and found.id == order.id
    assert "без копеек" in reason


# ------------------------------------------------------------------------- B4
async def test_one_payment_grants_once_even_with_stale_order_object(session, panel):
    """B4: повторное подтверждение с устаревшим объектом не продлевает срок дважды.

    PoC: между чтением заказа и подтверждением проходит запрос к платёжной
    системе (в бою ~10 с), за это время заказ подтверждает параллельный путь, а
    в памяти вызывающего статус всё ещё ``pending``.
    """
    user, order = await _make_order(session, 9909, provider="manual")
    stale = await session.get(Order, order.id)

    first_sub, first_already = await orders.mark_paid(session, order, panel)
    await session.commit()
    first_expiry = first_sub.expires_at

    second_sub, second_already = await orders.mark_paid(session, stale, panel)
    await session.commit()

    assert first_already is False
    assert second_already is True
    assert second_sub is not None and second_sub.expires_at == first_expiry

    subs = list(
        (await session.scalars(select(Subscription).where(Subscription.user_id == user.id))).all()
    )
    assert len(subs) == 1
    paid_events = int(
        await session.scalar(
            select(func.count(Event.id)).where(Event.kind == "order_paid", Event.user_id == user.id)
        )
        or 0
    )
    assert paid_events == 1
