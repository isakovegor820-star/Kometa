"""Компенсация простоя в веб-панели: открыть, закрыть, начислить вручную.

Проверяем последствия, а не вёрстку: период в базе, продлённые подписки,
записи в журнале и то, что поддержке этот раздел недоступен (это деньги).
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.models import AdminAccount, Downtime, Event
from app.services import subscriptions
from app.web import security
from app.web.sub import build_app

PASSWORD = "test-admin-password"
settings = get_settings()


@pytest.fixture(autouse=True)
def admin_env(monkeypatch):
    monkeypatch.setattr(settings, "admin_panel_password", PASSWORD)
    monkeypatch.setattr(settings, "admin_panel_secret", "test-secret")
    security.login_throttle._attempts.clear()
    yield


@pytest.fixture
async def client(session):
    app = await build_app(bot=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", follow_redirects=False) as c:
        yield c


async def login_owner(client: httpx.AsyncClient) -> None:
    assert (await client.post("/admin/login", data={"password": PASSWORD})).status_code == 303


async def make_account(session, login: str, password: str, role: str) -> AdminAccount:
    account = AdminAccount(
        login=login,
        display_name=login.capitalize(),
        role=role,
        password_hash=security.hash_password(password),
    )
    session.add(account)
    await session.commit()
    return account


async def test_page_opens_for_owner(client, session):
    await login_owner(client)
    response = await client.get("/admin/downtime")

    assert response.status_code == 200
    assert "Компенсация простоя" in response.text


async def test_start_opens_period(client, session):
    await login_owner(client)
    response = await client.post("/admin/downtime/start", data={"note": "Москва, МТС"})

    assert response.status_code == 303
    period = await session.scalar(select(Downtime).where(Downtime.ended_at.is_(None)))
    assert period is not None
    assert period.note == "Москва, МТС"
    assert period.created_by.startswith("web:")

    actions = (await session.scalars(select(Event).where(Event.kind == "admin.downtime_start"))).all()
    assert len(actions) == 1


async def test_end_extends_active_subscriptions(client, session, panel):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=9101, username="down")
    sub, _ = await subscriptions.start_trial(session, user, panel)
    await session.commit()
    before = sub.expires_at

    await login_owner(client)
    assert (await client.post("/admin/downtime/start", data={"note": "тест"})).status_code == 303

    period = await session.scalar(select(Downtime).where(Downtime.ended_at.is_(None)))
    period.started_at = period.started_at.replace(year=period.started_at.year - 1)
    await session.commit()

    response = await client.post("/admin/downtime/end", data={"days": "4"})
    assert response.status_code == 303

    await session.refresh(sub)
    assert sub.expires_at > before
    await session.refresh(period)
    assert period.days == 4
    assert period.granted_at is not None

    actions = (await session.scalars(select(Event).where(Event.kind == "admin.downtime_end"))).all()
    assert len(actions) == 1


async def test_end_without_period_reports_error(client, session):
    await login_owner(client)
    response = await client.post("/admin/downtime/end", data={})

    assert response.status_code == 303
    assert "downtime" in response.headers["location"]


async def test_manual_grant_creates_history(client, session, panel):
    await login_owner(client)
    response = await client.post("/admin/downtime/grant", data={"days": "3", "note": "жалоба"})

    assert response.status_code == 303
    period = await session.scalar(select(Downtime).order_by(Downtime.id.desc()))
    assert period.days == 3
    assert period.note == "жалоба"
    assert period.granted_at is not None

    actions = (await session.scalars(select(Event).where(Event.kind == "admin.downtime_grant"))).all()
    assert len(actions) == 1


async def test_support_cannot_open_downtime(client, session):
    """Поддержка не распоряжается днями клиентов: раздел только у владельца."""
    await make_account(session, "sup-dt", "sup-pass", "support")
    assert (await client.post("/admin/login", data={"login": "sup-dt", "password": "sup-pass"})).status_code == 303

    page = await client.get("/admin/downtime")
    assert page.status_code == 403
    assert (await client.post("/admin/downtime/start", data={"note": "нельзя"})).status_code == 403

    assert await session.scalar(select(Downtime)) is None
