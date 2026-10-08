"""Регрессия на инцидент «Нода 🇳🇱 Нидерланды: порт не пускает клиента».

Что было: у ноды в БД стоял ``inbound_ids="3"``, а панель отдавала инбаунды 1 и 2.
Проба «глазами клиента» спрашивала у панели список инбаундов, чтобы понять, куда
стучаться; список не собирался (фильтр по ID падал), и проба объявляла «порт не
пускает клиента» — хотя до порта не дошла. Оператор шёл смотреть фаервол,
админка показывала зелёное «отвечает» рядом с красным «порт не пускает», а
взятый в работу алерт слетал через пять минут.

Здесь зафиксировано честное поведение каждого участка: панель (`PanelInboundMissing`
и фактические ID в тексте), проверка нод (вид алерта), проба (вердикт «не
проверялось»), центр алертов (закрытие, ack) и инструмент диагностики.
"""

from __future__ import annotations

import httpx
import pytest
from datetime import datetime, timezone
from sqlalchemy import select

from app.db.models import Alert, Node
from app.panels.base import (
    Inbound,
    PanelClient,
    PanelError,
    PanelInboundMissing,
    PanelUser,
    UserSpec,
)
from app.panels.xui import XuiPanel
from app.services import alerts as alerts_service
from app.services import probe as probe_service
from tests.test_panel_xui import BASE, TOKEN, FakeXui, _vless_inbound, _wireguard_inbound, make_panel


class ReasonPanel(PanelClient):
    """Панель-двойник: можно задать ответ, ошибку панели и ошибку инбаундов.

    ``list_inbounds`` падает (как настоящая панель с чужим ``inbound_ids``),
    а ``list_all_inbounds`` отдаёт сырой список — ровно как ``XuiPanel``.
    """

    name = "xui"
    location_title = "🇳🇱 Нидерланды"

    def __init__(
        self,
        *,
        health_error: str = "",
        inbound_error: Exception | None = None,
        inbounds: int = 2,
    ) -> None:
        self.health_error = health_error
        self.inbound_error = inbound_error
        self.count = inbounds

    async def check_health(self) -> tuple[bool, str]:
        return (False, self.health_error) if self.health_error else (True, "")

    async def health(self) -> bool:
        return not self.health_error

    def _raw(self) -> list[Inbound]:
        return [
            Inbound(id=index + 1, remark=f"Kometa-{index + 1}", protocol="vless", port=443 + index, network="tcp")
            for index in range(self.count)
        ]

    async def list_inbounds(self) -> list[Inbound]:
        if self.inbound_error is not None:
            raise self.inbound_error
        return self._raw()

    async def list_all_inbounds(self) -> list[Inbound]:
        return self._raw()

    # --- абстрактные методы: в этих сценариях не нужны ------------------
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


def missing_inbound_error() -> PanelInboundMissing:
    """Тот же текст, что рождается в ``XuiPanel._target_inbounds``."""
    return PanelInboundMissing(
        "в панели не найдены инбаунды [3] (проверь inbound_ids); "
        "панель отдаёт: 1 «Kometa-Reality-443», 2 «Kometa-AWG-990»"
    )


async def _node(session, **kwargs) -> Node:
    """Нода «Нидерланды» — как в инциденте."""
    data = {"code": "nl", "title": "🇳🇱 Нидерланды", "inbound_ids": "3", "is_active": True}
    data.update(kwargs)
    node = Node(**data)
    session.add(node)
    await session.commit()
    return node


# ------------------------------------------------------------------ панель
async def test_missing_inbound_id_names_actual_panel_inbounds():
    """Ошибка панели не просто «проверь inbound_ids», а с фактическими ID."""
    fake = FakeXui(inbounds=[_vless_inbound(1), _wireguard_inbound(2)])
    panel, client = make_panel(fake, inbound_ids=[3])

    async with client:
        try:
            await panel.list_inbounds()
            raise AssertionError("ожидали PanelInboundMissing")
        except PanelInboundMissing as exc:
            message = str(exc)
        raw = await panel.list_all_inbounds()

    assert "не найдены инбаунды [3]" in message
    # Фактический состав панели — то, что оператор впишет в форму.
    assert "DE-Reality" in message and "DE-AmneziaWG" in message
    assert [item.id for item in raw] == [1, 2], "сырой список не должен фильтроваться"
    # Отдельный класс ошибки: вызывающий код обязан отличать её от падения панели.
    assert isinstance(missing_inbound_error(), PanelError)


