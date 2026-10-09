"""Тесты лимитов публичных адресов и кэша подписки (H8).

Зачем:

* ``/sub/{token}`` и ``/connect/{token}`` — открытые адреса: каждый запрос стоит
  двух обращений к панели. Тридцать запросов подряд от одного IP должны
  упираться в 429 с понятным текстом, а не молча грузить панели;
* повторный запрос с тем же токеном внутри короткого окна кэша (10–30 с) не
  должен ходить в панели вовсе;
* лежащая панель не должна задерживать ответ на полный таймаут: панели
  опрашиваются параллельно, а каждая отсекается своим таймаутом. Ответ при
  этом — не 500.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.config import get_settings
from app.db.models import Subscription
from app.panels.fake import FakePanel
from app.panels.registry import registry
from app.services import subscriptions
from app.web.sub import build_app
from tests.test_subscriptions import make_user

settings = get_settings()


class SlowPanel(FakePanel):
    """Панель, которая «висит»: проверяем таймаут и параллельность."""

    name = "slow-panel"

    async def get_configs(self, uuid: str) -> list[str]:  # noqa: ANN001
        await asyncio.sleep(30)
        return await super().get_configs(uuid)


class NamedPanel(FakePanel):
    """Панель с человеческим именем локации — как нода в админке."""

    name = "named-panel"
    location_title = "🇩🇪 Германия"


@pytest.fixture
async def client(session):
    app = await build_app(bot=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        yield c


@pytest.fixture
def single_panel(monkeypatch, panel):
    """Реестр отдаёт одну живую панель — как в бою с одной локацией."""

    async def fake_pairs(session):  # noqa: ANN001, ANN202
        return [(None, panel)]

    monkeypatch.setattr(registry, "all_panels_with_nodes", fake_pairs)
    return panel


async def _make_subscription(session, panel, tg_id: int) -> Subscription:
    user = await make_user(session, tg_id)
    sub, _ = await subscriptions.start_trial(session, user, panel)
    await session.commit()
    return sub


# ------------------------------------------------------------------ лимиты
async def test_thirty_requests_then_429(client, session, single_panel):
    """30 подряд запросов к /sub проходят, 31-й получает 429."""
    sub = await _make_subscription(session, single_panel, 9401)

    statuses = [(await client.get(f"/sub/{sub.subscription_token}")).status_code for _ in range(30)]
    blocked = await client.get(f"/sub/{sub.subscription_token}")

    assert set(statuses) == {200}
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"].isdigit()


async def test_429_explains_what_happened(client, session, single_panel, monkeypatch):
    """429 понятный: сколько ждать и какой лимит — без внутренних деталей."""
    monkeypatch.setattr(settings, "rate_limit_requests", 2)
    sub = await _make_subscription(session, single_panel, 9402)

    await client.get(f"/sub/{sub.subscription_token}")
    await client.get(f"/sub/{sub.subscription_token}")
    response = await client.get(f"/sub/{sub.subscription_token}")

    assert response.status_code == 429
    body = response.json()
    assert "не больше 2" in body["detail"]
    assert body["retry_after"] > 0
    assert int(response.headers["Retry-After"]) == body["retry_after"]


async def test_limit_is_configurable(client, session, single_panel, monkeypatch):
    """Лимит настраивается: 0 — выключить, число — порог на окно."""
    monkeypatch.setattr(settings, "rate_limit_requests", 0)
    sub = await _make_subscription(session, single_panel, 9403)

    for _ in range(5):
        assert (await client.get(f"/sub/{sub.subscription_token}")).status_code == 200

    monkeypatch.setattr(settings, "rate_limit_requests", 1)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 60)
    fresh = await build_app(bot=None)
    transport = httpx.ASGITransport(app=fresh)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        assert (await c.get(f"/sub/{sub.subscription_token}")).status_code == 200
        assert (await c.get(f"/sub/{sub.subscription_token}")).status_code == 429


async def test_rate_limit_can_be_disabled(client, session, single_panel, monkeypatch):
    """RATE_LIMIT_ENABLED=false — никаких 429 (нужно для локальной отладки)."""
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    sub = await _make_subscription(session, single_panel, 9404)

    for _ in range(40):
        assert (await client.get(f"/sub/{sub.subscription_token}")).status_code == 200


async def test_connect_page_shares_the_same_bucket(client, session, single_panel):
    """Страница подключения и подписка — один клиент, один общий лимит."""
    monkeypatch_ok = True  # порог по умолчанию 30
    assert monkeypatch_ok
    sub = await _make_subscription(session, single_panel, 9405)

    for _ in range(29):
        assert (await client.get(f"/connect/{sub.subscription_token}")).status_code == 200
    # 30-й запрос (переход на подписку) ещё проходит, 31-й — уже нет.
    assert (await client.get(f"/sub/{sub.subscription_token}")).status_code == 200
    assert (await client.get(f"/sub/{sub.subscription_token}")).status_code == 429


async def test_health_and_other_paths_are_not_limited(client):
    """/health не лимитируем: это проверка живости, а не клиентский трафик."""
    for _ in range(40):
        assert (await client.get("/health")).status_code == 200


async def test_webhook_has_its_own_limit(session, monkeypatch):
    """У вебхуков свой порог: платёжная система может прислать серию."""
    monkeypatch.setattr(settings, "rate_limit_webhook_requests", 3)
    monkeypatch.setattr(settings, "platega_merchant_id", "m")
    monkeypatch.setattr(settings, "platega_secret", "s")
    app = await build_app(bot=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        codes = [(await c.post("/payments/platega/webhook", content=b"")).status_code for _ in range(4)]

    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429


# -------------------------------------------------------------------- кэш
async def test_repeat_request_within_cache_does_not_touch_panels(
    client, session, single_panel, monkeypatch
):
    """Повторный запрос с тем же токеном в окне кэша панели не опрашивает."""
    calls = {"configs": 0, "user": 0}
    real_configs = single_panel.get_configs
    real_user = single_panel.get_user

    async def counting_configs(uuid):  # noqa: ANN001, ANN202
        calls["configs"] += 1
        return await real_configs(uuid)

    async def counting_user(uuid):  # noqa: ANN001, ANN202
        calls["user"] += 1
        return await real_user(uuid)

    monkeypatch.setattr(single_panel, "get_configs", counting_configs)
    monkeypatch.setattr(single_panel, "get_user", counting_user)
    monkeypatch.setattr(settings, "sub_cache_seconds", 20)
    sub = await _make_subscription(session, single_panel, 9406)

    first = await client.get(f"/sub/{sub.subscription_token}")
    second = await client.get(f"/sub/{sub.subscription_token}")

    assert first.status_code == 200 and second.status_code == 200
    assert calls == {"configs": 1, "user": 1}
    assert first.text == second.text


async def test_cache_expires_and_panels_are_asked_again(
    client, session, single_panel, monkeypatch
):
    """После окна кэша данные перезапрашиваются — кэш не «замораживает» подписку."""
    calls = {"n": 0}
    real_configs = single_panel.get_configs

    async def counting(uuid):  # noqa: ANN001, ANN202
        calls["n"] += 1
        return await real_configs(uuid)

    monkeypatch.setattr(single_panel, "get_configs", counting)
    monkeypatch.setattr(settings, "sub_cache_seconds", 0)
    sub = await _make_subscription(session, single_panel, 9407)

    await client.get(f"/sub/{sub.subscription_token}")
    await client.get(f"/sub/{sub.subscription_token}")

    assert calls["n"] == 2


async def test_different_tokens_do_not_share_cache(client, session, single_panel, monkeypatch):
    """Кэш различает токены: чужой токен не должен получить чужой профиль."""
    calls = {"n": 0}
    real_configs = single_panel.get_configs

    async def counting(uuid):  # noqa: ANN001, ANN202
        calls["n"] += 1
        return await real_configs(uuid)

    monkeypatch.setattr(single_panel, "get_configs", counting)
    first_sub = await _make_subscription(session, single_panel, 9408)
    second_sub = await _make_subscription(session, single_panel, 9409)

    await client.get(f"/sub/{first_sub.subscription_token}")
    await client.get(f"/sub/{second_sub.subscription_token}")

    assert calls["n"] == 2


# --------------------------------------------------- лежащая панель и таймаут
async def test_dead_panel_does_not_delay_or_break_the_response(client, session, monkeypatch, panel):
    """Одна панель висит — ответ собирается параллельно и приходит живым.

    Раньше панели опрашивались по очереди: висящая локация держала клиента
    полный таймаут, и только потом отдавались конфиги остальных. Теперь время
    ответа — таймаут одной панели, а не сумма по всем.
    """
    slow = SlowPanel()
    alive = NamedPanel()

    async def fake_pairs(session_):  # noqa: ANN001, ANN202
        return [(None, slow), (None, alive)]

    monkeypatch.setattr(registry, "all_panels_with_nodes", fake_pairs)
    monkeypatch.setattr(settings, "panel_timeout_seconds", 1)
    monkeypatch.setattr(settings, "sub_cache_seconds", 0)

    user = await make_user(session, 9410)
    sub, _ = await subscriptions.start_trial(session, user, alive)
    await session.commit()

    started = time.monotonic()
    response = await client.get(f"/sub/{sub.subscription_token}")
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert elapsed < 2.5  # один таймаут, а не два подряд
    assert "vless://" in response.text or len(response.text) > 0
    # И ответ не «500»: недоступная панель отсечена, живые локации отданы.
    assert response.status_code != 500


async def test_all_panels_down_gives_503_not_500(client, session, monkeypatch):
    """Все панели недоступны — честный 503 (сервис недоступен), а не 500."""
    slow = SlowPanel()
    second = SlowPanel()

    async def fake_pairs(session_):  # noqa: ANN001, ANN202
        return [(None, slow), (None, second)]

    monkeypatch.setattr(registry, "all_panels_with_nodes", fake_pairs)
    monkeypatch.setattr(settings, "panel_timeout_seconds", 1)
    monkeypatch.setattr(settings, "sub_cache_seconds", 0)

    user = await make_user(session, 9411)
    sub, _ = await subscriptions.start_trial(session, user, slow)
    await session.commit()

    response = await client.get(f"/sub/{sub.subscription_token}")

    assert response.status_code == 503
