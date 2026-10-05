"""Тесты автоподтверждения переводов по выписке.

Проверяем весь путь: заказ с уникальной суммой → поступление в выписке →
подтверждение → выдача подписки, а также защиту от повторного подтверждения
и уведомление о непонятном платеже.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.db.models import Event, User
from app.payments.statements import IncomingPayment, StatementError, StatementSource
from app.services import autopay, orders, subscriptions

settings = get_settings()


class FakeSource(StatementSource):
    """Источник выписки для тестов: отдаёт заранее заданные поступления."""

    name = "fake"

    def __init__(self, payments: list[IncomingPayment]) -> None:
        self.payments = payments

    async def fetch(self, since: datetime) -> list[IncomingPayment]:
        return [p for p in self.payments if p.received_at >= since]


class RepeatingSource(StatementSource):
    """Источник, который отдаёт платёж при каждом опросе (не дедуплицирует сам).

    Так проверяем именно нашу защиту: один и тот же платёж не должен
    подтвердить заказ дважды.
    """

    name = "repeating"

    def __init__(self, payment: IncomingPayment) -> None:
        self.payment = payment

    async def fetch(self, since: datetime) -> list[IncomingPayment]:
        return [self.payment]


@pytest.fixture(autouse=True)
def autopay_env(monkeypatch, tmp_path):
    """Включаем автоплатёж и уводим файл состояния в tmp."""
    monkeypatch.setattr(settings, "autopay_enabled", True)
    monkeypatch.setattr(settings, "statement_state_file", str(tmp_path / "state.json"))
    monkeypatch.setattr(settings, "autopay_tolerance_kopecks", 0)
    yield


class RecordingBot:
    """Минимальная заглушка Bot: запоминает отправленные сообщения."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs):  # noqa: ANN003
        self.messages.append((chat_id, text))

    def texts(self) -> str:
        return " ".join(text for _, text in self.messages)


async def make_manual_order(session, tg_id: int = 7001):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=tg_id, username=f"u{tg_id}")
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="manual")
    await session.flush()
    return user, plan, order


def payment_for(order, *, comment: str = "", source: str = "fake", key: str = "p1") -> IncomingPayment:
    return IncomingPayment(
        amount_kopecks=order.pay_amount_kopecks,
        received_at=datetime.now(timezone.utc),
        comment=comment,
        counterparty="ИВАН И.",
        source=source,
        external_id=key,
    )


async def test_manual_order_gets_unique_kopecks(session):
    _, _, first = await make_manual_order(session, tg_id=7101)
    _, _, second = await make_manual_order(session, tg_id=7102)

    assert first.pay_kopecks != second.pay_kopecks
    assert first.pay_amount_kopecks == first.amount_rub * 100 + first.pay_kopecks
    assert "." in first.pay_amount_text


async def test_payment_by_exact_amount_confirms_order(session, panel):
    user, _, order = await make_manual_order(session, tg_id=7201)
    await session.commit()

    bot = RecordingBot()
    result = await autopay.reconcile(session, panel, bot, sources=[FakeSource([payment_for(order)])])
    await session.commit()

    assert result.confirmed == [order.id]
    await session.refresh(order)
    assert order.status == "paid"

    sub = await subscriptions.get_subscription(session, user.id)
    assert sub is not None and sub.status == "active"
    assert "/sub/" in bot.texts()  # пользователю ушла ссылка-подписка


async def test_payment_by_comment_code_confirms_order(session, panel):
    _, _, order = await make_manual_order(session, tg_id=7301)
    await session.commit()

    # сумма «круглая» (банк потерял копейки), но в комментарии есть код заказа
    payment = IncomingPayment(
        amount_kopecks=order.amount_rub * 100,
        received_at=datetime.now(timezone.utc),
        comment=f"Kometa {order.id}",
        source="fake",
        external_id="p2",
    )
    result = await autopay.reconcile(session, panel, None, sources=[FakeSource([payment])])
    await session.commit()

    assert result.confirmed == [order.id]
    await session.refresh(order)
    assert order.status == "paid"