async def test_panel_inbound_missing_is_not_a_panel_failure():
    """``check_health`` отвечает «панель жива», хотя выдать конфиг нечем."""
    fake = FakeXui(inbounds=[_vless_inbound(1)])
    panel, client = make_panel(fake, inbound_ids=[3])

    async with client:
        ok, error = await panel.check_health()
        with pytest.raises(PanelInboundMissing):
            await panel.list_inbounds()

    assert ok is True and error == ""


async def test_check_health_keeps_reason_of_dead_panel():
    """Раньше причина падения терялась: ``health()`` возвращает только bool."""

    def panic(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"success": False, "msg": "panic"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(panic))
    panel = XuiPanel(BASE, token=TOKEN, client=client)

    async with client:
        ok, error = await panel.check_health()

    assert ok is False
    assert "HTTP 500" in error


# ------------------------------------------------------------- проверка нод
async def test_missing_inbound_id_is_degraded_and_node_not_marked_alive(session):
    """Алерт один и честный, а бейдж «отвечает» не врёт про готовность."""
    node = await _node(session)
    results = await alerts_service.check_nodes(session, [(node, ReasonPanel(inbound_error=missing_inbound_error()))])
    await session.commit()

    assert results[0]["ok"] is True, "панель ответила — это не падение ноды"
    assert node.last_check_ok is False, "выдать конфиг нечем, значит нода не готова"
    assert "инбаунды [3]" in node.last_check_error

    opened = await alerts_service.list_alerts(session, status="open")
    assert [alert.kind for alert in opened] == ["node_degraded"]
    assert opened[0].fingerprint == "node:nl:inbounds"
    assert "инбаунды [3]" in (opened[0].message or "")


async def test_panel_error_is_not_reported_as_missing_inbounds(session):
    """Ошибка панели — другой вид алерта: заголовок и действие оператора иные."""
    node = await _node(session, inbound_ids="")
    panel = ReasonPanel(inbound_error=PanelError("неожиданный формат ответа /panel/api/inbounds/list"))
    await alerts_service.check_nodes(session, [(node, panel)])
    await session.commit()

    opened = await alerts_service.list_alerts(session, status="open")
    assert [alert.kind for alert in opened] == ["panel_error"]
    assert opened[0].fingerprint == "node:nl:panel"
    assert "нет инбаундов" not in opened[0].title


async def test_node_down_alert_keeps_reason_from_panel(session):
    """Падение ноды объясняет себя: таймаут это или 401."""
    node = await _node(session)
    panel = ReasonPanel(health_error="панель http://203.0.113.5:2053 не ответила за 15 с (GET /panel/api/inbounds/list)")
    await alerts_service.check_nodes(session, [(node, panel)])
    await session.commit()

    opened = await alerts_service.list_alerts(session, status="open")
    assert opened[0].kind == "node_down"
    assert "не ответила за 15 с" in (opened[0].message or "")


async def test_ack_is_not_reset_by_repeated_check(session):
    """Взятый в работу алерт остаётся в работе: повтор не отменяет решение человека."""
    node = await _node(session)
    panel = ReasonPanel(inbound_error=missing_inbound_error())
    await alerts_service.check_nodes(session, [(node, panel)])
    await session.commit()

    alert = (await alerts_service.list_alerts(session, status="open"))[0]
    await alerts_service.ack_alert(session, alert, by="владелец")
    await session.commit()

    await alerts_service.check_nodes(session, [(node, panel)])
    await session.commit()

    assert alert.status == "ack"
    assert alert.resolved_by == "владелец"
    assert alert.repeat_count == 2


async def test_check_nodes_closes_alerts_of_disabled_node(session):
    """Выключенную ноду не проверяем — но и алерты за ней не оставляем."""
    node = await _node(session, is_active=False)
    await alerts_service.raise_alert(session, "node_degraded", fingerprint="node:nl:inbounds")
    await session.commit()

    results = await alerts_service.check_nodes(session, [(node, ReasonPanel())])
    await session.commit()

    assert results == []
    assert int(await alerts_service.count_alerts(session, "open")) == 0


