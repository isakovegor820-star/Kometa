"""Тесты ролей и безопасности панели.

Смысл: панель теперь общий инструмент на несколько человек. Проверяем, что
роль реально ограничивает действия, что при отсутствии пароля панель ничего не
делает (fail-closed), и что каждое действие попадает в журнал с автором.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.models import AdminAccount, Event
from app.services import orders, subscriptions
from app.web import security
from app.web.sub import build_app

OWNER_PASSWORD = "test-owner-password"
settings = get_settings()


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(settings, "admin_panel_password", OWNER_PASSWORD)
    monkeypatch.setattr(settings, "admin_panel_secret", "test-secret")
    monkeypatch.setattr(settings, "admin_local_only", True)
    monkeypatch.setattr(settings, "admin_allowed_ips", "")
    security.login_throttle._attempts.clear()
    yield


@pytest.fixture
async def client(session):
    app = await build_app(bot=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", follow_redirects=False) as c:
        yield c


async def login_owner(client: httpx.AsyncClient) -> None:
    response = await client.post("/admin/login", data={"password": OWNER_PASSWORD})
    assert response.status_code == 303
    assert security.COOKIE_NAME in response.cookies


async def make_account(session, login: str, password: str, role: str):
    account = AdminAccount(
        login=login,
        display_name=login.capitalize(),
        role=role,
        password_hash=security.hash_password(password),
    )
    session.add(account)
    await session.commit()
    return account


async def test_owner_enters_with_env_password(client):
    await login_owner(client)
    page = await client.get("/admin")
    assert page.status_code == 200
    assert "Владелец" in page.text  # роль видна в подписи пользователя


async def test_login_redirects_when_password_absent(client, monkeypatch, session):
    """Fail-closed: без пароля панель не выполняет действий, а зовёт настроить вход."""
    monkeypatch.setattr(settings, "admin_panel_password", "")

    page = await client.get("/admin")
    assert page.status_code == 303
    assert page.headers["location"] == "/admin/login"

    action = await client.post("/admin/orders/1/confirm")
    assert action.status_code == 303
    assert action.headers["location"] == "/admin/login"

    login_page = await client.get("/admin/login")
    assert login_page.status_code == 200
    assert "Панель выключена" in login_page.text


async def test_account_login_and_role_limits_actions(client, session, panel):
    """Поддержка смотрит, но не подтверждает оплаты; модератор подтверждает."""
    await make_account(session, "support1", "support-pass", "support")
    await make_account(session, "moder1", "moder-pass", "moderator")

    user, _ = await subscriptions.get_or_create_user(session, tg_id=7701, username="rolecheck")
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="manual")
    await session.commit()

    # Поддержка: вход работает, список клиентов открывается, действие — нет.
    support = await client.post("/admin/login", data={"login": "support1", "password": "support-pass"})
    assert support.status_code == 303
    assert (await client.get("/admin/users")).status_code == 200

    denied = await client.post(f"/admin/orders/{order.id}/confirm")
    assert denied.status_code == 403
    assert "Недостаточно прав" in denied.text

    await session.refresh(order)
    assert order.status == "pending"  # ничего не изменилось

    # Модератор: подтверждает оплату.
    await client.post("/admin/login", data={"login": "moder1", "password": "moder-pass"})
    confirmed = await client.post(f"/admin/orders/{order.id}/confirm")
    assert confirmed.status_code == 303

    await session.refresh(order)
    assert order.status == "paid"

    # Но управление командой модератору недоступно.
    team = await client.get("/admin/team")
    assert team.status_code == 403


async def test_wrong_password_for_account_does_not_login(client, session):
    await make_account(session, "moder2", "right-pass", "moderator")

    response = await client.post("/admin/login", data={"login": "moder2", "password": "wrong"})
    assert response.status_code == 303
    assert "error" in response.headers["location"]

    page = await client.get("/admin")
    assert page.status_code == 303


async def test_disabled_account_cannot_login(client, session):
    account = await make_account(session, "fired", "old-pass", "moderator")
    account.is_active = False
    await session.commit()

    response = await client.post("/admin/login", data={"login": "fired", "password": "old-pass"})
    assert response.status_code == 303
    assert "error" in response.headers["location"]


async def test_actions_are_written_to_audit_log(client, session, panel):
    """Кто подтвердил оплату — видно в журнале: имя, роль, источник, клиент."""
    await login_owner(client)
    user, _ = await subscriptions.get_or_create_user(session, tg_id=7702, username="audited")
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="manual")
    await session.commit()

    await client.post(f"/admin/orders/{order.id}/confirm")

    event = await session.scalar(
        select(Event).where(Event.kind == "admin.order_confirm").order_by(Event.id.desc())
    )
    assert event is not None
    assert event.actor_name == "владелец"
    assert event.actor_role == "owner"
    assert event.source == "web"
    assert event.user_id == user.id
    assert f'"order_id": {order.id}' in (event.payload or "")


async def test_audit_page_is_owner_only(client, session, panel):
    """Журнал действий: владелец видит, поддержка — нет."""
    await make_account(session, "support2", "support-pass", "support")
    await client.post("/admin/login", data={"login": "support2", "password": "support-pass"})
    denied = await client.get("/admin/audit")
    assert denied.status_code == 403

    await client.post("/admin/logout")
    await login_owner(client)
    allowed = await client.get("/admin/audit")
    assert allowed.status_code == 200


async def test_failed_login_is_logged(client, session):
    await client.post("/admin/login", data={"password": "definitely-wrong"})
    event = await session.scalar(select(Event).where(Event.kind == "admin.login_failed").order_by(Event.id.desc()))
    assert event is not None


async def test_cross_site_post_is_rejected(client):
    """POST с чужого сайта не проходит, даже если cookie украдена."""
    await login_owner(client)
    response = await client.post(
        "/admin/broadcast",
        data={"text": "привет", "audience": "all"},
        headers={"origin": "http://evil.example", "referer": "http://evil.example/form"},
    )
    assert response.status_code == 403


async def test_session_carries_role_and_name():
    token = security.issue_session(name="Модератор Пётр", role="moderator", account_id=5)
    session = security.read_session(token)
    assert session is not None
    assert session.name == "Модератор Пётр"
    assert session.role == "moderator"
    assert session.account_id == 5
    assert session.role_label == "Модератор"


async def test_expired_session_is_rejected():
    token = security.issue_session(now=0)  # срок истёк сразу
    assert security.read_session(token) is None


async def test_tampered_cookie_is_rejected():
    token = security.issue_session(name="владелец", role="owner")
    payload, encoded, signature = token.split(".", 2)
    forged = f"{payload}.{encoded}.{'0' * len(signature)}"
    assert security.read_session(forged) is None


async def test_owner_can_create_team_account(client, session):
    """Владелец заводит учётку модератора, и она сразу работает."""
    await login_owner(client)
    created = await client.post(
        "/admin/team",
        data={"login": "newmoder", "display_name": "Новый модер", "role": "moderator", "password": "strong-pass-1"},
    )
    assert created.status_code == 303

    account = await session.scalar(select(AdminAccount).where(AdminAccount.login == "newmoder"))
    assert account is not None and account.role == "moderator"
    assert account.password_hash and "strong-pass-1" not in account.password_hash

    await client.post("/admin/logout")
    login = await client.post("/admin/login", data={"login": "newmoder", "password": "strong-pass-1"})
    assert login.status_code == 303
    assert (await client.get("/admin")).status_code == 200


async def test_last_owner_cannot_be_demoted(client, session):
    """Панель не должна остаться без владельца: последнего не понижают."""
    await login_owner(client)
    owner = await make_account(session, "chief", "chief-pass", "owner")

    response = await client.post(f"/admin/team/{owner.id}", data={"role": "support", "is_active": "1"})
    assert response.status_code == 303

    await session.refresh(owner)
    assert owner.role == "owner"
