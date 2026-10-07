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


async def test_referrals_page_shows_program_stats(client, session, panel):
    from app.services import orders, referral

    referrer, _ = await subscriptions.get_or_create_user(session, tg_id=8901, username="inviter")
    invited, _ = await subscriptions.get_or_create_user(session, tg_id=8902, username="friend")
    await subscriptions.start_trial(session, referrer, panel)
    await referral.attach_referrer(session, invited, referrer.referral_code)
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, invited, plan, provider="manual")
    await orders.mark_paid(session, order, panel)
    await session.commit()

    await login(client)
    response = await client.get("/admin/referrals")
    page = response.text

    assert response.status_code == 200
    assert "Рефералы и промокоды" in page
    assert "inviter" in page  # топ пригласивших
    assert "friend" in page  # последнее приглашение
    assert "+30 дн." in page  # награда начислена
    assert "конверсия 100%" in page
    assert "99 ₽" in page  # сумма выданных скидок


async def test_promo_can_be_created_and_disabled_from_panel(client, session):
    from app.services import promo

    await login(client)
    created = await client.post(
        "/admin/referrals/promo",
        data={"code": "launch50", "percent": "50", "uses": "10", "days": "30"},
    )
    assert created.status_code == 303

    row = await promo.get_by_code(session, "LAUNCH50")
    assert row is not None and row.is_active and row.percent == 50

    page = await client.get("/admin/referrals")
    assert "LAUNCH50" in page.text

    off = await client.post(f"/admin/referrals/promo/{row.id}/toggle")
    assert off.status_code == 303

    await session.refresh(row)
    assert row.is_active is False


async def test_admin_can_add_and_toggle_node(client, session):
    """Страну (ноду) можно подключить формой в админке — без SQL и перезапуска.

    Смысл для продукта: чтобы сервис работал и во Владивостоке, новая нода
    должна подключаться за минуту. После сохранения клиенты выдаются и на ней,
    а ссылка-подписка начинает отдавать её локацию.
    """
    from sqlalchemy import select

    from app.db.models import Node
    from app.panels.registry import registry

    await login(client)
    response = await client.post(
        "/admin/nodes",
        data={
            "code": "jp",
            "title": "🇯🇵 Япония",
            "country": "JP",
            "host": "203.0.113.99",
            "panel_url": "http://203.0.113.99:2053/panel",
            "panel_token": "secret-token",
            "inbound_ids": "1,2",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    saved = await session.scalar(select(Node).where(Node.code == "jp"))
    assert saved is not None and saved.is_active is True
    assert saved.inbound_ids == "1,2"

    # нода появилась в списке панелей, с которых собирается подписка
    panels = await registry.all_panels(session)
    assert any(getattr(panel, "base_url", "") == "http://203.0.113.99:2053/panel" for panel in panels)

    # выключенная нода перестаёт участвовать
    toggled = await client.post(f"/admin/nodes/{saved.id}/toggle", follow_redirects=False)
    assert toggled.status_code == 303
    await session.refresh(saved)
    assert saved.is_active is False
    registry.invalidate()