async def test_resolve_node_alerts_closes_every_fingerprint(session):
    """У ноды четыре вида проблем, и удаление закрывает все, а не только доступность."""
    node = await _node(session)
    for suffix, kind in (
        ("", "node_down"),
        (":inbounds", "node_degraded"),
        (":panel", "panel_error"),
        (":probe", "node_probe_failed"),
    ):
        await alerts_service.raise_alert(session, kind, fingerprint=f"node:nl{suffix}", node_id=node.id)
    await session.commit()
    assert int(await alerts_service.count_alerts(session, "open")) == 4

    closed = await alerts_service.resolve_node_alerts(session, "nl", note="нода удалена")
    await session.commit()

    assert closed == 4
    assert int(await alerts_service.count_alerts(session, "open")) == 0
    assert int(await alerts_service.count_alerts(session, "resolved")) == 4


# -------------------------------------------------------------------- проба
async def test_probe_does_not_blame_port_when_panel_gave_no_inbounds(session):
    """Главная регрессия: порт не проверялся — «порт не пускает» запрещено."""
    node = await _node(session, host="203.0.113.5", last_probe_ok=True, last_probe_ms=42)
    panel = ReasonPanel(inbound_error=missing_inbound_error())

    results = await alerts_service.check_probes(session, [(node, panel)])
    await session.commit()

    assert results[0]["ok"] is False
    assert results[0]["probed"] is False, "проба не дошла до порта"
    assert node.last_probe_ok is False
    assert "панель не отдала инбаунды" in node.last_probe_error
    assert "порт не проверялся" in node.last_probe_error
    # Ни «порт не пускает клиента», ни второй алерт на ту же причину.
    assert await alerts_service.list_alerts(session, status="open") == []


async def test_probe_retracts_stale_port_alert_when_it_stops_measuring(session, monkeypatch):
    """Пока причина не в порте — старый алерт «порт не пускает» закрывается."""
    node = await _node(session, host="203.0.113.5")
    panel = ReasonPanel()

    async def failing_probe(_host, _port, *, sni="", timeout=None):  # noqa: ANN001 - двойник
        return probe_service.ProbeResult(False, stage="tcp", detail="Kometa-Reality: таймаут TCP (5.0 с)")

    monkeypatch.setattr(probe_service, "probe_endpoint", failing_probe)
    measured = await alerts_service.check_probes(session, [(node, panel)])
    await session.commit()
    assert measured[0]["probed"] is True
    assert [alert.kind for alert in await alerts_service.list_alerts(session, status="open")] == [
        "node_probe_failed"
    ]

    # Панель перестала отдавать инбаунды: проба уже не про порт.
    panel.inbound_error = missing_inbound_error()
    results = await alerts_service.check_probes(session, [(node, panel)])
    await session.commit()

    assert results[0]["probed"] is False
    assert int(await alerts_service.count_alerts(session, "open")) == 0
    resolved = await session.scalars(select(Alert).where(Alert.kind == "node_probe_failed"))
    assert resolved.all()[0].status == "resolved"


async def test_failed_probe_still_alerts_when_port_really_closed(session, monkeypatch):
    """Настоящая сетевая проблема по-прежнему поднимает критичный алерт."""
    node = await _node(session, host="203.0.113.5")
    panel = ReasonPanel()

    async def failing_probe(_host, _port, *, sni="", timeout=None):  # noqa: ANN001 - двойник
        return probe_service.ProbeResult(False, stage="tcp", detail="Kometa-Reality: таймаут TCP (5.0 с)")

    monkeypatch.setattr(probe_service, "probe_endpoint", failing_probe)
    await alerts_service.check_probes(session, [(node, panel)])
    await session.commit()

    opened = await alerts_service.list_alerts(session, status="open")
    assert opened[0].kind == "node_probe_failed"
    assert opened[0].severity == "err"
    assert "порт не пускает клиента" in opened[0].title


