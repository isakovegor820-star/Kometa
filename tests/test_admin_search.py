"""Тесты командной палитры (⌘K): поиск клиентов и заказов + разделы по правам.

Смысл: палитра — это вход в данные, значит она обязана уважать права роли и не
показывать то, что роль всё равно не откроет. Проверяем ровно это: данные
находятся, короткий запрос ничего не отдаёт, анонимный запрос не отдаёт данные,
а список разделов в шаблоне зависит от прав.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.db.models import AdminAccount
from app.services import orders, subscriptions
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


async def login(client: httpx.AsyncClient, password: str = PASSWORD, login_name: str = "") -> None:
    response = await client.post("/admin/login", data={"password": password, "login": login_name})
    assert response.status_code == 303


async def test_search_finds_user_by_name_and_username(client, session):
    await subscriptions.get_or_create_user(session, tg_id=770011, username="paletteuser", first_name="Мария")
    await session.commit()
    await login(client)

    by_name = await client.get("/admin/search", params={"q": "Мария"})
    assert by_name.status_code == 200
    items = by_name.json()["items"]
    assert any(item["kind"] == "user" and "/admin/users/" in item["url"] for item in items)

    by_username = await client.get("/admin/search", params={"q": "paletteuser"})
    assert any("paletteuser" in item["sub"] for item in by_username.json()["items"])

    by_tg = await client.get("/admin/search", params={"q": "770011"})
    assert any("770011" in item["sub"] for item in by_tg.json()["items"])


async def test_search_finds_order_by_id(client, session, panel):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=770022, username="orderuser")
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="manual")
    await session.commit()
    await login(client)

    response = await client.get("/admin/search", params={"q": str(order.id)})
    items = response.json()["items"]
    assert any(item["kind"] == "order" and item["url"] == f"/admin/orders?q={order.id}" for item in items)


async def test_search_short_query_returns_nothing(client, session):
    await subscriptions.get_or_create_user(session, tg_id=770033, username="shortq")
    await session.commit()
    await login(client)

    response = await client.get("/admin/search", params={"q": "a"})
    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_search_requires_login(client):
    response = await client.get("/admin/search", params={"q": "Мария"})
    assert response.status_code in (303, 307)
    assert "/admin/login" in response.headers.get("location", "")


async def test_sections_depend_on_role(client, session):
    """Владелец видит «Команда и роли», поддержка — нет."""
    await login(client)
    owner_page = await client.get("/admin/users")
    assert "Команда и роли" in owner_page.text

    from app.web.security import hash_password

    session.add(
        AdminAccount(
            login="support1",
            display_name="Поддержка",
            password_hash=hash_password("support-password-1"),
            role="support",
            is_active=True,
        )
    )
    await session.commit()

    support = httpx.ASGITransport(app=await build_app(bot=None))
    async with httpx.AsyncClient(transport=support, base_url="http://testserver", follow_redirects=False) as c:
        await login(c, password="support-password-1", login_name="support1")
        page = await c.get("/admin/users")
        assert page.status_code == 200
        assert "Команда и роли" not in page.text
        # Разделы поддержки: клиенты есть, команды и тарифов нет.
        assert "Пользователи" in page.text
