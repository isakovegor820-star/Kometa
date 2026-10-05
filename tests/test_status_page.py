"""Тесты публичной страницы состояния сервиса /status.

Смысл страницы: клиент проверяет, жив ли сервис, когда Telegram тормозит
или недоступен. Секретов и адресов нод на странице быть не должно.
"""

from __future__ import annotations

import httpx
import pytest

from app.panels.registry import registry
from app.web.sub import build_app


@pytest.fixture
async def client(session, panel, monkeypatch):
    async def fake_all_panels(session):  # noqa: ANN001
        return [panel]

    monkeypatch.setattr(registry, "all_panels", fake_all_panels)
    app = await build_app(bot=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_status_page_renders(client):
    response = await client.get("/status")

    assert response.status_code == 200
    assert "состояние сервиса" in response.text.lower()
    assert "Всё работает" in response.text


async def test_status_json(client):
    response = await client.get("/status", params={"format": "json"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["subscriptions_available"] is True
    assert payload["nodes"][0]["title"] == "fake"
    assert "payments" in payload
    assert payload["checked_at"]


async def test_status_does_not_leak_secrets(client):
    response = await client.get("/status")
    body = response.text

    for secret in ("PANEL_TOKEN", "BOT_TOKEN", "127.0.0.1", "password", "token"):
        assert secret.lower() not in body.lower()


async def test_status_reports_problem_when_panel_down(client, monkeypatch, panel):
    async def broken_health() -> bool:
        return False

    monkeypatch.setattr(panel, "health", broken_health)
    # сбрасываем кэш состояния, иначе вернётся прошлый ответ
    from app.web import sub

    sub._STATUS_CACHE["at"] = 0.0
    sub._STATUS_CACHE["value"] = None

    response = await client.get("/status", params={"format": "json"})

    assert response.json()["ok"] is False
    assert "Есть проблемы" in (await client.get("/status")).text


async def test_status_page_is_public(client):
    """Страница должна открываться без авторизации — иначе она бесполезна."""
    response = await client.get("/status")
    assert response.status_code == 200
