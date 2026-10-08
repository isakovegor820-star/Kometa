"""Тесты инструмента «довести состав клиентов до настроек» (``sync_inbounds``).

Повод: 07-08.10.2026 из настроек нод выпал резервный Reality-8443. Клиенты,
выданные раньше, остались без профиля на запасном порту (14 на NL, 8 на FI), а
``/sync`` в боте этого не видел — он проверяет наличие клиента **на панели**, а
не в каждом её инбаунде. Здесь проверяем расчёт «чего не хватает» (чистая
функция) и то, что запись идёт тем же объектом клиента — без нового uuid/subId.
"""

from __future__ import annotations

import json

import pytest

from app.panels.base import Inbound, PanelClient, PanelError, PanelUser, UserSpec
from app.tools.sync_inbounds import plan


def inbound(inbound_id: int, *, protocol: str = "vless", emails: list[str] | None = None) -> dict:
    return {
        "id": inbound_id,
        "remark": f"inbound-{inbound_id}",
        "protocol": protocol,
        "port": 443 if inbound_id == 1 else 8443,
        "settings": json.dumps({"clients": [{"email": email, "id": f"uuid-{email}"} for email in emails or []]}),
    }


def test_plan_finds_clients_missing_in_new_inbound():
    """Клиент есть на 443, но не на 8443 → его надо досоздать именно туда."""
    inbounds = [inbound(1, emails=["u1", "u2"]), inbound(4, emails=["u1"])]

    todo = plan(inbounds, [1, 4])

    assert [(iid, email) for iid, email, _client in todo] == [(4, "u2")]
    # Переносим существующий объект, а не создаём нового клиента.
    assert todo[0][2]["id"] == "uuid-u2"


def test_plan_ignores_inbounds_outside_settings():
    """Инбаунд не входит в настройки ноды — трогать его не нужно."""
    inbounds = [inbound(1, emails=["u1"]), inbound(9, emails=[])]

    assert plan(inbounds, [1]) == []


def test_plan_does_not_move_clients_between_protocols():
    """AmneziaWG и vless — разные формы клиента: переносить между ними нельзя."""
    inbounds = [
        inbound(1, protocol="vless", emails=["u1"]),
        inbound(2, protocol="amneziawg", emails=["u1"]),
    ]
    # u2 есть только в vless-инбаунде → в wireguard его тянуть не должны.
    inbounds[0]["settings"] = json.dumps({"clients": [{"email": "u1"}, {"email": "u2"}]})

    todo = plan(inbounds, [1, 2])

    assert todo == []


def test_plan_skips_when_everything_is_in_place():
    inbounds = [inbound(1, emails=["u1"]), inbound(4, emails=["u1"])]

    assert plan(inbounds, [1, 4]) == []


def test_plan_can_be_limited_to_one_client():
    """``--only`` — проверка механики на одном человеке, остальных не трогаем."""
    inbounds = [inbound(1, emails=["u1", "u2"]), inbound(4, emails=[])]

    todo = plan(inbounds, [1, 4], only="u2")

    assert [(iid, email) for iid, email, _ in todo] == [(4, "u2")]


def test_plan_reads_settings_as_object_too():
    """Панель ≥ v3.9 отдаёт ``settings`` объектом — разбор должен это понимать."""
    inbounds = [
        {"id": 1, "protocol": "vless", "settings": {"clients": [{"email": "u1"}]}},
        {"id": 4, "protocol": "vless", "settings": {"clients": []}},
    ]

    assert [(iid, email) for iid, email, _ in plan(inbounds, [1, 4])] == [(4, "u1")]


