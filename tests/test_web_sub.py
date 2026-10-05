"""Тесты веб-слоя: ссылка-подписка /sub/<token>."""

from __future__ import annotations

import base64

import httpx
import pytest

from app.panels.registry import registry
from app.services import subscriptions
from app.web.sub import build_app
from tests.test_subscriptions import make_user


@pytest.fixture
def patch_registry(monkeypatch, panel):
    async def fake_all_panels(session):
        return [panel]

    monkeypatch.setattr(registry, "all_panels", fake_all_panels)
    return panel


async def test_health_endpoint():
    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_subscription_returns_base64_configs(session, patch_registry):
    user = await make_user(session, 6001)
    sub, _ = await subscriptions.start_trial(session, user, patch_registry)
    await session.commit()

    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"/sub/{sub.subscription_token}")

    assert response.status_code == 200
    decoded = base64.b64decode(response.text).decode()
    assert "vless://" in decoded
    assert sub.panel_user_uuid in decoded
    assert response.headers["profile-title"] == "Kometa"
    assert "subscription-userinfo" in response.headers


async def test_subscription_plain_format(session, patch_registry):
    user = await make_user(session, 6002)
    sub, _ = await subscriptions.start_trial(session, user, patch_registry)
    await session.commit()

    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"/sub/{sub.subscription_token}?format=plain")

    assert response.status_code == 200
    assert response.text.startswith("vless://")


async def test_subscription_info_endpoint(session, patch_registry):
    user = await make_user(session, 6003)
    sub, _ = await subscriptions.start_trial(session, user, patch_registry)
    await session.commit()

    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"/sub/{sub.subscription_token}/info")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "trial"
    assert payload["devices_limit"] == 1
    assert payload["traffic_limit_gb"] == 10


async def test_unknown_token_returns_404():
    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/sub/does-not-exist")

    assert response.status_code == 404
