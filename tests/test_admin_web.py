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
    assert str(order.amount_rub) in response.text


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
    assert f"{order.discount_rub} ₽" in page  # сумма выданных скидок


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


# ---------------------------------------------------- ноды: ID инбаундов
async def test_node_form_rejects_non_numeric_inbound_ids(client, session):
    """Опечатка в ID не должна молча расширять выдачу на все инбаунды.

    Пустое поле означает «все инбаунды панели», поэтому «3x» нельзя сохранять
    как пустую строку: оператор будет уверен, что ограничил ноду одним
    инбаундом, а клиенты пойдут во все.
    """
    from sqlalchemy import select

    from app.db.models import Node

    await login(client)
    response = await client.post(
        "/admin/nodes",
        data={
            "code": "nl",
            "title": "🇳🇱 Нидерланды",
            "host": "203.0.113.5",
            "panel_url": "http://203.0.113.5:2053",
            "panel_token": "secret-token",
            "inbound_ids": "3x",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert await session.scalar(select(Node).where(Node.code == "nl")) is None, "нода с опечаткой не сохраняется"

    page = await client.get("/admin/nodes")
    assert "должны быть числами" in page.text


async def test_node_form_normalizes_inbound_ids(client, session):
    """«1 2» — это два инбаунда, а не двенадцатый (раньше строки склеивались)."""
    from sqlalchemy import select

    from app.db.models import Node

    await login(client)
    response = await client.post(
        "/admin/nodes",
        data={
            "code": "nl",
            "title": "🇳🇱 Нидерланды",
            "host": "203.0.113.5",
            "panel_url": "http://203.0.113.5:2053",
            "panel_token": "secret-token",
            "inbound_ids": "1 2",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    saved = await session.scalar(select(Node).where(Node.code == "nl"))
    assert saved is not None and saved.inbound_ids == "1,2"


async def test_nodes_page_shows_actual_ids_and_honest_probe_state(client, session, monkeypatch):
    """Страница нод: видно фактические ID панели и почему проба не состоялась.

    До правок карточка говорила «панель не ответила», хотя панель ответила и
    объяснила причину, а строка ноды показывала «порт не пускает» там, где порт
    не проверяли.
    """
    from datetime import datetime, timezone

    from app.db.models import Node
    from app.panels.registry import registry
    from app.web.admin import infra
    from tests.test_panel_xui import FakeXui, _vless_inbound, _wireguard_inbound, make_panel

    node = Node(
        code="nl",
        title="🇳🇱 Нидерланды",
        host="203.0.113.5",
        panel_type="xui",
        panel_url="http://203.0.113.5:2053/nl",
        panel_token="secret-token",
        inbound_ids="3",
        is_active=True,
        last_check_at=datetime.now(timezone.utc),
        last_check_ok=False,
        last_check_error="в панели не найдены инбаунды [3] (проверь inbound_ids)",
        last_probe_at=datetime.now(timezone.utc),
        last_probe_ok=False,
        last_probe_ms=0,
        last_probe_error="панель не отдала инбаунды, порт не проверялся: в панели не найдены инбаунды [3]",
    )
    session.add(node)
    await session.commit()

    fake = FakeXui(inbounds=[_vless_inbound(1), _wireguard_inbound(2)])
    panel, http_client = make_panel(fake, inbound_ids=[3])
    monkeypatch.setattr(registry, "primary", lambda: panel)
    monkeypatch.setattr(registry, "for_node", lambda _node: panel)
    infra._inbounds_cache.clear()

    await login(client)
    try:
        response = await client.get("/admin/nodes")
    finally:
        await http_client.aclose()
        infra._inbounds_cache.clear()

    assert response.status_code == 200
    assert "нет ID 3" in response.text, "карточка панели должна называть отсутствующий ID"
    assert "DE-Reality" in response.text, "фактические инбаунды панели должны быть видны"
    assert "отвечает с ошибкой" in response.text, "бейдж не должен врать про «отвечает»"
    assert "проба не выполнена" in response.text
    assert "порт не пускает" not in response.text

    # На дашборде та же нода: причина видна и там, а не только «нет ответа».
    dashboard = await client.get("/admin")
    assert dashboard.status_code == 200
    assert "отвечает с ошибкой" in dashboard.text


async def test_deleting_node_closes_all_its_alerts(client, session):
    """Удаление ноды закрывает все её алерты, а не только «не отвечает»."""
    from sqlalchemy import select

    from app.db.models import Node
    from app.services import alerts as alerts_service

    node = Node(code="nl", title="🇳🇱 Нидерланды", panel_type="fake", is_active=True)
    session.add(node)
    await session.commit()

    for suffix, kind in (
        ("", "node_down"),
        (":inbounds", "node_degraded"),
        (":panel", "panel_error"),
        (":probe", "node_probe_failed"),
    ):
        await alerts_service.raise_alert(session, kind, fingerprint=f"node:nl{suffix}", node_id=node.id)
    await session.commit()
    assert int(await alerts_service.count_alerts(session, "open")) == 4

    await login(client)
    response = await client.post(f"/admin/nodes/{node.id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert await session.scalar(select(Node).where(Node.code == "nl")) is None
    assert int(await alerts_service.count_alerts(session, "open")) == 0
    assert int(await alerts_service.count_alerts(session, "resolved")) == 4


async def test_disabling_node_by_form_closes_its_alerts(client, session):
    """Галочка «Включена» в форме — тот же выключатель, что кнопка в списке.

    Выключенную ноду никто не проверяет, поэтому автозакрытие для неё уже не
    сработает: без явного закрытия алерты («нет инбаундов», «порт не пускает»)
    висят в «Открытых» вечно.
    """
    from sqlalchemy import select

    from app.db.models import Node
    from app.services import alerts as alerts_service

    node = Node(code="nl", title="🇳🇱 Нидерланды", panel_type="fake", is_active=True)
    session.add(node)
    await session.commit()
    for suffix, kind in (
        ("", "node_down"),
        (":inbounds", "node_degraded"),
        (":panel", "panel_error"),
        (":probe", "node_probe_failed"),
    ):
        await alerts_service.raise_alert(session, kind, fingerprint=f"node:nl{suffix}", node_id=node.id)
    await session.commit()

    await login(client)
    response = await client.post(
        "/admin/nodes",
        data={
            "code": "nl",
            "title": "🇳🇱 Нидерланды",
            "host": "203.0.113.5",
            "panel_url": "http://203.0.113.5:2053",
            "panel_token": "secret-token",
            "inbound_ids": "1",
            "is_active": "0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    saved = await session.scalar(select(Node).where(Node.code == "nl"))
    assert saved is not None
    await session.refresh(saved)
    assert saved.is_active is False
    assert int(await alerts_service.count_alerts(session, "open")) == 0
    assert int(await alerts_service.count_alerts(session, "resolved")) == 4


async def test_nodes_check_reports_not_ready_panel(client, session, monkeypatch):
    """«Проверить ноды» не должна говорить «все панели отвечают», если выдача сломана."""
    from app.db.models import Node
    from app.panels.registry import registry
    from app.services import alerts as alerts_service
    from tests.test_node_inbound_diagnosis import ReasonPanel, missing_inbound_error

    session.add(
        Node(
            code="nl",
            title="🇳🇱 Нидерланды",
            host="203.0.113.5",
            panel_type="fake",
            is_active=True,
            inbound_ids="3",
        )
    )
    await session.commit()

    primary = ReasonPanel()
    primary.location_title = "🇩🇪 Германия"
    broken = ReasonPanel(inbound_error=missing_inbound_error())
    broken.location_title = "🇳🇱 Нидерланды"
    monkeypatch.setattr(registry, "primary", lambda: primary)
    monkeypatch.setattr(registry, "for_node", lambda _node: broken)

    await login(client)
    response = await client.post("/admin/nodes/check", follow_redirects=False)
    assert response.status_code == 303

    page = await client.get("/admin/nodes")
    assert "Готовы 1 из 2" in page.text, "flash не должен обещать «все панели готовы»"
    assert "nl" in page.text

    opened = await alerts_service.list_alerts(session, status="open")
    assert [alert.kind for alert in opened] == ["node_degraded"]
