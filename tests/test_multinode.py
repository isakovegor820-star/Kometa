"""Тесты мультинод: один клиент — на всех панелях, подписка собирает все страны.

Смысл: чтобы сервис работал и в Калининграде, и во Владивостоке, нужно несколько
нод (Германия + Токио). Клиент должен существовать на каждой панели с одним
uuid — тогда ссылка-подписка отдаёт конфиги всех стран, а продление, блокировка
и истечение применяются к каждой ноде.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Node, Subscription, User
from app.panels.base import PanelError, UserSpec
from app.panels.fake import FakePanel
from app.panels.registry import registry
from app.services import subscriptions


class BrokenPanel(FakePanel):
    """Нода, которая не отвечает: проверяем, что выдача не срывается."""

    name = "broken-node"

    async def create_user(self, spec: UserSpec):
        raise PanelError("нода недоступна")


@pytest.fixture
def node_panel():
    return FakePanel(sub_base="http://node/fake", host="203.0.113.77")


@pytest.fixture
def two_panels(monkeypatch, panel, node_panel):
    """Основная панель + нода, как их видит приложение."""

    async def fake_all_panels(session):
        return [panel, node_panel]

    async def fake_pairs(session):
        # Подписка ходит за парами (нода, панель): ей нужен канал локации.
        # Здесь обе локации обычные — режимы проверяются в test_reserve_and_probe.
        return [(None, panel), (None, node_panel)]

    monkeypatch.setattr(registry, "all_panels", fake_all_panels)
    monkeypatch.setattr(registry, "all_panels_with_nodes", fake_pairs)
    return panel, node_panel


async def make_user(session, tg_id: int) -> User:
    """Пользователь как его создаёт бот (с реферальным кодом)."""
    user, _ = await subscriptions.get_or_create_user(session, tg_id=tg_id, username=f"u{tg_id}")
    await session.flush()
    return user


async def test_trial_creates_client_on_every_panel(session, two_panels):
    """Ключ создаётся на обеих нодах с одинаковым uuid."""
    primary, node = two_panels
    user = await make_user(session, 7701)

    sub, granted = await subscriptions.start_trial(session, user, [primary, node])
    await session.commit()

    assert granted is True
    assert sub.panel_user_uuid
    for panel in (primary, node):
        found = await panel.find_user_by_email(subscriptions._panel_email(user))
        assert found is not None, f"{panel.name}: клиента нет"
        assert found.uuid == sub.panel_user_uuid


async def test_subscription_collects_configs_from_all_panels(session, two_panels):
    """В одной ссылке-подписке — конфиги всех стран."""
    primary, node = two_panels
    user = await make_user(session, 7702)
    sub, _ = await subscriptions.start_trial(session, user, [primary, node])
    await session.commit()

    configs = []
    for panel in (primary, node):
        configs.extend(await panel.get_configs(sub.panel_user_uuid))

    body = "\n".join(configs)
    assert primary.host in body
    assert node.host in body


async def test_extend_days_touches_all_panels(session, two_panels):
    """Продление (оплата, бонус) применяется на каждой ноде."""
    primary, node = two_panels
    user = await make_user(session, 7703)
    sub, _ = await subscriptions.start_trial(session, user, [primary, node])
    await session.commit()

    before = (await node.find_user_by_email(subscriptions._panel_email(user))).expires_at
    await subscriptions.extend_days(session, user, 30, [primary, node], reason="test")
    await session.commit()
    after = (await node.find_user_by_email(subscriptions._panel_email(user))).expires_at

    assert after > before


async def test_blocking_touches_all_panels(session, two_panels):
    """Блокировка закрывает доступ на всех нодах, а не только на основной."""
    primary, node = two_panels
    user = await make_user(session, 7704)
    sub, _ = await subscriptions.start_trial(session, user, [primary, node])
    await session.commit()

    await subscriptions.set_enabled(sub, [primary, node], enabled=False)
    await session.commit()

    for panel in (primary, node):
        found = await panel.find_user_by_email(subscriptions._panel_email(user))
        assert found is not None and found.enabled is False


async def test_expired_subscription_is_disabled_everywhere(session, two_panels):
    """Истёк триал — доступ гасится на всех нодах сразу."""
    from datetime import datetime, timedelta, timezone

    primary, node = two_panels
    user = await make_user(session, 7705)
    sub, _ = await subscriptions.start_trial(session, user, [primary, node])
    sub.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()

    changed = await subscriptions.disable_expired(session, [primary, node])
    await session.commit()

    assert len(changed) == 1
    for panel in (primary, node):
        found = await panel.find_user_by_email(subscriptions._panel_email(user))
        assert found is not None and found.enabled is False


async def test_dead_node_does_not_break_issue(session, panel):
    """Недоступная нода не должна лишать человека доступа к рабочей стране."""
    broken = BrokenPanel()
    user = await make_user(session, 7706)

    sub, granted = await subscriptions.start_trial(session, user, [panel, broken])

    assert granted is True
    assert sub.panel_user_uuid  # клиент есть на основной панели
    assert await panel.find_user_by_email(subscriptions._panel_email(user)) is not None


async def test_client_with_other_uuid_is_recreated(session, two_panels):
    """Если нода знает email с другим uuid — клиента пересоздаём под общий uuid.

    Иначе ссылка-подписка не соберёт конфиг с этой панели: она ищет клиента по uuid.
    """
    primary, node = two_panels
    user = await make_user(session, 7707)
    stray = await node.create_user(
        UserSpec(email=subscriptions._panel_email(user), days=1, traffic_gb=1, devices=1, uuid="old-uuid")
    )
    assert stray.uuid == "old-uuid"

    sub, _ = await subscriptions.start_trial(session, user, [primary, node])
    await session.commit()

    found = await node.find_user_by_email(subscriptions._panel_email(user))
    assert found is not None
    assert found.uuid == sub.panel_user_uuid != "old-uuid"


async def test_nodes_from_db_become_extra_panels(session, panel):
    """Нода, добавленная в БД, автоматически попадает в список панелей."""
    session.add(
        Node(
            code="jp",
            title="🇯🇵 Япония",
            country="JP",
            host="203.0.113.88",
            panel_type="fake",
            panel_url="http://jp-panel",
            panel_token="token",
            inbound_ids="1",
            is_active=True,
        )
    )
    await session.commit()

    panels = await registry.all_panels(session)

    assert len(panels) == 2
    assert any(p is panel for p in panels)


async def test_subscription_names_each_country_separately(session, two_panels):
    """В одной подписке каждая локация подписана своей страной.

    Германия берёт имя из LOCATION_TITLE, Япония — из названия ноды в админке.
    Иначе клиент видит служебные имена инбаундов из панели.
    """
    import httpx
    from urllib.parse import unquote

    from app.web.sub import build_app

    primary, node = two_panels
    primary.location_title = "🇩🇪 Германия"
    node.location_title = "🇯🇵 Япония"
    user = await make_user(session, 7708)
    sub, _ = await subscriptions.start_trial(session, user, [primary, node])
    await session.commit()

    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"/sub/{sub.subscription_token}?format=plain")

    body = unquote(response.text)
    assert "🇩🇪 Германия" in body
    assert "🇯🇵 Япония" in body
    assert primary.host in body and node.host in body


async def test_sync_adds_client_to_new_panel_without_extending(session, panel):
    """Новая нода: старым клиентам досоздаём ключ, но срок не продлеваем.

    Иначе подключение новой страны незаметно продлевало бы всем подписки.
    """
    from datetime import datetime, timezone

    node = FakePanel(sub_base="http://jp/fake", host="203.0.113.55")
    user = await make_user(session, 7709)
    sub, _ = await subscriptions.start_trial(session, user, [panel])
    await session.commit()
    expires_before = sub.expires_at

    checked, failed = await subscriptions.sync_all_subscriptions(session, [panel, node])
    await session.commit()

    assert checked == 1 and failed == []
    created = await node.find_user_by_email(subscriptions._panel_email(user))
    assert created is not None
    assert created.uuid == sub.panel_user_uuid   # тот же uuid, что и на основной панели
    assert created.expires_at <= expires_before  # срок тот же, не продлён

    # повторная синхронизация ничего не ломает и не продлевает
    await subscriptions.sync_all_subscriptions(session, [panel, node])
    await session.commit()
    again = await node.find_user_by_email(subscriptions._panel_email(user))
    assert again.uuid == sub.panel_user_uuid
