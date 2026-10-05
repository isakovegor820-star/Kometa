"""Тесты веб-админ-панели: вход, защита, действия.

Проверяем через httpx.ASGITransport — реальный ASGI-стек без сети.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.db.models import Order
from app.services import orders, subscriptions
from app.web import security
from app.web.sub import build_app
from urllib.parse import unquote

PASSWORD = "test-admin-password"


@pytest.fixture(autouse=True)
def admin_password(monkeypatch):
    """Задаём пароль панели на время теста (в .env он пустой)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_panel_password", PASSWORD)
    monkeypatch.setattr(settings, "admin_panel_secret", "test-secret")
    security.login_throttle._attempts.clear()
    yield


@pytest.fixture
async def client(session):
    """Клиент панели. Зависит от session, чтобы таблицы БД были созданы."""
    app = await build_app(bot=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", follow_redirects=False) as c:
        yield c


async def login(client: httpx.AsyncClient) -> None:
    response = await client.post("/admin/login", data={"password": PASSWORD})
    assert response.status_code == 303
    assert security.COOKIE_NAME in response.cookies


async def test_panel_requires_login(client):
    response = await client.get("/admin")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


async def test_wrong_password_does_not_log_in(client):
    response = await client.post("/admin/login", data={"password": "nope"})
    assert response.status_code == 303
    assert "error" in response.headers["location"]

    page = await client.get("/admin")
    assert page.status_code == 303  # всё ещё не авторизованы


async def test_login_and_dashboard(client):
    await login(client)
    response = await client.get("/admin")

    assert response.status_code == 200
    assert "Дашборд" in response.text
    assert "Заявки на оплату" in response.text


async def test_dashboard_shows_pending_order(client, session):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=8801, username="buyer")
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="manual")
    await session.commit()

    await login(client)
    response = await client.get("/admin")

    assert f"#{order.id}" in response.text
    assert "199" in response.text


async def test_confirm_order_from_panel(client, session, panel):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=8802, username="buyer2")
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="manual")
    await session.commit()

    await login(client)
    response = await client.post(f"/admin/orders/{order.id}/confirm")
    assert response.status_code == 303

    await session.refresh(order)
    assert order.status == "paid"
    sub = await subscriptions.get_subscription(session, user.id)
    assert sub is not None and sub.status == "active"


async def test_reject_order_from_panel(client, session):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=8803, username="buyer3")
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="manual")
    await session.commit()

    await login(client)
    response = await client.post(f"/admin/orders/{order.id}/reject")
    assert response.status_code == 303

    await session.refresh(order)
    assert order.status == "canceled"


async def test_users_page_finds_user_and_shows_link(client, session, panel):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=8804, username="findme")
    sub, _ = await subscriptions.start_trial(session, user, panel)
    await session.commit()

    await login(client)
    response = await client.get("/admin/users", params={"q": "findme"})

    assert response.status_code == 200
    assert "findme" in response.text
    assert sub.subscription_token in response.text


async def test_grant_days_from_panel(client, session, panel):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=8805, username="grantme")
    sub, _ = await subscriptions.start_trial(session, user, panel)
    await session.commit()
    before = sub.days_left

    await login(client)
    response = await client.post(f"/admin/users/{user.id}/grant", data={"days": "10"})
    assert response.status_code == 303

    await session.refresh(sub)
    assert sub.days_left >= before + 9


async def test_block_user_from_panel(client, session, panel):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=8806, username="blockme")
    await subscriptions.start_trial(session, user, panel)
    await session.commit()

    await login(client)
    response = await client.post(f"/admin/users/{user.id}/block", data={"block": "1"})
    assert response.status_code == 303

    await session.refresh(user)
    assert user.is_blocked is True
    sub = await subscriptions.get_subscription(session, user.id)
    assert sub is not None and sub.status == "blocked"


async def test_nodes_page_renders(client, panel, monkeypatch):
    from app.panels.registry import registry

    async def fake_all_panels(session):
        return [panel]

    monkeypatch.setattr(registry, "all_panels", fake_all_panels)
    await login(client)
    response = await client.get("/admin/nodes")

    assert response.status_code == 200
    assert "DE-Reality" in response.text  # инбаунд из заглушки панели


async def test_login_throttle_blocks_bruteforce(client):
    for _ in range(security.MAX_LOGIN_ATTEMPTS):
        await client.post("/admin/login", data={"password": "wrong"})

    response = await client.post("/admin/login", data={"password": PASSWORD})
    location = unquote(response.headers["location"])
    assert "Слишком много попыток" in location
