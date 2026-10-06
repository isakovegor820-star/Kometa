"""Тесты операционных доработок панели: возвраты, списания, массовые действия.

Смысл: модератор должен уметь исправить ошибку (вернуть деньги, списать лишние
дни, закрыть пачку заявок) из интерфейса, а не через SQL. Здесь проверяем не
вёрстку, а последствия: статусы, сроки, деньги в отчётах и записи в журнале.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.models import Event, UserNote
from app.services import orders, stats, subscriptions
from app.web import security
from app.web.sub import build_app

PASSWORD = "test-admin-password"
settings = get_settings()


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


async def paid_order(session, panel, tg_id: int, username: str):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=tg_id, username=username)
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="manual")
    await orders.mark_paid(session, order, panel)
    await session.commit()
    return user, order


# ------------------------------------------------------------------ возвраты
async def test_refund_removes_money_from_revenue(client, session, panel):
    """Возврат убирает заказ из выручки и отключает доступ."""
    user, order = await paid_order(session, panel, 8801, "refundme")
    before = (await stats.collect(session)).revenue_month
    assert before >= order.amount_rub

    await login(client)
    response = await client.post(f"/admin/orders/{order.id}/refund", data={"note": "клиент передумал"})
    assert response.status_code == 303

    await session.refresh(order)
    assert order.status == "refunded"
    assert order.refunded_at is not None
    assert order.refunded_by == "владелец"
    assert order.refund_note == "клиент передумал"

    after = (await stats.collect(session)).revenue_month
    assert after == before - order.amount_rub

    sub = await subscriptions.get_subscription(session, user.id)
    assert sub is not None and sub.status == "blocked"

    event = await session.scalar(select(Event).where(Event.kind == "order_refunded").order_by(Event.id.desc()))
    assert event is not None


async def test_refund_keeps_access_when_another_payment_exists(session, client, panel):
    """Вернули первый месяц, а второй оплачен — доступ остаётся."""
    user, first = await paid_order(session, panel, 8802, "twice")
    plan = (await orders.list_plans(session))[0]
    second = await orders.create_order(session, user, plan, provider="manual")
    await orders.mark_paid(session, second, panel)
    await session.commit()

    await login(client)
    assert (await client.post(f"/admin/orders/{first.id}/refund", data={"note": ""})).status_code == 303

    await session.refresh(first)
    await session.refresh(second)
    sub = await subscriptions.get_subscription(session, user.id)
    assert first.status == "refunded"
    assert second.status == "paid"
    assert sub is not None and sub.status in {"active", "trial"}


async def test_refund_of_unpaid_order_is_rejected(client, session, panel):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=8803, username="pending")
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="manual")
    await session.commit()

    await login(client)
    response = await client.post(f"/admin/orders/{order.id}/refund")
    assert response.status_code == 303

    await session.refresh(order)
    assert order.status == "pending"  # ничего не сломалось


async def test_confirmed_refund_is_idempotent(client, session, panel):
    user, order = await paid_order(session, panel, 8804, "once")
    await login(client)
    assert (await client.post(f"/admin/orders/{order.id}/refund")).status_code == 303
    assert (await client.post(f"/admin/orders/{order.id}/refund")).status_code == 303

    await session.refresh(order)
    assert order.status == "refunded"
    sub = await subscriptions.get_subscription(session, user.id)
    assert sub is not None and sub.status == "blocked"


# --------------------------------------------------------- ручные корректировки
async def test_write_off_shortens_subscription(client, session, panel):
    user, _ = await paid_order(session, panel, 8805, "writeoff")
    sub = await subscriptions.get_subscription(session, user.id)
    assert sub is not None
    before = sub.expires_at

    await login(client)
    response = await client.post(
        f"/admin/users/{user.id}/write-off", data={"days": "5", "reason": "компенсация сбоя"}
    )
    assert response.status_code == 303

    await session.refresh(sub)
    assert timedelta(days=4) <= (before - sub.expires_at) <= timedelta(days=6)


async def test_grant_without_subscription_goes_to_balance(client, session, panel):
    """У клиента ещё нет подписки — дни не теряются, а ждут первой оплаты."""
    user, _ = await subscriptions.get_or_create_user(session, tg_id=8806, username="nosub")
    await session.commit()

    await login(client)
    response = await client.post(f"/admin/users/{user.id}/grant", data={"days": "10", "reason": "акция"})
    assert response.status_code == 303

    await session.refresh(user)
    assert user.bonus_days_balance == 10


async def test_revoke_and_restore_access(client, session, panel):
    user, _ = await paid_order(session, panel, 8807, "revoked")
    sub = await subscriptions.get_subscription(session, user.id)

    await login(client)
    assert (await client.post(f"/admin/users/{user.id}/revoke", data={"reason": "шеринг"})).status_code == 303
    await session.refresh(sub)
    assert sub.status == "blocked"
    await session.refresh(user)
    assert user.is_blocked is False  # аккаунт в боте не тронули

    assert (await client.post(f"/admin/users/{user.id}/restore")).status_code == 303
    await session.refresh(sub)
    assert sub.status == "active"


async def test_unblock_restores_trial_status(client, session, panel):
    """Разблокировка не превращает пробный доступ в платный."""
    user, _ = await subscriptions.get_or_create_user(session, tg_id=8808, username="trialer")
    sub, _ = await subscriptions.start_trial(session, user, panel)
    await session.commit()

    await login(client)
    await client.post(f"/admin/users/{user.id}/block", data={"block": "1"})
    await session.refresh(sub)
    assert sub.status == "blocked"

    await client.post(f"/admin/users/{user.id}/block", data={"block": "0"})
    await session.refresh(sub)
    assert sub.status == "trial"


async def test_blocked_user_cannot_get_access_by_confirm(client, session, panel):
    """Оплата от заблокированного не выдаёт доступ автоматически."""
    user, _ = await subscriptions.get_or_create_user(session, tg_id=8809, username="banned-payer")
    user.is_blocked = True
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="manual")
    await session.commit()

    await login(client)
    response = await client.post(f"/admin/orders/{order.id}/confirm")
    assert response.status_code == 303

    await session.refresh(order)
    assert order.status == "pending"


# ---------------------------------------------------------- массовые действия
async def test_bulk_confirm_processes_several_orders(client, session, panel):
    users = []
    for index in range(3):
        user, _ = await subscriptions.get_or_create_user(session, tg_id=8900 + index, username=f"bulk{index}")
        plan = (await orders.list_plans(session))[0]
        order = await orders.create_order(session, user, plan, provider="manual")
        users.append((user, order))
    await session.commit()
    ids = [order.id for _user, order in users]

    await login(client)
    response = await client.post(
        "/admin/orders/bulk",
        data={"action": "confirm", "ids": [str(value) for value in ids]},
    )
    assert response.status_code == 303

    for _user, order in users:
        await session.refresh(order)
        assert order.status == "paid"


async def test_bulk_reject_cancels_selected(client, session, panel):
    created = []
    for index in range(2):
        user, _ = await subscriptions.get_or_create_user(session, tg_id=8910 + index, username=f"rej{index}")
        plan = (await orders.list_plans(session))[0]
        created.append(await orders.create_order(session, user, plan, provider="manual"))
    await session.commit()

    await login(client)
    response = await client.post(
        "/admin/orders/bulk",
        data={"action": "reject", "ids": [str(order.id) for order in created]},
    )
    assert response.status_code == 303

    for order in created:
        await session.refresh(order)
        assert order.status == "canceled"

    event = await session.scalar(select(Event).where(Event.kind == "admin.order_bulk").order_by(Event.id.desc()))
    assert event is not None and event.actor_name == "владелец"


async def test_bulk_without_selection_reports_error(client):
    await login(client)
    response = await client.post("/admin/orders/bulk", data={"action": "confirm"})
    assert response.status_code == 303


# ------------------------------------------------------------------- заметки
async def test_notes_are_saved_and_visible(client, session, panel):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=8920, username="noted")
    await session.commit()

    await login(client)
    created = await client.post(
        f"/admin/users/{user.id}/notes", data={"text": "Просил вернуть деньги за простой 2 дня"}
    )
    assert created.status_code == 303

    page = await client.get(f"/admin/users/{user.id}")
    assert "Просил вернуть деньги" in page.text

    note = await session.scalar(select(UserNote).where(UserNote.user_id == user.id))
    assert note is not None and note.author == "владелец"

    deleted = await client.post(f"/admin/users/{user.id}/notes/{note.id}/delete")
    assert deleted.status_code == 303
    assert await session.scalar(select(UserNote).where(UserNote.user_id == user.id)) is None


async def test_tags_are_saved_and_filterable(client, session, panel):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=8921, username="tagged")
    await session.commit()

    await login(client)
    assert (await client.post(f"/admin/users/{user.id}/tags", data={"tags": "шеринг, vip"})).status_code == 303

    await session.refresh(user)
    assert user.tags == "шеринг,vip"

    page = await client.get("/admin/users", params={"tag": "vip"})
    assert "tagged" in page.text


# --------------------------------------------------------- поиск и карточка
async def test_user_card_shows_orders_and_subscription(client, session, panel):
    user, order = await paid_order(session, panel, 8930, "carduser")

    await login(client)
    page = await client.get(f"/admin/users/{user.id}")

    assert page.status_code == 200
    assert "carduser" in page.text
    assert f"#{order.id}" in page.text
    sub = await subscriptions.get_subscription(session, user.id)
    assert sub is not None and sub.subscription_token in page.text


async def test_orders_search_finds_by_kopecks(client, session, panel):
    """Сверка «пришло 199.13 ₽» ищется по точной сумме."""
    user, order = await subscriptions.get_or_create_user(session, tg_id=8931, username="searchme")
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="manual")
    await session.commit()

    await login(client)
    page = await client.get("/admin/orders", params={"status": "pending", "q": order.pay_amount_text})
    assert page.status_code == 200
    assert f"#{order.id}" in page.text


async def test_blocked_filter_uses_account_flag(client, session, panel):
    """Заблокированный без подписки виден в фильтре «заблокированные»."""
    user, _ = await subscriptions.get_or_create_user(session, tg_id=8932, username="blockednosub")
    user.is_blocked = True
    await session.commit()

    await login(client)
    page = await client.get("/admin/users", params={"status": "blocked"})
    assert page.status_code == 200
    assert "blockednosub" in page.text


async def test_users_page_paginates(client, session, panel):
    for index in range(5):
        await subscriptions.get_or_create_user(session, tg_id=8940 + index, username=f"page{index}")
    await session.commit()

    await login(client)
    page = await client.get("/admin/users", params={"per_page": "25", "page": "1"})
    assert page.status_code == 200
    assert "из 5" in page.text  # «Строки 1–5 из 5» либо «Всего: 5»


async def test_dashboard_shows_alert_and_refund_tiles(client, session, panel, monkeypatch):
    user, order = await paid_order(session, panel, 8950, "tiles")
    await login(client)
    assert (await client.post(f"/admin/orders/{order.id}/refund")).status_code == 303

    page = await client.get("/admin")
    assert page.status_code == 200
    assert "Возвраты 30 дней" in page.text
    assert "Средний чек" in page.text


async def test_health_and_subscription_endpoints_stay_public(client):
    """Панель закрыта, а подписки и health — нет: их дёргают клиенты."""
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/sub/nope")).status_code == 404


async def test_expire_stale_closes_old_pending(client, session, panel):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=8960, username="stale")
    plan = (await orders.list_plans(session))[0]
    order = await orders.create_order(session, user, plan, provider="manual")
    order.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    await session.commit()

    await login(client)
    response = await client.post("/admin/orders/expire-stale")
    assert response.status_code == 303

    await session.refresh(order)
    assert order.status == "expired"