async def test_probe_job_stays_silent_about_port_when_panel_gave_no_inbounds(session, bot, monkeypatch):
    """В Telegram не должно быть «порт не пускает», если порт не проверяли.

    Про ту же причину сообщает проверка нод (`job_node_health`), поэтому у
    пробы тут отдельного сообщения нет — иначе на одну причину уходило бы два.
    """
    from app import main as app_main
    from app.panels.registry import registry
    from app.services import notifications

    await _node(session, host="203.0.113.5")
    sent: list[str] = []

    async def fake_notify(bot_, text, **kwargs):  # noqa: ANN001 - двойник
        sent.append(text)

    monkeypatch.setattr(notifications, "notify_admins", fake_notify)
    panel = ReasonPanel(inbound_error=missing_inbound_error())
    monkeypatch.setattr(registry, "for_node", lambda node: panel)
    app_main.job_node_probe.__dict__.pop("state", None)

    await app_main.job_node_probe(bot)

    assert sent == [], "причина не в порте — сообщение о порте было бы ложным"


async def test_probe_job_reports_real_port_failure(session, bot, monkeypatch):
    """А вот настоящий провал пробы по порту обязан быть в Telegram."""
    from app import main as app_main
    from app.panels.registry import registry
    from app.services import notifications

    node = await _node(session, host="203.0.113.5")
    sent: list[str] = []

    async def fake_notify(bot_, text, **kwargs):  # noqa: ANN001 - двойник
        sent.append(text)

    async def failing_probe(_host, _port, *, sni="", timeout=None):  # noqa: ANN001 - двойник
        return probe_service.ProbeResult(False, stage="tcp", detail="Kometa-Reality: таймаут TCP (5.0 с)")

    monkeypatch.setattr(notifications, "notify_admins", fake_notify)
    monkeypatch.setattr(probe_service, "probe_endpoint", failing_probe)
    monkeypatch.setattr(registry, "for_node", lambda n: ReasonPanel())
    app_main.job_node_probe.__dict__.pop("state", None)

    await app_main.job_node_probe(bot)
    await session.refresh(node)

    assert sent and "порт не пускает клиента" in sent[0]
    assert "таймаут TCP" in sent[0]
    assert node.last_probe_stage == "tcp"


async def test_health_job_reports_degraded_panel_with_reason(session, bot, monkeypatch):
    """«Панель отвечает, но выдать нечего» — это сообщение админам, а не тишина.

    Раньше такое состояние не попадало в Telegram вообще: проверка смотрела
    только на «панель ответила».
    """
    from app import main as app_main
    from app.panels.registry import registry
    from app.services import notifications

    node = await _node(session, host="203.0.113.5")
    sent: list[str] = []

    async def fake_notify(bot_, text, **kwargs):  # noqa: ANN001 - двойник
        sent.append(text)

    monkeypatch.setattr(notifications, "notify_admins", fake_notify)
    monkeypatch.setattr(registry, "primary", lambda: ReasonPanel())
    monkeypatch.setattr(
        registry, "for_node", lambda n: ReasonPanel(inbound_error=missing_inbound_error())
    )
    app_main.job_node_health.__dict__.pop("state", None)

    await app_main.job_node_health(bot)

    assert sent, "о сломанной выдаче админы должны узнать из Telegram"
    assert "панель отвечает с ошибкой" in sent[0]
    assert "инбаунды [3]" in sent[0]
    assert node.last_check_ok is False


# ------------------------------------------------------- что видит оператор
async def test_connect_page_marks_unprobed_node_as_no_data(session):
    """Страница подключения не называет мёртвой локацию, порт которой не проверяли."""
    from app.web.sub import _locations_html

    node = await _node(session, host="203.0.113.5")
    await alerts_service.check_probes(session, [(node, ReasonPanel(inbound_error=missing_inbound_error()))])
    await session.commit()

    html = _locations_html([node])

    assert node.last_probe_stage == "panel"
    assert "нет данных пробы" in html
    assert "⚪" in html
    assert "не отвечает" not in html


