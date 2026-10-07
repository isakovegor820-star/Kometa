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


async def test_tcp_only_mode_drops_udp_profiles(session, patch_registry, monkeypatch):
    """Режим «белых списков»: UDP-профили не попадают в подписку.

    Под БС у оператора проходят только TCP 80/443/22, поэтому AmneziaWG в
    подписке — мёртвый профиль: приложение будет долбиться в него и показывать
    «VPN не подключается». Фильтр обязан работать во ВСЕХ форматах, иначе
    base64-список и sing-box разойдутся по составу.
    """
    from app.web import sub as sub_module

    monkeypatch.setattr(sub_module.settings, "subscription_tcp_only", True)

    user = await make_user(session, 6104)
    subscription, _ = await subscriptions.start_trial(session, user, patch_registry)
    await session.commit()

    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        plain = await client.get(f"/sub/{subscription.subscription_token}?format=plain")
        singbox = await client.get(
            f"/sub/{subscription.subscription_token}",
            headers={"User-Agent": "Hiddify/2.0.5"},
        )

    assert plain.status_code == 200
    assert "vless://" in plain.text
    assert "amneziawg://" not in plain.text
    assert "wireguard" not in singbox.text


async def test_tcp_only_mode_keeps_vless(session, patch_registry, monkeypatch):
    """Фильтр не должен выкидывать рабочий TCP-профиль вместе с UDP."""
    from app.web import sub as sub_module

    monkeypatch.setattr(sub_module.settings, "subscription_tcp_only", True)

    user = await make_user(session, 6105)
    subscription, _ = await subscriptions.start_trial(session, user, patch_registry)
    await session.commit()

    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"/sub/{subscription.subscription_token}?format=plain")

    assert response.status_code == 200
    assert response.text.count("vless://") >= 1


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


async def test_profile_title_base64_for_cyrillic(session, patch_registry, monkeypatch):
    """Кириллицу в profile-title отдаём в base64 — иначе Happ показывает адрес сервера."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "subscription_title", "Германия")
    user = await make_user(session, 6007)
    sub, _ = await subscriptions.start_trial(session, user, patch_registry)
    await session.commit()

    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"/sub/{sub.subscription_token}")

    expected = "base64:" + base64.b64encode("Германия".encode()).decode()
    assert response.headers["profile-title"] == expected


async def test_subscription_renames_locations(session, patch_registry, monkeypatch):
    """В подписке локации называются по-человечески, а не «DE-REALITY-firefox-u123»."""
    from urllib.parse import unquote

    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "location_title", "🇩🇪 Германия")
    user = await make_user(session, 6008)
    sub, _ = await subscriptions.start_trial(session, user, patch_registry)
    await session.commit()

    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"/sub/{sub.subscription_token}?format=plain")

    assert "🇩🇪 Германия" in unquote(response.text)


async def test_subscription_header_marks_forever_without_date(session, patch_registry):
    """Бессрочную подписку клиент должен видеть как «без срока», а не как 2099 год."""
    from datetime import datetime, timedelta, timezone

    user = await make_user(session, 6009)
    sub, _ = await subscriptions.start_trial(session, user, patch_registry)
    sub.expires_at = datetime.now(timezone.utc) + timedelta(days=365 * 30)
    await session.commit()

    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"/sub/{sub.subscription_token}")

    assert "expire=0" in response.headers["subscription-userinfo"]


async def test_subscription_header_keeps_real_expiry_date(session, patch_registry):
    """Обычная подписка (3 дня) обязана отдавать настоящую дату окончания."""
    user = await make_user(session, 6010)
    sub, _ = await subscriptions.start_trial(session, user, patch_registry)
    await session.commit()

    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"/sub/{sub.subscription_token}")

    assert "expire=0" not in response.headers["subscription-userinfo"]


async def test_connect_page_opens_apps_by_scheme(session, patch_registry):
    """Страница подключения — единственное место, где живут схемы приложений.

    Telegram запрещает их в кнопках, поэтому кнопка ведёт на https-страницу,
    а страница открывает Happ/v2rayNG/Hiddify и показывает ссылку вручную.
    """
    user = await make_user(session, 6011)
    sub, _ = await subscriptions.start_trial(session, user, patch_registry)
    await session.commit()

    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"/connect/{sub.subscription_token}")

    assert response.status_code == 200
    html = response.text
    sub_url = f"http://testserver/sub/{sub.subscription_token}"
    assert f"happ://add/{sub_url}#Kometa" in html
    assert "v2rayng://install-sub/?url=" in html
    assert f"hiddify://import/{sub_url}#Kometa" in html
    assert sub_url in html  # ссылку видно целиком, если приложение не установлено


async def test_connect_page_preopens_chosen_app(session, patch_registry):
    """?app=happ — сразу пытаемся открыть Happ и подсвечиваем кнопку."""
    user = await make_user(session, 6012)
    sub, _ = await subscriptions.start_trial(session, user, patch_registry)
    await session.commit()

    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"/connect/{sub.subscription_token}?app=happ")

    assert response.status_code == 200
    assert "location.href='happ://add/" in response.text
    assert "primary" in response.text


async def test_connect_page_rejects_unknown_token():
    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/connect/nosuchtoken")

    assert response.status_code == 404
