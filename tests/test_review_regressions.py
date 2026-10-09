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
from sqlalchemy import func, select, update

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


# ------------- C8 (находка 09.10.2026): возврат и чарджбэк отзывают доступ
async def test_refunding_the_latest_payment_revokes_access(session, panel):
    """C8: возврат ПОСЛЕДНЕГО платежа обязан отключить доступ.

    Находка 09.10.2026: условие было «есть любая другая оплата», без даты.
    Клиент платит второй раз, возвращает последний платёж — доступ остаётся, а в
    сообщении администратору написано «есть более поздняя оплата», хотя та оплата
    была раньше. Цикл «оплатил → чарджбэк → доступ остался» давал до 1440 ₽ в год
    на одного злоупотребляющего.
    """
    user, first = await _make_order(session, 9951, provider="manual")
    await orders.mark_paid(session, first, panel)
    await session.commit()

    _, second = await _make_order(session, user.tg_id, provider="manual")
    await orders.mark_paid(session, second, panel)
    await session.commit()

    ok, message, sub = await orders.refund_order(session, second, [panel])
    await session.commit()
    await session.refresh(sub)

    assert ok is True
    assert "более поздняя оплата" not in message
    assert sub.status == "blocked", "доступ остался после возврата последнего платежа"


async def test_refunding_an_older_payment_keeps_access(session, panel):
    """Обратный случай не сломан: вернули старый платёж — доступ живёт."""
    user, first = await _make_order(session, 9952, provider="manual")
    await orders.mark_paid(session, first, panel)
    await session.commit()

    _, second = await _make_order(session, user.tg_id, provider="manual")
    await orders.mark_paid(session, second, panel)
    await session.commit()

    ok, message, sub = await orders.refund_order(session, first, [panel])
    await session.commit()
    await session.refresh(sub)

    assert ok is True
    assert "сохранён" in message
    assert sub.status == "active"


class _RefundingPanel(FakePanel):
    """Панель, во время ответа которой по заказу успевает пройти возврат.

    Воспроизводит чарджбэк: вебхук Platega приходит в любой момент, в том числе
    пока панель выдаёт доступ. Возврат делаем из ОТДЕЛЬНОЙ сессии — как это и
    происходит в бою, где вебхук живёт своей транзакцией.
    """

    name = "refunding-panel"

    def __init__(self, order_id: int) -> None:
        super().__init__()
        self._order_id = order_id
        # Возврат ровно один: отзыв доступа тоже зовёт панель, и без флага
        # повторный вызов пытался бы вернуть деньги второй раз.
        self._refunded = False

    async def _refund(self) -> None:
        if self._refunded:
            return
        self._refunded = True
        from app.db.session import SessionMaker

        async with SessionMaker() as other:
            await other.execute(
                update(Order).where(Order.id == self._order_id).values(status="refunded")
            )
            await other.commit()

    async def create_user(self, spec):  # noqa: ANN001, ANN201
        user = await super().create_user(spec)
        await self._refund()
        return user

    async def update_user(self, uuid: str, **kwargs):  # noqa: ANN003, ANN201
        user = await super().update_user(uuid, **kwargs)
        await self._refund()
        return user


async def test_chargeback_during_grant_revokes_access(session):
    """C8: возврат во время выдачи не оставляет доступ живым.

    Находка 09.10.2026: итог был «деньги вернули И доступ живёт» — заказ
    refunded, granted_at проставлен, подписка активна, панель включена.
    """
    user, order = await _make_order(session, 9953, provider="manual")

    sub, already = await orders.mark_paid(session, order, _RefundingPanel(order.id))
    await session.commit()
    await session.refresh(order)

    assert already is False
    assert order.status == "refunded"
    assert order.granted_at is None, "выдача подтверждена по возвращённому заказу"
    sub_row = await subscriptions.get_subscription(session, user.id)
    assert sub_row is None or sub_row.status != "active", "доступ остался активным после возврата"


# ----------------------- C7 (находка 09.10.2026): захват выдачи
async def test_inflight_grant_is_not_started_twice(session, panel):
    """C7: пока выдача идёт, фоновая задача не зовёт панель второй раз.

    Находка 09.10.2026: деньги фиксируются в БД ДО обращения к панели (B2),
    поэтому между этими моментами заказ виден как «оплачен и не выдан».
    Фоновая задача брала такой заказ и продлевала клиента второй раз — в худшем
    случае лишний месяц доступа за одну оплату.
    """
    _, order = await _make_order(session, 9941, provider="manual")
    await orders.mark_paid(session, order, DeadPanel())
    await session.commit()
    await session.refresh(order)

    assert order.grant_claimed_at is None, "неудачная попытка обязана освободить захват"

    # Кто-то уже выдаёт этот заказ прямо сейчас.
    order.grant_claimed_at = datetime.now(timezone.utc)
    await session.commit()

    granted = await orders.grant_ungranted_orders(session, [panel])
    await session.commit()
    await session.refresh(order)

    assert granted == [], "заказ выдан, хотя его уже выдаёт другой путь"
    assert order.granted_at is None