async def test_connect_page_still_marks_dead_port_as_dead(session, monkeypatch):
    """А вот реально закрытый порт виден клиенту как «не отвечает»."""
    from app.web.sub import _locations_html

    node = await _node(session, host="203.0.113.5")

    async def failing_probe(_host, _port, *, sni="", timeout=None):  # noqa: ANN001 - двойник
        return probe_service.ProbeResult(False, stage="tcp", detail="Kometa-Reality: таймаут TCP (5.0 с)")

    monkeypatch.setattr(probe_service, "probe_endpoint", failing_probe)
    await alerts_service.check_probes(session, [(node, ReasonPanel())])
    await session.commit()

    html = _locations_html([node])

    assert node.last_probe_stage == "tcp"
    assert "🔴" in html
    assert "не отвечает" in html
    assert "нет данных пробы" not in html


async def test_emergency_report_does_not_send_to_firewall_when_probe_never_ran(session):
    """Готовность аварийного уровня: совет «проверь firewall» был неверным действием."""
    from app.tools import emergency_check as check

    node = await _node(session, host="203.0.113.5", channel="reserve")
    await alerts_service.check_probes(session, [(node, ReasonPanel(inbound_error=missing_inbound_error()))])
    await session.commit()

    report = check.assess([node], ping_ok=True, ping_url="https://sub.example/ping")

    assert any("проба не выполнена" in line for line in report.lines)
    assert not any("порт не пускает" in line for line in report.lines)
    assert any("ни одну пробу не удалось выполнить" in issue for issue in report.issues)
    assert not any("firewall" in issue for issue in report.issues)


async def test_emergency_report_sends_to_firewall_when_port_really_closed(session, monkeypatch):
    """Обратная сторона: закрытый порт — это именно сеть, и совет верный."""
    from app.tools import emergency_check as check

    node = await _node(session, host="203.0.113.5", channel="reserve")

    async def failing_probe(_host, _port, *, sni="", timeout=None):  # noqa: ANN001 - двойник
        return probe_service.ProbeResult(False, stage="tcp", detail="Kometa-Reality: таймаут TCP (5.0 с)")

    monkeypatch.setattr(probe_service, "probe_endpoint", failing_probe)
    await alerts_service.check_probes(session, [(node, ReasonPanel())])
    await session.commit()

    report = check.assess([node], ping_ok=True, ping_url="https://sub.example/ping")

    assert any("порт не пускает" in line for line in report.lines)
    assert any("проверь порты и firewall" in issue for issue in report.issues)
    assert not any("сверь ID" in issue for issue in report.issues)


async def test_udp_only_node_is_not_reported_as_failure(session):
    """Канал только на AmneziaWG: TCP-проба к нему неприменима, это не авария."""
    from app.tools import emergency_check as check

    node = await _node(session, host="203.0.113.5", channel="reserve")
    panel = ReasonPanel(inbounds=0)

    results = await alerts_service.check_probes(session, [(node, panel)])
    await session.commit()

    assert results[0]["stage"] == "config"
    assert results[0]["verdict"] == "not_configured"
    assert await alerts_service.list_alerts(session, status="open") == []

    report = check.assess([node], ping_ok=True, ping_url="https://sub.example/ping")
    assert any("проба не настроена" in line for line in report.lines)
    assert not any("порт не пускает" in line for line in report.lines)
    assert not any("сверь ID" in issue for issue in report.issues)


# --------------------------------------------------------------- диагностика
async def test_diagnosis_tool_shows_configured_against_actual(session, monkeypatch):
    """Инструмент отвечает на вопрос «какие ID в панели есть» без похода в 3x-ui."""
    from app.panels.registry import registry
    from app.tools import check_nodes as tool

    node = await _node(session, panel_type="xui", panel_url="http://203.0.113.5:2053/nl", host="203.0.113.5")
    panel = ReasonPanel(inbound_error=missing_inbound_error())

    async def fake_pairs(_session):  # noqa: ANN001 - двойник реестра
        return [(node, panel)]

    monkeypatch.setattr(registry, "all_panels_with_nodes", fake_pairs)

    report = await tool.collect(session)

    assert report.ok is False
    assert report.panels[0].configured == [3]
    assert report.panels[0].missing == [3]
    assert report.panels[0].actual_ids == [1, 2]

    text = report.as_text()
    assert "нет настроенных ID: 3" in text
    assert "Kometa-1" in text
    assert "/admin/nodes?edit=nl" in text


