"""Тесты публичной страницы состояния сервиса /status.

Смысл страницы: клиент проверяет, жив ли сервис, когда Telegram тормозит
или недоступен. Секретов и адресов нод на странице быть не должно.
"""

from __future__ import annotations

import httpx
import pytest

from app.panels.registry import registry
from app.web.sub import build_app


@pytest.fixture(autouse=True)
def fresh_status_cache():
    """Кэш состояния сервиса — процессный; между тестами его надо сбрасывать.

    Иначе тест видит ответ, посчитанный предыдущим тестом, и проверяет не то,
    что думает: ровно так «/status» и начал врать в жизни — кэш на минуту плюс
    подмена смысла поля.
    """
    from app.web import sub

    sub._STATUS_CACHE["at"] = 0.0
    sub._STATUS_CACHE["value"] = None
    yield
    sub._STATUS_CACHE["at"] = 0.0
    sub._STATUS_CACHE["value"] = None


@pytest.fixture
async def client(session, panel, monkeypatch):
    """Клиент страницы: одна «основная» панель и ни одной ноды из БД.

    Патчим именно ``all_panels_with_nodes`` (а не ``all_panels``): страница
    состояния берёт локации оттуда, потому что для каждой нужен её канал и
    поля пробы. Со заглушкой только на ``all_panels`` тест читал бы ноды из
    базы и зависел от того, что оставил предыдущий тест.
    """
    async def fake_pairs(session):  # noqa: ANN001
        return [(None, panel)]

    async def fake_all_panels(session):  # noqa: ANN001
        return [panel]

    monkeypatch.setattr(registry, "all_panels_with_nodes", fake_pairs)
    monkeypatch.setattr(registry, "all_panels", fake_all_panels)
    app = await build_app(bot=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_status_page_renders(client):
    """Свежий сервис без замеров не имеет права говорить «всё работает».

    Раньше здесь стояло «Всё работает» — страница утверждала благополучие,
    которого не измеряла: панель-заглушка отвечает, а порт клиента никто не
    проверял. «Работает» — это результат проверки, а не отсутствие плохих
    новостей.
    """
    response = await client.get("/status")

    assert response.status_code == 200
    assert "состояние сервиса" in response.text.lower()
    assert "Проверяем" in response.text
    assert "Всё работает" not in response.text


async def test_status_json(client):
    response = await client.get("/status", params={"format": "json"})

    assert response.status_code == 200
    payload = response.json()
    # Локация ещё не измерена, значит «всё хорошо» утверждать нечем: JSON отдаёт
    # три состояния (panel / probe / ok), и по умолчанию честно говорит «нет».
    assert payload["ok"] is False
    assert payload["subscriptions_available"] is True
    assert payload["nodes"][0]["panel"] is True
    assert payload["nodes"][0]["probe"] == "unknown"
    # Показываем страну или название локации, а не служебное имя панели:
    # у всех xui-панелей name одинаковый («xui»), и клиент не мог понять, где что.
    from app.config import get_settings

    assert payload["nodes"][0]["title"] == (get_settings().location_title or "fake")
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
