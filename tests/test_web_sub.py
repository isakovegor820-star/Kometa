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


async def test_clash_client_gets_yaml_with_autoselect(session, patch_registry):
    """Clash-подобные клиенты должны получать YAML с группой авто-выбора."""
    user = await make_user(session, 6101)
    sub, _ = await subscriptions.start_trial(session, user, patch_registry)
    await session.commit()

    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            f"/sub/{sub.subscription_token}", headers={"User-Agent": "clash-verge/1.7.7"}
        )

    assert response.status_code == 200
    assert "yaml" in response.headers["content-type"]
    assert "url-test" in response.text
    assert "proxies:" in response.text


async def test_singbox_client_gets_json_with_urltest(session, patch_registry):
    user = await make_user(session, 6102)
    sub, _ = await subscriptions.start_trial(session, user, patch_registry)
    await session.commit()

    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            f"/sub/{sub.subscription_token}", headers={"User-Agent": "Hiddify/2.0.5"}
        )

    assert response.status_code == 200
    assert "json" in response.headers["content-type"]
    assert '"urltest"' in response.text
    assert '"outbounds"' in response.text


async def test_format_can_be_requested_explicitly(session, patch_registry):
    user = await make_user(session, 6103)
    sub, _ = await subscriptions.start_trial(session, user, patch_registry)
    await session.commit()

    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        clash = await client.get(f"/sub/{sub.subscription_token}", params={"format": "clash"})
        singbox = await client.get(f"/sub/{sub.subscription_token}", params={"format": "singbox"})

    assert "url-test" in clash.text
    assert '"urltest"' in singbox.text


async def test_unknown_client_gets_base64_as_before(session, patch_registry):
    """v2rayNG и прочие ждут привычный base64-список — не ломаем их."""
    user = await make_user(session, 6104)
    sub, _ = await subscriptions.start_trial(session, user, patch_registry)
    await session.commit()

    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            f"/sub/{sub.subscription_token}", headers={"User-Agent": "v2rayNG/1.9.16"}
        )

    assert response.status_code == 200
    decoded = base64.b64decode(response.text).decode()
    assert decoded.startswith("vless://")
