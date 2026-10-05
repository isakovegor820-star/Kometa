"""Тесты доступа к админ-панели при работе без домена (только IP).

Смысл: панель и ссылки-подписки живут на одном порту. Без HTTPS пускать
панель в открытый интернет нельзя, поэтому по умолчанию она доступна
только с localhost, а подписки — со всех адресов.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.web import security
from app.web.sub import build_app

PASSWORD = "test-admin-password"
settings = get_settings()


@pytest.fixture(autouse=True)
def admin_env(monkeypatch):
    monkeypatch.setattr(settings, "admin_panel_password", PASSWORD)
    monkeypatch.setattr(settings, "admin_panel_secret", "test-secret")
    monkeypatch.setattr(settings, "admin_local_only", True)
    monkeypatch.setattr(settings, "admin_allowed_ips", "")
    security.login_throttle._attempts.clear()
    yield


async def make_client(session, host: str) -> httpx.AsyncClient:  # noqa: ANN001
    app = await build_app(bot=None)
    transport = httpx.ASGITransport(app=app, client=(host, 12345))
    return httpx.AsyncClient(transport=transport, base_url="http://testserver", follow_redirects=False)


async def test_localhost_can_open_panel(session):
    async with await make_client(session, "127.0.0.1") as client:
        response = await client.get("/admin/login")
    assert response.status_code == 200


async def test_foreign_ip_is_blocked(session):
    async with await make_client(session, "203.0.113.7") as client:
        login_page = await client.get("/admin/login")
        login_post = await client.post("/admin/login", data={"password": PASSWORD})
        dashboard = await client.get("/admin")

    assert login_page.status_code == 403
    assert login_post.status_code == 403
    assert dashboard.status_code == 403
    # в тексте отказа должна быть подсказка про SSH-туннель
    assert "SSH-туннель" in login_page.json()["detail"]


async def test_subscriptions_stay_open_for_everyone(session):
    """Ссылка-подписка и /health должны работать с любого адреса."""
    async with await make_client(session, "203.0.113.7") as client:
        health = await client.get("/health")
        subscription = await client.get("/sub/does-not-exist")

    assert health.status_code == 200
    assert subscription.status_code == 404  # токена нет, но доступ не запрещён


async def test_whitelisted_ip_can_open_panel(session, monkeypatch):
    monkeypatch.setattr(settings, "admin_allowed_ips", "203.0.113.7, 198.51.100.1")

    async with await make_client(session, "203.0.113.7") as client:
        allowed = await client.get("/admin/login")
    async with await make_client(session, "192.0.2.55") as client:
        blocked = await client.get("/admin/login")

    assert allowed.status_code == 200
    assert blocked.status_code == 403


async def test_panel_can_be_opened_to_all_if_explicitly_allowed(session, monkeypatch):
    """ADMIN_LOCAL_ONLY=false — осознанный выбор владельца (например, с HTTPS)."""
    monkeypatch.setattr(settings, "admin_local_only", False)

    async with await make_client(session, "203.0.113.7") as client:
        response = await client.get("/admin/login")

    assert response.status_code == 200


async def test_payments_webhook_is_not_restricted(session):
    """Вебхуки платёжек приходят с чужих IP — их блокировать нельзя."""
    async with await make_client(session, "203.0.113.7") as client:
        crypto = await client.post("/payments/crypto/webhook", content=b"{}")
        wata = await client.post("/payments/wata/webhook", content=b"{}")

    assert crypto.status_code == 403  # отказ по подписи, а не по IP
    assert wata.status_code in {403, 503}