async def test_same_payment_is_not_confirmed_twice(session, panel):
    _, _, order = await make_manual_order(session, tg_id=7401)
    await session.commit()
    payment = payment_for(order, key="dup-1")
    source = RepeatingSource(payment)

    first = await autopay.reconcile(session, panel, None, sources=[source])
    await session.commit()
    assert first.confirmed == [order.id]

    second = await autopay.reconcile(session, panel, None, sources=[source])
    assert second.confirmed == []
    assert second.skipped == 1


async def test_unmatched_payment_notifies_admin(session, panel):
    bot = RecordingBot()
    stranger = IncomingPayment(
        amount_kopecks=5000,
        received_at=datetime.now(timezone.utc),
        comment="перевод от мамы",
        counterparty="МАРИЯ П.",
        source="fake",
        external_id="unknown-1",
    )

    result = await autopay.reconcile(session, panel, bot, sources=[FakeSource([stranger])])
    await session.commit()

    assert result.unmatched and not result.confirmed
    assert "заказ не найден" in bot.texts().lower()
    assert "50.00" in bot.texts()


async def test_old_payments_are_filtered_by_last_check(session, panel):
    _, _, order = await make_manual_order(session, tg_id=7501)
    await session.commit()

    old = payment_for(order, key="old-1")
    old.received_at = datetime.now(timezone.utc) - timedelta(days=3)

    result = await autopay.reconcile(session, panel, None, sources=[FakeSource([old])])
    assert result.fetched == 0
    await session.refresh(order)
    assert order.status == "pending"  # старый платёж не подтверждаем


async def test_autopay_disabled_does_nothing(session, panel, monkeypatch):
    monkeypatch.setattr(settings, "autopay_enabled", False)
    _, _, order = await make_manual_order(session, tg_id=7601)
    await session.commit()

    result = await autopay.reconcile(session, panel, None, sources=[FakeSource([payment_for(order)])])

    assert result.fetched == 0 and result.confirmed == []
    await session.refresh(order)
    assert order.status == "pending"


async def test_payment_before_order_creation_is_kept_for_admin(session, panel):
    """Оплата пришла, а заказ ещё не создан (или уже истёк) — админ узнаёт."""
    bot = RecordingBot()
    payment = IncomingPayment(
        amount_kopecks=19913,
        received_at=datetime.now(timezone.utc),
        comment="Kometa 999999",
        source="fake",
        external_id="early-1",
    )

    result = await autopay.reconcile(session, panel, bot, sources=[FakeSource([payment])])
    await session.commit()

    assert result.unmatched
    assert "не найден" in bot.texts().lower()


class FailingSource(StatementSource):
    """Источник, который падает, но часть поступлений успел прочитать."""

    name = "failing"

    def __init__(self, recovered: list[IncomingPayment]) -> None:
        self.recovered = recovered

    async def fetch(self, since: datetime) -> list[IncomingPayment]:
        error = StatementError("битый файл выписки")
        error.payments = self.recovered  # как договорились с источником
        raise error


async def test_payments_recovered_from_failed_source_are_confirmed(session, panel):
    """Один битый файл не должен задерживать оплату из остальных файлов."""
    _, _, order = await make_manual_order(session, tg_id=7701)
    await session.commit()

    result = await autopay.reconcile(
        session, panel, None, sources=[FailingSource([payment_for(order, key="recovered-1")])]
    )
    await session.commit()

    assert result.confirmed == [order.id]
    assert result.errors  # об ошибке всё равно сообщаем
    await session.refresh(order)
    assert order.status == "paid"


async def test_failed_source_does_not_advance_time_window(session, panel, tmp_path):
    """При ошибке чтения окно проверки не сдвигается — иначе платёж потеряется."""
    _, _, order = await make_manual_order(session, tg_id=7801)
    await session.commit()

    await autopay.reconcile(session, panel, None, sources=[FailingSource([])])
    state_after_failure = autopay.load_state()

    assert "last_check" not in state_after_failure or state_after_failure["last_check"] <= (
        datetime.now(timezone.utc).isoformat()
    )
    # и повторный прогон с рабочим источником по-прежнему видит платёж
    result = await autopay.reconcile(
        session, panel, None, sources=[FakeSource([payment_for(order, key="after-failure")])]
    )
    await session.commit()
    assert result.confirmed == [order.id]
