"""Правда о локациях: система не имеет права утверждать то, что не измеряла.

Повод — жалоба владельца 08.10.2026: «у нас 3 геолокации, но сервер везде
сообщает, что не работают Нидерланды». Проверка показала две болезни, и обе
одного корня — **интерфейс выдавал одно состояние за другое**:

1. ``/status`` (публичная страница) считала локацию доступной по ответу
   **панели управления** и подписывала это словом «доступна». Панель отвечает по
   API-порту, клиент идёт на порт инбаунда — это два разных пути. Живая панель
   при закрытом порте показывалась как «работает» (ложная зелень), а панель,
   закрытая по IP-whitelist, — как «не работает» при живом клиентском порте
   (ложная тревога). Второе владелец и видел: «везде сообщает, что не работают».
2. Карточка ноды показывала состояние пробы по РАЗБОРУ полей шаблоном. Проба с
   ``last_probe_ok=true`` и пустым ``last_probe_stage`` (записи до появления
   шага) попадала в ветку «проба не выполнена» — зелёная нода с подписью, что
   её не проверяли.

Здесь зафиксировано правило, которое теперь одно на все интерфейсы: **состояние
порты/панель/не измеряли — три разных факта, и подменять их нельзя.**
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from app.db.models import Node
from app.panels.base import Inbound
from app.panels.registry import registry
from app.services import alerts, probe
from app.web.admin import infra
from app.web.sub import build_app


class StubPanel:
    """Панель-заглушка: отдаёт заданные инбаунды и не ходит в сеть."""

    name = "stub"
    location_title = ""

    def __init__(self, inbounds: list[Inbound], *, health: bool = True) -> None:
        self._inbounds = inbounds
        self._health = health

    async def health(self) -> bool:
        return self._health

    async def check_health(self) -> tuple[bool, str]:
        return self._health, "" if self._health else "панель не ответила"

    async def list_inbounds(self) -> list[Inbound]:
        return self._inbounds


def _vless(port: int = 443) -> Inbound:
    return Inbound(id=1, remark="Reality", protocol="vless", port=port, network="tcp", security="reality")


def _node(**overrides) -> Node:
    """Нода с пройденной проверкой панели — проба добавляется в тесте."""
    base = dict(
        code="nl",
        title="🇳🇱 Нидерланды",
        host="203.0.113.5",
        panel_type="fake",
        is_active=True,
        last_check_at=datetime.now(timezone.utc),
        last_check_ok=True,
    )
    base.update(overrides)
    return Node(**base)


# ------------------------------------------------------- вердикт и подпись
def test_successful_probe_without_stage_is_still_success():
    """Успешная проба — успех, даже если шаг не записан (старые данные).

    Шаг ``ok`` появился позже самой пробы. Нода, которую успешно проверили до
    обновления, обязана читаться как рабочая, а не как «пробу не выполняли».
    """
    node = _node(last_probe_at=datetime.now(timezone.utc), last_probe_ok=True, last_probe_stage="")

    assert probe.probe_verdict(node) == probe.PROBE_OK
    assert probe.probe_state(node)[1].startswith("порт открыт")


def test_port_failure_names_the_port_when_another_is_open():
    """«Порт 8443 не пускает, а 443 открыт» — диагноз, а не «нода недоступна».

    У локации в подписке несколько портов. Ответ «работает» и ответ «сломалось»
    оба неверны: клиент подключится через живой порт, а чинить надо закрытый.
    """
    node = _node(
        last_probe_at=datetime.now(timezone.utc),
        last_probe_ok=True,
        last_probe_stage="ok",
        last_probe_ports=json.dumps(
            [
                {"port": 443, "label": "Reality", "ok": True, "ms": 30},
                {"port": 8443, "label": "Добор", "ok": False, "ms": 0, "detail": "таймаут TCP"},
            ]
        ),
    )

    state, text = probe.probe_state(node)

    assert state == probe.PROBE_OK
    # Успех остаётся успехом: живого порта достаточно, чтобы подключиться.
    assert probe.probe_ports(node)[1]["ok"] is False


def test_broken_json_in_ports_does_not_break_card():
    """Битая диагностическая строка не должна ломать карточку ноды."""
    node = _node(last_probe_at=datetime.now(timezone.utc), last_probe_ok=True, last_probe_ports="{не json")

    assert probe.probe_ports(node) == []
    assert probe.probe_verdict(node) == probe.PROBE_OK


def test_never_probed_is_not_reported_as_working():
    """«Пробы не было» — не «работает»: страница не выдумывает результат."""
    node = _node(last_check_ok=True)

    state, text = probe.probe_state(node)

    assert state == probe.PROBE_UNKNOWN
    assert "не запускалась" in text
    assert "открыт" not in text


def test_ready_requires_probe_or_unknown_but_not_failure():
    """Готовность локации: панель жива И порт не отвалился."""
    assert infra.node_ready(_node(last_probe_ok=False, last_probe_stage="")) is True  # не измеряли
    assert (
        infra.node_ready(
            _node(last_probe_at=datetime.now(timezone.utc), last_probe_ok=True, last_probe_stage="ok")
        )
        is True
    )
    assert (
        infra.node_ready(
            _node(last_probe_at=datetime.now(timezone.utc), last_probe_ok=False, last_probe_stage="tcp")
        )
        is False
    )
    assert infra.node_ready(_node(last_check_ok=False)) is False


# --------------------------------------------------- проба: несколько портов
async def test_detailed_probe_keeps_every_port(monkeypatch):
    """Проба отчитывается по каждому порту, а не «первый успешный»."""
    panel = StubPanel([_vless(443), Inbound(id=2, remark="WS", protocol="vless", port=8443, network="ws", security="tls")])

    async def fake_endpoint(host, port, *, sni="", timeout=None):  # noqa: ANN001 - двойник
        return probe.ProbeResult(port == 443, ms=25, stage="tcp", detail="двойник")

    monkeypatch.setattr(probe, "probe_endpoint", fake_endpoint)

    detailed = await probe.probe_panel_detailed(panel, "203.0.113.5")

    assert detailed.best.ok is True
    assert detailed.best.stage == probe.OK_STAGE, "успех обязан иметь явный шаг для интерфейсов"
    assert detailed.open_ports == [443]
    assert detailed.closed_ports == [8443]


async def test_check_probes_stores_port_evidence(session, monkeypatch):
    """Доказательства пробы сохраняются в ноде — их видно в админке и в разборе."""
    node = _node(host="127.0.0.1", last_check_ok=True)
    session.add(node)
    await session.commit()

    panel = StubPanel([_vless(443), Inbound(id=2, remark="WS", protocol="vless", port=8443, network="ws", security="tls")])

    async def fake_endpoint(host, port, *, sni="", timeout=None):  # noqa: ANN001 - двойник
        return probe.ProbeResult(port == 443, ms=11, stage="tcp", detail="двойник")

    monkeypatch.setattr(probe, "probe_endpoint", fake_endpoint)

    results = await alerts.check_probes(session, [(node, panel)])
    await session.commit()

    assert results[0]["ports"], "в результате проверки должна быть таблица портов"
    stored = json.loads(node.last_probe_ports)
    assert {item["port"] for item in stored} == {443, 8443}
    assert node.last_probe_stage == probe.OK_STAGE


async def test_alert_names_closed_port_when_another_is_open(session, monkeypatch):
    """Один порт из двух закрыт → предупреждение с номером порта, а не «нода упала».

    Разделение важное: «подключиться есть куда» и «подключиться некуда» — разные
    сообщения и разные действия. Склеив их, мы получали ровно ту путаницу, из-за
    которой владелец читал «порт не пускает» там, где клиенты работали.
    """
    from app.db.models import Alert

    node = _node(host="127.0.0.1", last_check_ok=True)
    session.add(node)
    await session.commit()

    panel = StubPanel([_vless(443), Inbound(id=2, remark="WS", protocol="vless", port=8443, network="ws", security="tls")])

    async def fake_endpoint(host, port, *, sni="", timeout=None):  # noqa: ANN001 - двойник
        return probe.ProbeResult(port == 443, ms=9, stage="tcp", detail="таймаут TCP")

    monkeypatch.setattr(probe, "probe_endpoint", fake_endpoint)

    results = await alerts.check_probes(session, [(node, panel)])
    await session.commit()

    assert results[0]["degraded"] is True
    alert = (
        await session.scalars(select(Alert).where(Alert.kind == "node_probe_degraded"))
    ).one()
    assert alert.status == "open"
    assert alert.severity == "warn"
    assert "8443" in alert.title, "алерт обязан называть порт: иначе непонятно, что чинить"
    assert "443" in alert.message, "и называть рабочий порт: клиенту есть куда подключиться"
    # Подключение живое: нода не «упала», и клиентский профиль работает.
    assert node.last_probe_ok is True


async def test_all_ports_closed_is_a_real_failure(session, monkeypatch):
    """Закрыты ВСЕ порты → «нода не пускает клиента», важность «ошибка»."""
    from app.db.models import Alert

    node = _node(host="127.0.0.1", last_check_ok=True)
    session.add(node)
    await session.commit()

    panel = StubPanel([_vless(443), Inbound(id=2, remark="WS", protocol="vless", port=8443, network="ws", security="tls")])

    async def fake_endpoint(host, port, *, sni="", timeout=None):  # noqa: ANN001 - двойник
        return probe.ProbeResult(False, ms=0, stage="tcp", detail="таймаут TCP")

    monkeypatch.setattr(probe, "probe_endpoint", fake_endpoint)

    results = await alerts.check_probes(session, [(node, panel)])
    await session.commit()

    assert results[0]["degraded"] is False
    alert = (await session.scalars(select(Alert).where(Alert.kind == "node_probe_failed"))).one()
    assert alert.status == "open"
    assert alert.severity == "err"
    assert "443" in alert.title and "8443" in alert.title
    assert node.last_probe_ok is False


# --------------------------------------------------- публичная страница /status
@pytest.fixture
async def status_client(session, monkeypatch):
    """Клиент публичной страницы: подменяем реестр панелей на одну ноду."""

    async def build(panel, node):
        async def pairs(_session):  # noqa: ANN001
            return [(node, panel)]

        async def panels_only(_session):  # noqa: ANN001
            return [panel]

        monkeypatch.setattr(registry, "all_panels_with_nodes", pairs)
        monkeypatch.setattr(registry, "all_panels", panels_only)
        from app.web import sub

        sub._STATUS_CACHE["at"] = 0.0
        sub._STATUS_CACHE["value"] = None
        app = await build_app(bot=None)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client

    return build


async def test_status_distinguishes_panel_from_port(session, status_client):
    """Панель не отвечает, а порт открыт → локация РАБОТАЕТ.

    Это ровно тот случай, из-за которого владелец видел «не работают
    Нидерланды»: 3x-ui закрыт по IP-allowlist, health не проходит, а клиенты
    подключаются. Страница обязана это различать, а не пугать клиента.
    """
    node = _node(
        last_check_ok=False,
        last_check_error="панель не ответила за 5 с",
        last_probe_at=datetime.now(timezone.utc),
        last_probe_ok=True,
        last_probe_stage="ok",
        last_probe_ms=63,
    )
    session.add(node)
    await session.commit()

    async for client in status_client(StubPanel([_vless()], health=False), node):
        payload = (await client.get("/status", params={"format": "json"})).json()
        html = (await client.get("/status")).text

    entry = payload["nodes"][0]
    assert entry["panel"] is False
    assert entry["probe"] == probe.PROBE_OK
    assert entry["ok"] is True, "живой порт важнее недоступной панели"
    assert "работает" in html
    # Про закрытую панель страница говорит прямо — и не пугает клиента: замер
    # порта важнее, клиент подключается.
    assert "подключение работает" in html


async def test_status_shows_problem_when_panel_ok_but_port_closed(session, status_client):
    """Панель отвечает, порт закрыт → проблемы, и об этом сказано словами."""
    node = _node(
        last_probe_at=datetime.now(timezone.utc),
        last_probe_ok=False,
        last_probe_stage="tcp",
        last_probe_error="Reality: таймаут TCP (5.0 с)",
        last_probe_ports=json.dumps([{"port": 443, "label": "Reality", "ok": False, "detail": "таймаут TCP"}]),
    )
    session.add(node)
    await session.commit()

    async for client in status_client(StubPanel([_vless()]), node):
        payload = (await client.get("/status", params={"format": "json"})).json()
        html = (await client.get("/status")).text

    entry = payload["nodes"][0]
    assert entry["panel"] is True
    assert entry["probe"] == probe.PROBE_PORT_FAILED
    assert entry["ok"] is False
    assert payload["ok"] is False
    assert "Есть проблемы" in html
    assert "порт для подключения закрыт" in html
    assert "443" in html, "страница называет проблемный порт"


async def test_status_never_claims_working_without_measurement(session, status_client):
    """Ни одного замера → «Проверяем», а не «Всё работает».

    Фальшивое «всё хорошо» опаснее честного «данных нет»: по нему владелец
    решает, что инцидента нет, и не идёт разбираться.
    """
    node = _node(last_check_ok=True)
    session.add(node)
    await session.commit()

    async for client in status_client(StubPanel([_vless()]), node):
        html = (await client.get("/status")).text
        payload = (await client.get("/status", params={"format": "json"})).json()

    assert "Проверяем" in html
    assert "Всё работает" not in html
    assert payload["ok"] is False, "непроверенное состояние — не «всё хорошо»"
    assert "Замер порта ещё не проходил" in html