async def test_diagnosis_tool_warns_about_duplicate_panel_url(session, monkeypatch):
    """Дубль адреса — версия «панель опрашивается дважды с разными ID» (F18)."""
    from app.panels.registry import registry
    from app.tools import check_nodes as tool

    node = await _node(session, panel_type="xui", panel_url="http://203.0.113.5:2053")
    primary = ReasonPanel()
    primary.location_title = "🇩🇪 Германия"
    primary.base_url = "http://203.0.113.5:2053"

    async def fake_pairs(_session):  # noqa: ANN001 - двойник реестра
        return [(None, primary), (node, ReasonPanel())]

    monkeypatch.setattr(registry, "all_panels_with_nodes", fake_pairs)

    report = await tool.collect(session)

    assert any("указан дважды" in warning for warning in report.warnings)


async def test_diagnosis_tool_is_quiet_when_everything_matches(session, monkeypatch):
    """Здоровая конфигурация: отчёт зелёный и код возврата 0."""
    from app.panels.registry import registry
    from app.tools import check_nodes as tool

    node = await _node(session, inbound_ids="1, 2")

    async def fake_pairs(_session):  # noqa: ANN001 - двойник реестра
        return [(node, ReasonPanel())]

    monkeypatch.setattr(registry, "all_panels_with_nodes", fake_pairs)

    report = await tool.collect(session)

    assert report.ok is True
    assert report.panels[0].missing == []
    assert "✅ сходится" in report.as_text()


__all__ = ["ReasonPanel", "missing_inbound_error"]


# ------------------------------------------------- разбор строки с ID инбаундов
def test_parse_inbound_ids_treats_spaces_as_separators():
    """Три парсера строки сведены в один: «1 2» — это два инбаунда, не двенадцатый."""
    from app.panels.base import parse_inbound_ids

    assert parse_inbound_ids("1 2") == [1, 2]
    assert parse_inbound_ids("1, 2;3") == [1, 2, 3]
    assert parse_inbound_ids("3") == [3]
    assert parse_inbound_ids("") == []
    assert parse_inbound_ids("мусор") == []
    assert parse_inbound_ids("1,1,2") == [1, 2], "повторы схлопываются"


def test_normalize_inbound_ids_reports_garbage_instead_of_widening():
    """«3x» — ошибка, а не пустая строка: пустая строка значит «все инбаунды»."""
    from app.panels.base import normalize_inbound_ids

    assert normalize_inbound_ids("1 2") == ("1,2", "")
    assert normalize_inbound_ids("") == ("", "")
    value, problem = normalize_inbound_ids("3x")
    assert value == "" and "3x" in problem
    value, problem = normalize_inbound_ids("1," + "2" * 64)
    assert value == "" and "Слишком много" in problem


async def test_registry_and_settings_parse_ids_the_same_way(session):
    """Реестр и настройки используют один разбор: раньше «1 2» давало [12]."""
    from app.config import get_settings
    from app.panels.registry import registry

    node = await _node(session, inbound_ids="1 2", panel_type="xui", panel_url="http://203.0.113.5:2053")
    panel = registry.for_node(node)

    assert panel.inbound_ids == [1, 2]

    settings = get_settings()
    original = settings.panel_inbound_ids
    try:
        settings.panel_inbound_ids = "1 2"
        assert settings.inbound_id_list == [1, 2]
    finally:
        settings.panel_inbound_ids = original


def test_panel_label_prefers_country_over_service_name():
    """В логах и в /nodes должна быть страна: у всех xui-панелей имя «xui»."""
    from app.panels.base import panel_label
    from app.panels.fake import FakePanel

    panel = FakePanel()
    assert panel_label(panel) == "fake", "без названия остаётся служебное имя"

    panel.location_title = "🇩🇪 Германия"
    assert panel_label(panel) == "🇩🇪 Германия"


async def test_diagnosis_tool_notes_when_there_are_no_nodes(session, monkeypatch):
    """«✅ сходятся» на базе без нод — не «всё в порядке», а «сравнивать нечего»."""
    from app.panels.registry import registry
    from app.tools import check_nodes as tool

    async def fake_pairs(_session):  # noqa: ANN001 - двойник реестра
        return [(None, ReasonPanel())]

    monkeypatch.setattr(registry, "all_panels_with_nodes", fake_pairs)

    report = await tool.collect(session)

    assert any("активных нод нет" in warning for warning in report.warnings)
    assert "активных нод нет" in report.as_text()