# --------------------------------------------------- запись через панель
class RecordingPanel(PanelClient):
    """Панель-двойник: помнит, что в неё добавляли, и в сеть не ходит."""

    name = "xui"
    location_title = "🇳🇱 Нидерланды"

    def __init__(self, raw: list[dict]) -> None:
        self._raw = raw
        self.added: list[tuple[int, dict]] = []

    async def health(self) -> bool:
        return True

    async def list_inbounds(self) -> list[Inbound]:
        return [Inbound(id=1, remark="R", protocol="vless", port=443, network="tcp")]

    async def raw_inbounds(self) -> list[dict]:
        return self._raw

    async def add_client_to_inbounds(self, client: dict, inbound_ids: list[int]) -> None:
        for inbound_id in inbound_ids:
            self.added.append((inbound_id, client))

    async def create_user(self, spec: UserSpec) -> PanelUser:  # pragma: no cover
        raise NotImplementedError

    async def get_user(self, uuid: str) -> PanelUser | None:  # pragma: no cover
        return None

    async def update_user(self, uuid: str, **kwargs) -> PanelUser:  # pragma: no cover
        raise NotImplementedError

    async def delete_user(self, uuid: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def get_configs(self, uuid: str) -> list[str]:  # pragma: no cover
        return []


async def test_apply_adds_existing_client_object(session, monkeypatch):
    """В панель уходит тот же клиент (uuid/subId), а не новый."""
    from app.db.models import Node
    from app.panels.registry import registry
    from app.tools import sync_inbounds as tool

    node = Node(code="nl", title="🇳🇱 Нидерланды", panel_type="xui", is_active=True, inbound_ids="1,4")
    session.add(node)
    await session.commit()

    raw = [
        inbound(1, emails=["u1"]),
        {"id": 4, "remark": "R-8443", "protocol": "vless", "port": 8443, "settings": {"clients": []}},
    ]
    panel = RecordingPanel(raw)
    monkeypatch.setattr(registry, "for_node", lambda _node: panel)

    dry = await tool.collect(session)
    assert [(iid, email) for iid, email in dry[0].missing] == [(4, "u1")]

    results = await tool.apply_plan(session)

    assert results == [("nl", 4, "u1", "добавлен")]
    assert panel.added == [(4, {"email": "u1", "id": "uuid-u1"})]


async def test_run_reports_nothing_to_do(session, monkeypatch, capsys):
    """Когда всё на месте, dry-run возвращает 0 и говорит об этом."""
    from app.db.models import Node
    from app.panels.registry import registry
    from app.tools import sync_inbounds as tool

    session.add(Node(code="nl", title="🇳🇱 Нидерланды", panel_type="xui", is_active=True, inbound_ids="1,4"))
    await session.commit()
    panel = RecordingPanel([inbound(1, emails=["u1"]), inbound(4, emails=["u1"])])
    monkeypatch.setattr(registry, "for_node", lambda _node: panel)

    code = await tool.run(apply=False)

    assert code == 0
    assert "✅" in capsys.readouterr().out


async def test_collect_reports_unreachable_panel(session, monkeypatch):
    """Недоступная панель — это ошибка в плане, а не «всё хорошо»."""
    from app.db.models import Node
    from app.panels.registry import registry
    from app.tools import sync_inbounds as tool

    class BrokenPanel(RecordingPanel):
        async def raw_inbounds(self) -> list[dict]:
            raise PanelError("панель не ответила за 15 с")

    session.add(Node(code="nl", title="🇳🇱 Нидерланды", panel_type="xui", is_active=True, inbound_ids="1,4"))
    await session.commit()
    monkeypatch.setattr(registry, "for_node", lambda _node: BrokenPanel([]))

    plans = await tool.collect(session)

    assert plans[0].error and "не ответила" in plans[0].error
    assert plans[0].missing == []


async def test_base_panel_refuses_to_add_into_single_inbound():
    """Базовая панель честно говорит, что не умеет: молча ничего не делаем."""

    class Minimal(RecordingPanel):
        async def add_client_to_inbounds(self, client: dict, inbound_ids: list[int]) -> None:
            return await PanelClient.add_client_to_inbounds(self, client, inbound_ids)

    with pytest.raises(PanelError):
        await Minimal([]).add_client_to_inbounds({"email": "u1"}, [4])
