"""Тесты кнопки «Проверить выписку сейчас» в админ-панели."""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.payments.statements import IncomingPayment, StatementSource
from app.services import orders, subscriptions
from app.web import security
from app.web.sub import build_app
from datetime import datetime, timezone

PASSWORD = "test-admin-password"
settings = get_settings()


class OneShotSource(StatementSource):
    name = "test"

    def __init__(self, payment: IncomingPayment) -> None:
        self.payment = payment

    async def fetch(self, since: datetime) -> list[IncomingPayment]:
        return [self.payment]


@pytest.fixture(autouse=True)
def admin_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "admin_panel_password", PASSWORD)
    monkeypatch.setattr(settings, "admin_panel_secret", "test-secret")
    monkeypatch.setattr(settings, "statement_state_file", str(tmp_path / "state.json"))
    security.login_throttle._attempts.clear()
    yield


@pytest.fixture
async def client(session):
    app = await build_app(bot=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", follow_redirects=False) as c:
        yield c


async def login(client: httpx.AsyncClient) -> None:
    response = await client.post("/admin/login", data={"password": PASSWORD})
    assert response.status_code == 303


async def test_autopay_button_requires_authentication(client):
    response = await client.post("/admin/autopay/run")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


async def test_autopay_button_reports_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "autopay_enabled", False)
    await login(client)

    response = await client.post("/admin/autopay/run")

    assert response.status_code == 303
    assert "error" in response.headers["location"]


async def test_autopay_button_confirms_payment(client, session, panel, monkeypatch):
    monkeypatch.setattr(settings, "autopay_enabled", True)

    user, _ = await subscriptions.get_or_create_user(session, tg_id=9501, username="auto")
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="manual")
    await session.commit()

    payment = IncomingPayment(
        amount_kopecks=order.pay_amount_kopecks,
        received_at=datetime.now(timezone.utc),
        comment=f"Kometa {order.id}",
        source="test",
        external_id="btn-1",
    )
    monkeypatch.setattr(
        "app.services.autopay.build_statement_sources", lambda: [OneShotSource(payment)]
    )

    await login(client)
    response = await client.post("/admin/autopay/run")

    assert response.status_code == 303
    assert "message" in response.headers["location"]

    await session.refresh(order)
    assert order.status == "paid"
    sub = await subscriptions.get_subscription(session, user.id)
    assert sub is not None and sub.status == "active"


async def test_dashboard_shows_autopay_status(client, monkeypatch):
    monkeypatch.setattr(settings, "autopay_enabled", True)
    await login(client)

    response = await client.get("/admin")

    assert "Автоплатёж" in response.text
    assert "Проверить выписку сейчас" in response.text


async def test_orders_page_shows_exact_amount(client, session, panel):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=9502, username="exact")
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="manual")
    await session.commit()

    await login(client)
    response = await client.get("/admin/orders")

    assert order.pay_amount_text in response.text
    assert "." in order.pay_amount_text  # копейки для автосверки