async def test_registry_orders_nodes_like_the_admin_page(session):
    """Страница нод склеивает карточки панелей с нодами по индексу.

    Значит порядок нод в реестре обязан совпадать с порядком на странице и в
    планировщике (``priority, id``): при равных приоритетах разный ``ORDER BY``
    приклеил бы инбаунды одной страны к карточке другой.
    """
    from sqlalchemy import select

    from app.db.models import Node as NodeModel
    from app.panels.registry import registry

    session.add_all(
        [
            NodeModel(code="aa", title="A", priority=100, is_active=True, panel_type="fake"),
            NodeModel(code="bb", title="B", priority=100, is_active=True, panel_type="fake"),
        ]
    )
    await session.commit()

    pairs = await registry.all_panels_with_nodes(session)
    registry_order = [node.code for node, _ in pairs if node is not None]

    expected = list(
        (
            await session.scalars(
                select(NodeModel)
                .where(NodeModel.is_active.is_(True))
                .order_by(NodeModel.priority, NodeModel.id)
            )
        ).all()
    )
    assert registry_order == [node.code for node in expected] == ["aa", "bb"]


async def test_panel_views_survive_panel_without_raw_list(session):
    """Сторонняя панель без ``list_all_inbounds`` не должна «ломаться» на карточке."""
    from app.web.admin import infra

    class DuckPanel:  # не наследник PanelClient
        name = "duck"
        location_title = "🇳🇱 Нидерланды"

        async def list_inbounds(self) -> list[Inbound]:
            return [Inbound(id=1, remark="R", protocol="vless", port=443, network="tcp")]

    node = await _node(session, inbound_ids="1", panel_type="xui")
    infra._inbounds_cache.clear()
    try:
        views = await infra._panel_views([ReasonPanel(), DuckPanel()], [node])
    finally:
        infra._inbounds_cache.clear()

    assert views[1]["error"] == ""
    assert [item.id for item in views[1]["inbounds"]] == [1]
    assert views[1]["missing"] == []


async def test_panel_views_do_not_blame_fake_panel_for_ids(session):
    """Заглушка панели игнорирует inbound_ids: «нет ID 3» на ней было бы выдумкой."""
    from app.web.admin import infra

    node = await _node(session, inbound_ids="3", panel_type="fake")
    infra._inbounds_cache.clear()
    try:
        views = await infra._panel_views([ReasonPanel(), ReasonPanel()], [node])
    finally:
        infra._inbounds_cache.clear()

    assert views[1]["configured"] == []
    assert views[1]["missing"] == []


async def test_diagnosis_tool_skips_id_check_for_fake_panel(session, monkeypatch):
    """Заглушка панели не применяет inbound_ids — «нет ID 3» на ней было бы выдумкой."""
    from app.panels.registry import registry
    from app.tools import check_nodes as tool

    node = await _node(session, inbound_ids="3", panel_type="fake")

    async def fake_pairs(_session):  # noqa: ANN001 - двойник реестра
        return [(node, ReasonPanel())]

    monkeypatch.setattr(registry, "all_panels_with_nodes", fake_pairs)

    report = await tool.collect(session)

    assert report.ok is True
    assert report.panels[0].missing == []
    assert report.panels[0].note
    assert "сверка ID пропущена" in report.as_text()


async def test_diagnosis_tool_notes_inbounds_outside_settings(session, monkeypatch):
    """Инбаунд есть в панели, но не в настройках: «сходится» ≠ «всё включено»."""
    from app.panels.registry import registry
    from app.tools import check_nodes as tool

    node = await _node(session, inbound_ids="1", panel_type="xui", panel_url="http://203.0.113.5:2053")

    async def fake_pairs(_session):  # noqa: ANN001 - двойник реестра
        return [(node, ReasonPanel(inbounds=3))]

    monkeypatch.setattr(registry, "all_panels_with_nodes", fake_pairs)

    report = await tool.collect(session)

    assert report.ok is True, "отсутствующих ID нет — это не авария"
    assert report.panels[0].extra == [2, 3]
    assert "вне настроек" in report.as_text()
