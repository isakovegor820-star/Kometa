"""Аварийный уровень: резервный профиль, пинг и проба ноды.

Проверяем три вещи, от которых зависит «клиент видит живой профиль с пингом»:
* проба TCP до инбаунда и замер задержки (``app/services/probe.py``);
* алерт, когда панель отвечает, а порт клиента не пускает;
* резервная локация: метка в подписке, отдельная группа в Clash/sing-box,
  свой test-URL и список локаций с пингом на странице подключения.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone

import httpx
import pytest
import yaml
from sqlalchemy import select
from urllib.parse import unquote

from app.db.models import Alert, Node
from app.panels.base import Inbound
from app.panels.registry import registry
from app.services import alerts, probe, subscriptions
from app.services.probe import probe_endpoint, probe_targets
from app.tools import ping_node
from app.web.sub import build_app
from app.web.subscription_format import RESERVE_MARK, build_clash_yaml, build_singbox_json
from tests.test_subscriptions import make_user


# --------------------------------------------------------------------- helpers
def vless(name: str) -> str:
    """VLESS-ссылка с человеческим именем локации."""
    return (
        "vless://11111111-2222-3333-4444-555555555555@1.2.3.4:443"
        "?type=tcp&security=reality&fp=firefox&pbk=KEY&sni=www.microsoft.com&sid=ab12"
        f"&flow=xtls-rprx-vision#{name}"
    )


async def _live_server() -> tuple[asyncio.AbstractServer, int]:
    """Поднять локальный TCP-сервер и вернуть его порт."""

    async def handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


class StubPanel:
    """Панель-заглушка: отдаёт заданные инбаунды, в сеть не ходит."""

    name = "stub"
    location_title = ""

    def __init__(self, inbounds: list[Inbound]) -> None:
        self._inbounds = inbounds

    async def list_inbounds(self) -> list[Inbound]:
        return self._inbounds


# ------------------------------------------------------------------ проба ноды
async def test_probe_endpoint_ok_on_live_port():
    server, port = await _live_server()
    try:
        result = await probe_endpoint("127.0.0.1", port, timeout=2.0)
    finally:
        server.close()
        await server.wait_closed()

    assert result.ok is True
    assert result.stage == "tcp"
    assert result.ms >= 0


async def test_probe_endpoint_reports_closed_port():
    server, port = await _live_server()
    server.close()
    await server.wait_closed()

    result = await probe_endpoint("127.0.0.1", port, timeout=1.0)

    assert result.ok is False
    assert result.stage == "tcp"
    assert result.detail


def test_probe_targets_skip_udp_and_wireguard():
    inbounds = [
        Inbound(id=1, remark="Reality", protocol="vless", port=443, network="tcp", security="reality"),
        Inbound(id=2, remark="AWG", protocol="amneziawg", port=990, network="udp", security=""),
        Inbound(id=3, remark="WS", protocol="vless", port=8443, network="ws", security="tls"),
        Inbound(id=4, remark="Дубль порта", protocol="vless", port=443, network="tcp", security=""),
    ]

    targets = probe_targets(inbounds, "1.2.3.4")

    assert [target.port for target in targets] == [443, 8443]
    assert targets[0].label == "Reality"


def test_probe_targets_need_host():
    inbounds = [Inbound(id=1, remark="R", protocol="vless", port=443, network="tcp", security="reality")]
    assert probe_targets(inbounds, "") == []


async def test_probe_panel_without_host_explains_why():
    panel = StubPanel([Inbound(id=1, remark="R", protocol="vless", port=443, network="tcp")])

    result = await probe.probe_panel(panel, "")

    assert result.ok is False
    assert result.stage == "config"
    assert "host" in result.detail


async def test_ping_node_tool_reports_success():
    server, port = await _live_server()
    try:
        code = await ping_node.run("127.0.0.1", port, "", 2, 2.0)
    finally:
        server.close()
        await server.wait_closed()

    assert code == 0


# ------------------------------------------------------- алерт «порт не пускает»
async def test_check_probes_raises_and_resolves_alert(session, monkeypatch):
    node = Node(code="de", title="🇩🇪 Германия", host="127.0.0.1", is_active=True)
    session.add(node)
    await session.commit()

    state = {"ok": False}

    async def fake_probe_endpoint(_host, _port, *, sni="", timeout=None):  # noqa: ANN001 - двойник
        return probe.ProbeResult(state["ok"], ms=42 if state["ok"] else 0, stage="tcp", detail="двойник")

    monkeypatch.setattr(probe, "probe_endpoint", fake_probe_endpoint)
    panel = StubPanel([Inbound(id=1, remark="R", protocol="vless", port=443, network="tcp", security="reality")])

    results = await alerts.check_probes(session, [(node, panel)])
    await session.commit()

    assert results[0]["ok"] is False
    assert node.last_probe_ok is False
    assert node.last_probe_at is not None
    opened = (await session.scalars(select(Alert).where(Alert.kind == "node_probe_failed"))).all()
    assert len(opened) == 1
    assert opened[0].status == "open"

    state["ok"] = True
    await alerts.check_probes(session, [(node, panel)])
    await session.commit()

    assert node.last_probe_ok is True
    assert node.last_probe_ms == 42
    resolved = (await session.scalars(select(Alert).where(Alert.kind == "node_probe_failed"))).all()
    assert resolved[0].status == "resolved"


# ----------------------------------------------------------------- подписка
def test_clash_adds_reserve_group():
    document = yaml.safe_load(
        build_clash_yaml(
            [vless("DE"), vless("DE" + RESERVE_MARK)],
            title="Kometa",
            test_url="https://sub.example/ping",
        )
    )

    groups = document["proxy-groups"]
    assert [group["name"] for group in groups] == ["Kometa", "Kometa Резерв"]
    assert groups[0]["proxies"] == ["DE", "DE" + RESERVE_MARK]
    assert groups[1]["proxies"] == ["DE" + RESERVE_MARK]
    assert groups[1]["url"] == "https://sub.example/ping"
    assert document["rules"][-1] == "MATCH,Kometa"


def test_clash_without_reserve_keeps_one_group():
    document = yaml.safe_load(build_clash_yaml([vless("DE")]))

    assert [group["name"] for group in document["proxy-groups"]] == ["Kometa"]


def test_singbox_adds_reserve_group():
    document = json.loads(build_singbox_json([vless("DE"), vless("DE" + RESERVE_MARK)]))

    tags = [outbound["tag"] for outbound in document["outbounds"]]
    assert "Kometa Auto" in tags
    assert "Kometa Резерв" in tags
    reserve = next(o for o in document["outbounds"] if o["tag"] == "Kometa Резерв")
    assert reserve["outbounds"] == ["DE" + RESERVE_MARK]
    assert reserve["type"] == "urltest"


async def test_ping_endpoint_returns_204():
    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/ping")

    assert response.status_code == 204
    assert response.content == b""


def test_test_url_prefers_own_endpoint(monkeypatch):
    from app.web import sub as sub_module

    monkeypatch.setattr(sub_module.settings, "subscription_test_url", "")
    monkeypatch.setattr(sub_module.settings, "public_base_url", "https://sub.example")

    assert sub_module._test_url() == "https://sub.example/ping"


def test_test_url_falls_back_to_external(monkeypatch):
    from app.web import sub as sub_module
    from app.web.subscription_format import DEFAULT_TEST_URL

    monkeypatch.setattr(sub_module.settings, "subscription_test_url", "")
    monkeypatch.setattr(sub_module.settings, "public_base_url", "http://127.0.0.1:8080")

    assert sub_module._test_url() == DEFAULT_TEST_URL


async def test_subscription_marks_reserve_and_shows_ping(session, monkeypatch, panel):
    node = Node(
        code="de",
        title="🇩🇪 Германия",
        host="1.2.3.4",
        channel="reserve",
        is_active=True,
        last_probe_at=datetime.now(timezone.utc),
        last_probe_ok=True,
        last_probe_ms=48,
    )
    session.add(node)
    await session.commit()

    async def fake_pairs(_session):  # noqa: ANN001 - подменяем реестр
        return [(node, panel)]

    monkeypatch.setattr(registry, "all_panels_with_nodes", fake_pairs)

    user = await make_user(session, 6101)
    sub, _ = await subscriptions.start_trial(session, user, panel)
    await session.commit()

    app = await build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"/sub/{sub.subscription_token}")
        page = await client.get(f"/connect/{sub.subscription_token}")

    decoded = base64.b64decode(response.text).decode()
    # Имя локации панель отдаёт в URL-кодировке (как и «🇩🇪 Германия»), поэтому
    # проверяем метку после декодирования фрагмента.
    assert "резерв" in unquote(decoded), "резервная локация должна быть помечена в base64-списке"
    assert page.status_code == 200
    assert "48 мс" in page.text
    # На странице канал подписан человеческим именем группы («Резерв»).
    assert "Резерв" in page.text
