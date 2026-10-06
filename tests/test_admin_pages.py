"""Дымовые тесты страниц панели: каждая открывается и не ломается.

Смысл: страниц стало много, и любая из них может «упасть» из-за одной опечатки
в шаблоне или отсутствующей переменной. Здесь мы просто проходим по всем
разделам под каждой ролью и проверяем, что страница отдаётся, меню не
показывает запретное, а выгрузки работают.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.db.models import AdminAccount
from app.services import orders, subscriptions
from app.web import security, ui
from app.web.sub import build_app

PASSWORD = "test-admin-password"
settings = get_settings()

#: Страницы, доступные владельцу: путь → что должно быть на странице.
OWNER_PAGES: dict[str, str] = {
    "/admin": "Дашборд",
    "/admin/orders": "Очередь заказов",
    "/admin/orders?status=all": "Очередь заказов",
    "/admin/refunds": "Возвраты",
    "/admin/finance": "Финансы и отчёты",
    "/admin/plans": "Тариф",
    "/admin/nodes": "Нод",
    "/admin/alerts": "Алерт",
    "/admin/audit": "Журнал",
    "/admin/referrals": "Рефералы и промокоды",
    "/admin/broadcast": "Рассылки",
    "/admin/team": "Команда",
}

#: Что поддержке нельзя даже открывать (нет права).
SUPPORT_FORBIDDEN = ("/admin/team", "/admin/audit", "/admin/plans", "/admin/broadcast", "/admin/finance", "/admin/refunds")
MODERATOR_FORBIDDEN = ("/admin/team", "/admin/plans", "/admin/broadcast")


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


async def login(client: httpx.AsyncClient, login_name: str = "", password: str = PASSWORD) -> None:
    data = {"password": password}
    if login_name:
        data["login"] = login_name
    response = await client.post("/admin/login", data=data)
    assert response.status_code == 303


async def account(session, login_name: str, password: str, role: str) -> AdminAccount:
    row = AdminAccount(
        login=login_name,
        display_name=login_name,
        role=role,
        password_hash=security.hash_password(password),
    )
    session.add(row)
    await session.commit()
    return row


async def test_every_owner_page_renders(client, session, panel):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=9901, username="smoke")
    await session.commit()
    await login(client)

    for path, needle in {**OWNER_PAGES, f"/admin/users/{user.id}": "smoke"}.items():
        response = await client.get(path)
        assert response.status_code == 200, f"{path} → {response.status_code}"
        body = response.text
        assert needle in body, f"на {path} нет «{needle}»"
        for trouble in ("jinja2.exceptions", "Undefined", "{{", "{%"):
            assert trouble not in body, f"{path}: в HTML остался «{trouble}»"


async def test_support_sees_pages_but_not_forbidden_ones(client, session):
    await account(session, "sup", "sup-pass", "support")
    await login(client, "sup", "sup-pass")

    for path in ("/admin", "/admin/orders", "/admin/users", "/admin/nodes", "/admin/alerts", "/admin/referrals"):
        assert (await client.get(path)).status_code == 200, path

    for path in SUPPORT_FORBIDDEN:
        response = await client.get(path)
        assert response.status_code == 403, f"{path} должен быть закрыт для поддержки"

    # В меню поддержки нет ссылок на закрытые разделы
    page = await client.get("/admin")
    for path in ("/admin/team", "/admin/audit", "/admin/plans", "/admin/broadcast"):
        assert f'href="{path}"' not in page.text, f"поддержке показана ссылка {path}"


async def test_moderator_can_work_but_not_manage(client, session, panel):
    await account(session, "mod", "mod-pass", "moderator")
    user, _ = await subscriptions.get_or_create_user(session, tg_id=9902, username="moduser")
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="manual")
    await session.commit()

    await login(client, "mod", "mod-pass")
    assert (await client.get("/admin/audit")).status_code == 200
    assert (await client.post(f"/admin/orders/{order.id}/confirm")).status_code == 303

    for path in MODERATOR_FORBIDDEN:
        assert (await client.get(path)).status_code == 403, path


async def test_exports_return_csv_for_owner(client, session, panel):
    user, order = await subscriptions.get_or_create_user(session, tg_id=9903, username="csvuser")
    plan = (await orders.list_plans(session))[0]
    created = await orders.create_order(session, user, plan, provider="manual")
    await orders.mark_paid(session, created, panel)
    await session.commit()

    await login(client)
    orders_csv = await client.get("/admin/export/orders.csv", params={"status": "all"})
    assert orders_csv.status_code == 200
    assert "text/csv" in orders_csv.headers["content-type"]
    assert "attachment" in orders_csv.headers["content-disposition"]
    assert orders_csv.content.startswith(b"\xef\xbb\xbf")  # BOM для Excel
    assert "csvuser" in orders_csv.content.decode("utf-8-sig")

    users_csv = await client.get("/admin/export/users.csv")
    assert users_csv.status_code == 200
    assert "csvuser" in users_csv.content.decode("utf-8-sig")

    audit_csv = await client.get("/admin/export/audit.csv")
    assert audit_csv.status_code == 200


async def test_support_cannot_export(client, session):
    await account(session, "sup2", "sup-pass", "support")
    await login(client, "sup2", "sup-pass")
    assert (await client.get("/admin/export/orders.csv")).status_code == 403


async def test_status_all_tab_is_not_lost(client, session, panel):
    """Регресс: значение фильтра «all» нельзя выбрасывать из ссылок.

    Раньше вкладка «Все» вела на «ждут оплаты», и оплаченный заказ исчезал.
    """
    user, _ = await subscriptions.get_or_create_user(session, tg_id=9904, username="alltab")
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="manual")
    await orders.mark_paid(session, order, panel)
    await session.commit()

    await login(client)
    page = await client.get("/admin/orders", params={"status": "all"})
    assert page.status_code == 200
    assert f"#{order.id}" in page.text

    # и ссылка в пагинации сохраняет статус
    assert "status=all" in page.text or "status%3Dall" in page.text


async def test_query_string_keeps_all():
    assert ui.query_string({"status": "all", "q": ""}, page=2) == "status=all&page=2"
    assert ui.query_string({"status": "pending", "q": "199"}, page=1) == "status=pending&q=199&page=1"


async def test_forbidden_page_explains_role(client, session):
    await account(session, "sup3", "sup-pass", "support")
    await login(client, "sup3", "sup-pass")
    page = await client.get("/admin/team")
    assert page.status_code == 403
    assert "Недостаточно прав" in page.text
    assert "Поддержка" in page.text  # роль названа человеческим словом