async def test_expired_claim_is_taken_over(session, panel):
    """Просроченный захват (процесс упал) не блокирует заказ навсегда."""
    _, order = await _make_order(session, 9942, provider="manual")
    await orders.mark_paid(session, order, DeadPanel())
    await session.commit()
    await session.refresh(order)

    order.grant_claimed_at = datetime.now(timezone.utc) - timedelta(
        minutes=orders.GRANT_CLAIM_TTL_MINUTES + 1
    )
    await session.commit()

    granted = await orders.grant_ungranted_orders(session, [panel])
    await session.commit()
    await session.refresh(order)

    assert granted == [order.id]
    assert order.granted_at is not None


async def test_retry_is_not_delayed_by_the_claim(session, panel):
    """После неудачи захват снят: следующий тик выдаёт сразу, а не через TTL."""
    _, order = await _make_order(session, 9943, provider="manual")
    await orders.mark_paid(session, order, DeadPanel())
    await session.commit()
    await session.refresh(order)

    granted = await orders.grant_ungranted_orders(session, [panel])
    await session.commit()

    assert granted == [order.id]


# ------------------- C6 (находка 09.10.2026): выдача по снимку тарифа
async def test_grant_uses_plan_snapshot_not_live_plan(session, panel):
    """C6: правка тарифа в окне заказа не меняет то, что получит клиент.

    Находка 09.10.2026: заказ хранил цену, но не срок и лимиты — выдача читала
    ЖИВОЙ тариф. Правка ``plan.days`` 30 → 365 после создания заказа давала
    оплату 120 ₽ за 364 дня вместо 959 ₽ (окно до 24 часов: закрытые заказы
    переопрашиваются, то есть ровно правка прайса в день запуска). Обратная
    правка отнимала у клиента оплаченное.
    """
    _, order = await _make_order(session, 9931, provider="manual")
    plan = await orders.get_plan(session, order.plan_id)
    paid_days, paid_devices, paid_traffic = plan.days, plan.devices_limit, plan.traffic_limit_gb

    assert order.plan_days == paid_days, "условия тарифа не записаны в заказ"

    plan.days = paid_days + 335
    plan.devices_limit = paid_devices + 10
    plan.traffic_limit_gb = (paid_traffic or 0) + 500
    await session.commit()

    before = datetime.now(timezone.utc)
    sub, already = await orders.mark_paid(session, order, panel)
    await session.commit()

    assert already is False and sub is not None
    assert sub.expires_at <= before + timedelta(days=paid_days + 1), "выдан срок из правленого тарифа"
    assert sub.devices_limit == paid_devices
    assert sub.traffic_limit_gb == paid_traffic


async def test_legacy_order_without_snapshot_falls_back_to_live_plan(session, panel):
    """Заказы до 09.10.2026 (без снимка) обслуживаются как раньше."""
    _, order = await _make_order(session, 9932, provider="manual")
    plan = await orders.get_plan(session, order.plan_id)
    live_days = plan.days

    order.plan_days = None
    order.plan_devices_limit = None
    order.plan_traffic_gb = None
    await session.commit()

    before = datetime.now(timezone.utc)
    sub, already = await orders.mark_paid(session, order, panel)
    await session.commit()

    assert already is False and sub is not None
    assert sub.expires_at >= before + timedelta(days=live_days - 1)


