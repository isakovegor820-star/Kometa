"""Тесты «действий без перезагрузки»: панель отвечает JSON-ом на ajax-запросы.

Смысл: тост и обновление списка на месте не должны ломать обычный сценарий без
JS. Поэтому проверяем обе ветки: с заголовком X-Panel-Ajax действие отдаёт
словарь, без заголовка — прежний редирект с сообщением в cookie. И отдельно —
что анонимный ajax-запрос не превращается в утечку данных.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.services import subscriptions
from app.web import security
from app.web.sub import build_app

PASSWORD = "test-admin-password"
settings = get_settings()
AJAX = {"X-Panel-Ajax": "1"}


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


async def login(client: httpx.AsyncClient) -> None:
    response = await client.post("/admin/login", data={"password": PASSWORD})
    assert response.status_code == 303


async def test_ajax_action_answers_json(client, session):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=990011, username="ajaxuser")
    await session.commit()
    await login(client)

    response = await client.post(f"/admin/users/{user.id}/grant", data={"days": "5"}, headers=AJAX)
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert "5" in payload["message"]


async def test_ajax_action_returns_error_as_json(client, session):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=990022, username="ajaxfail")
    await session.commit()
    await login(client)

    response = await client.post(f"/admin/users/{user.id}/grant", data={"days": "0"}, headers=AJAX)
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]


async def test_plain_post_still_redirects(client, session):
    """Без заголовка поведение прежнее: 303 и сообщение в cookie."""
    user, _ = await subscriptions.get_or_create_user(session, tg_id=990033, username="plainuser")
    await session.commit()
    await login(client)

    response = await client.post(f"/admin/users/{user.id}/grant", data={"days": "3"})
    assert response.status_code == 303
    assert response.cookies.get("kometa_flash"), "сообщение должно уехать в flash-cookie"
    assert response.headers.get("location", "").startswith("/admin/users")


async def test_ajax_without_session_is_not_json(client):
    response = await client.post("/admin/users/1/grant", data={"days": "3"}, headers=AJAX)
    assert response.status_code in (303, 307)
    assert "/admin/login" in response.headers.get("location", "")