# ---------------------------------- C5 (находка 09.10.2026): сбой выдачи слышно
class _RecordingBot:
    """Бот-заглушка: запоминает, что ушло команде."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:  # noqa: ANN003, ARG002
        self.sent.append((chat_id, text))

    def texts(self) -> str:
        return " ".join(text for _, text in self.sent)


async def test_grant_failure_raises_money_alert_and_pushes(session):
    """C5: «оплачено, доступа нет» — денежный алерт и сообщение, а не строка в логе.

    Находка 09.10.2026: поднимался алерт вида ``panel_error`` важности ``warn``,
    в Telegram не уходило ничего (``alerts.py`` вообще не отправляет), а после
    десяти попыток оставался только ``logger.error``. Клиент, заплативший утром,
    ждал до вечерней сводки, где видел счётчик алертов.
    """
    _, order = await _make_order(session, 9921, provider="manual")
    bot = _RecordingBot()

    await orders.mark_paid(session, order, DeadPanel(), bot=bot)
    await session.commit()

    alert = await session.scalar(select(Alert).where(Alert.fingerprint == f"grant:order:{order.id}"))
    assert alert is not None
    assert alert.kind == "payment_not_granted", "денежный сбой не должен зваться «ошибка панели»"
    assert alert.severity == "err", "важность warn прятала сбой в суточной сводке"

    assert bot.sent, "о «оплачено, доступа нет» команда не узнала"
    assert f"#{order.id}" in bot.texts()


async def test_repeated_grant_failures_do_not_spam_team(session):
    """Повторы выдачи каждые 3 минуты не превращаются в спам."""
    _, order = await _make_order(session, 9922, provider="manual")
    bot = _RecordingBot()

    await orders.mark_paid(session, order, DeadPanel(), bot=bot)
    await session.commit()
    assert len(bot.sent) == 1

    for _ in range(3):
        await orders.grant_ungranted_orders(session, [DeadPanel()], bot=bot)
        await session.commit()

    assert len(bot.sent) == 1, f"повторы снова пишут команде: {len(bot.sent)} сообщений"


async def test_exhausted_grant_attempts_shout_once(session):
    """Автоповторы кончились — «нужен человек» звучит ровно один раз.

    Раньше здесь была только строка в логе: заказ оставался оплаченным без
    доступа навсегда, и никто об этом не узнавал.
    """
    _, order = await _make_order(session, 9923, provider="manual")
    bot = _RecordingBot()

    await orders.mark_paid(session, order, DeadPanel(), bot=bot)
    await session.commit()
    await session.refresh(order)

    order.grant_attempts = orders.MAX_GRANT_ATTEMPTS
    await session.commit()
    bot.sent.clear()

    for _ in range(3):
        await orders.grant_ungranted_orders(session, [DeadPanel()], bot=bot)
        await session.commit()

    stuck = await session.scalar(
        select(Alert).where(Alert.fingerprint == f"grant-stuck:order:{order.id}")
    )
    assert stuck is not None, "об исчерпании попыток не поднято ни одного алерта"
    assert stuck.kind == "payment_not_granted"
    assert len(bot.sent) == 1, f"«нужен человек» прозвучало {len(bot.sent)} раз вместо одного"


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


# ------------------------------------ C4 (находка 09.10.2026): код заказа и сумма
async def test_order_code_with_smaller_amount_is_not_confirmed(session, autopay_env):
    """C4: код заказа в комментарии не заменяет сверку суммы.

    Находка 09.10.2026: ветка «код заказа в комментарии» возвращала заказ **до**
    любых проверок суммы, а номер заказа последовательный и напечатан клиенту в
    инструкции ``ManualProvider.create_invoice``. Перевод на 1 ₽ с комментарием
    «Kometa 7» подтверждал заказ на 120 ₽, выдавал 30 дней и не поднимал алерта.

    Дефект спал только потому, что реквизиты перевода не заполнены. Он
    вооружается в день, когда вклюют СБП-перевод, — поэтому тест здесь.
    """
    from app.services import autopay

    user, order = await _make_order(session, 9911, provider="manual")

    found, reason = await autopay.find_order_for_payment(
        session, _payment(100, comment=f"Kometa {order.id}")
    )

    assert found is None
    assert reason == autopay.UNDERPAID_MATCH

    # И на уровне всего цикла: доступ не выдаётся, платёж уходит админам.
    result = await autopay.reconcile(
        session,
        None,
        None,
        sources=[_StaticSource([_payment(100, comment=f"Kometa {order.id}", key="op-under")])],
    )
    await session.commit()

    assert result.confirmed == []
    assert result.unmatched
    assert await session.scalar(select(Subscription).where(Subscription.user_id == user.id)) is None
    alert = await session.scalar(select(Alert).where(Alert.kind == "payment_unmatched"))
    assert alert is not None


async def test_order_code_with_full_amount_is_confirmed(session, autopay_env):
    """Обычный путь не сломан: верный код и полная сумма подтверждают заказ."""
    from app.services import autopay

    _, order = await _make_order(session, 9912, provider="manual")

    found, reason = await autopay.find_order_for_payment(
        session, _payment(order.pay_amount_kopecks, comment=f"Kometa {order.id}")
    )

    assert found is not None and found.id == order.id
    assert "по коду заказа" in reason


async def test_order_code_with_overpayment_is_confirmed(session, autopay_env):
    """Переплата не мешает: клиент заплатил больше — заказ подтверждаем."""
    from app.services import autopay

    _, order = await _make_order(session, 9913, provider="manual")

    found, _reason = await autopay.find_order_for_payment(
        session, _payment(order.pay_amount_kopecks + 5000, comment=f"Kometa {order.id}")
    )

    assert found is not None and found.id == order.id


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
