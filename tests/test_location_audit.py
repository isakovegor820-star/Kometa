"""Тесты аудита локаций: шесть вердиктов и доказательства к каждому.

Сеть не используется: панель — двойник в памяти, TCP-проба и проба сервиса
подписок подменяются. Проверяется главное — инструмент **не путает** три разных
диагноза, которые раньше выглядели одинаково:

* «панель не отвечает» — панель не дала список, порт при этом может пускать;
* «порт не пускает» — панель здорова, а клиент не подключится;
* «конфиг не выдаётся» — порт пускает, но локация молча выпала из подписки.

Плюс режим ``--repeat``: панель, отвечающая через раз, не должна ни хоронить
локацию, ни выглядеть здоровой.
"""

from __future__ import annotations

import json

import pytest

from app.db.models import Node
from app.panels.base import Inbound, PanelClient, PanelError, PanelInboundMissing, PanelUser
from app.panels.registry import registry
from app.services import probe as probe_service
from app.tools import location_audit as tool

PUBLIC_HOST = "203.0.113.5"
PANEL_URL = "http://203.0.113.5:2053"
SUB_BASE = "http://203.0.113.5:2096/sub/"
CLIENT_UUID = "11111111-2222-3333-4444-555555555555"
OTHER_UUID = "99999999-8888-7777-6666-555555555555"
VLESS = f"vless://{CLIENT_UUID}@{PUBLIC_HOST}:443?type=tcp&security=reality#nl-443"


class LayerPanel(PanelClient):
    """Панель-двойник: каждый слой ломается отдельно от остальных.

    ``health_error`` — панель не отвечает совсем; ``flaky`` — отвечает через
    раз (каждая нечётная проверка мимо), так проверяется ``--repeat``.
    ``inbound_error`` — фильтрованный список падает (как при расхождении ID),
    а сырой отдаётся: ровно это делает ``XuiPanel``.
    """

    name = "xui"
    location_title = "🇳🇱 Нидерланды"

    def __init__(
        self,
        *,
        inbounds: list[Inbound] | None = None,
        health_error: str = "",
        flaky: bool = False,
        inbound_error: str = "",
        users: list[PanelUser] | None = None,
        configs: list[str] | None = None,
        config_error: str = "",
        sub_base: str = SUB_BASE,
        base_url: str = PANEL_URL,
    ) -> None:
        self.inbounds = list(inbounds) if inbounds is not None else [_tcp_inbound(1)]
        self.health_error = health_error
        self.flaky = flaky
        self.inbound_error = inbound_error
        self.users = list(users) if users is not None else [_user()]
        self.configs = list(configs) if configs is not None else [VLESS]
        self.config_error = config_error
        self.sub_base = sub_base
        self.base_url = base_url
        #: Счётчики обращений: ими проверяется, что ``--dry-run`` не ходит в сеть.
        self.calls: dict[str, int] = {}

    def _count(self, name: str) -> int:
        self.calls[name] = self.calls.get(name, 0) + 1
        return self.calls[name]

    async def check_health(self) -> tuple[bool, str]:
        number = self._count("health")
        if self.flaky and number % 2 == 1:
            return False, "панель не ответила за 5 с (попытка мимо)"
        if self.health_error:
            return False, self.health_error
        return True, ""

    async def health(self) -> bool:
        return not self.health_error

    async def list_inbounds(self) -> list[Inbound]:
        self._count("list")
        if self.inbound_error:
            raise PanelInboundMissing(self.inbound_error)
        return list(self.inbounds)

    async def list_all_inbounds(self) -> list[Inbound]:
        """Сырой список: фильтр по ``inbound_ids`` к нему не применяется."""
        self._count("list_all")
        return list(self.inbounds)

    async def list_users(self) -> list[PanelUser]:
        return list(self.users)

    async def get_configs(self, uuid: str) -> list[str]:
        if self.config_error:
            raise PanelError(self.config_error)
        return list(self.configs)

    # --- абстрактные методы: аудиту они не нужны -------------------------
    async def create_user(self, spec):  # noqa: ANN001, ANN201 - двойник
        raise NotImplementedError

    async def get_user(self, uuid: str):  # noqa: ANN201 - двойник
        return None

    async def update_user(self, uuid: str, **kwargs):  # noqa: ANN003, ANN201 - двойник
        raise NotImplementedError

    async def delete_user(self, uuid: str) -> None:
        raise NotImplementedError


# ------------------------------------------------------------------- помощники
def _tcp_inbound(inbound_id: int = 1, port: int = 443, remark: str = "Kometa-Reality-443") -> Inbound:
    return Inbound(id=inbound_id, remark=remark, protocol="vless", port=port, network="tcp", security="reality")


def _udp_inbound(inbound_id: int = 2, port: int = 51820) -> Inbound:
    return Inbound(id=inbound_id, remark="Kometa-AWG-51820", protocol="wireguard", port=port, network="udp")


def _user(uuid: str = CLIENT_UUID, email: str = "nl-1") -> PanelUser:
    return PanelUser(uuid=uuid, email=email)


async def _node(session, **kwargs) -> Node:  # noqa: ANN001 - AsyncSession
    """Нода «Нидерланды» — как в проде, если не сказано иное."""
    data = {
        "code": "nl",
        "title": "🇳🇱 Нидерланды",
        "host": PUBLIC_HOST,
        "panel_type": "xui",
        "panel_url": PANEL_URL,
        "inbound_ids": "1",
        "is_active": True,
    }
    data.update(kwargs)
    node = Node(**data)
    session.add(node)
    await session.commit()
    return node


def _wire(monkeypatch, pairs) -> None:  # noqa: ANN001 - pytest monkeypatch
    """Подменить реестр: аудит и check_nodes берут панели из одного места."""

    async def fake_pairs(_session):  # noqa: ANN001 - двойник реестра
        return pairs

    monkeypatch.setattr(registry, "all_panels_with_nodes", fake_pairs)


async def _probe_ok(host: str, port: int, *, sni: str = "", timeout: float | None = None):
    """Порт пускает: клиент дозвонится."""
    return probe_service.ProbeResult(True, ms=14, stage="tcp", detail="TCP открыт")


async def _probe_closed(host: str, port: int, *, sni: str = "", timeout: float | None = None):
    """Порт не пускает: таймаут TCP — самый частый ответ фаервола."""
    limit = float(timeout or 5.0)
    return probe_service.ProbeResult(False, stage="tcp", detail=f"таймаут TCP ({limit:.1f} с)")


async def _sub_ok(base: str, timeout: float | None = None):
    """Сервис подписок жив: отвечает хоть чем-то (обычно 404 на чужого клиента)."""
    return True, 404, ""


@pytest.fixture
def network(monkeypatch):  # noqa: ANN001 - pytest monkeypatch
    """Сеть в тестах не нужна: порт пускает, сервис подписок отвечает.

    Кто проверяет отказ — переопределяет нужную пробу у себя.
    """
    monkeypatch.setattr(probe_service, "probe_endpoint", _probe_ok)
    monkeypatch.setattr(tool, "_probe_sub_service", _sub_ok)
    return monkeypatch


async def _collect(session, monkeypatch, pairs, **kwargs):  # noqa: ANN001 - pytest monkeypatch
    _wire(monkeypatch, pairs)
    return await tool.collect(session, **kwargs)


# -------------------------------------------------------------------- вердикты
async def test_working_location_is_reported_as_working(session, network, monkeypatch):
    """(а) Все слои сошлись — «работает», и видно, чем это подтверждено."""
    node = await _node(session, inbound_ids="1,4")
    panel = LayerPanel(inbounds=[_tcp_inbound(1, 443), _tcp_inbound(4, 8443, "Kometa-Reality-8443")])

    report = await _collect(session, monkeypatch, [(node, panel)])
    item = report.locations[0]
    text = report.as_text()

    assert item.verdict == tool.VERDICT_OK
    assert report.ok is True
    assert "✅ работает" in text
    # Проба идёт по КАЖДОМУ TCP-инбаунду, а не «по первому успешному».
    assert "порт 443" in text and "порт 8443" in text
    assert "✅ сходятся" in text
    assert f"host в конфигах: {PUBLIC_HOST}:443" in text
    assert any("подтверждено" in reason for reason in item.reasons)


async def test_panel_answers_but_port_is_closed(session, network, monkeypatch):
    """(б) Панель здорова, порт закрыт: причина — именно порт, и это видно."""
    node = await _node(session, inbound_ids="1,4")
    panel = LayerPanel(inbounds=[_tcp_inbound(1, 443), _tcp_inbound(4, 8443, "Kometa-Reality-8443")])
    monkeypatch.setattr(probe_service, "probe_endpoint", _probe_closed)

    report = await _collect(session, monkeypatch, [(node, panel)])
    item = report.locations[0]
    text = report.as_text()

    assert item.verdict == tool.VERDICT_PORT_CLOSED
    assert report.ok is False
    assert "панель:     ✅ отвечает" in text, "панель не виновата — и отчёт это показывает"
    assert "порт 443" in text and "❌ не пускает" in text and "таймаут TCP" in text
    assert any("ни один TCP-порт" in reason for reason in item.reasons)
    assert "фаервол" in text or "firewall" in text
    # Подписка проверена отдельно: даже с закрытым портом видно, выдаются ли конфиги.
    assert item.sub.configs == 1


async def test_panel_is_down_but_port_still_accepts_clients(session, network, monkeypatch):
    """(в) Панель не отвечает, порт открыт — это НЕ «локация мертва»."""
    node = await _node(session)
    panel = LayerPanel(health_error="панель http://203.0.113.5:2053 не ответила за 5 с")

    report = await _collect(session, monkeypatch, [(node, panel)])
    item = report.locations[0]
    text = report.as_text()

    assert item.verdict == tool.VERDICT_PANEL_DOWN
    assert item.panel.rate == "0/1", "панель не ответила ни разу — это и есть доказательство"
    assert "не ответила за 5 с" in text
    assert "порт 443" in text and "✅ открыт" in text
    assert any("НЕ «локация мертва»" in reason for reason in item.reasons)


async def test_empty_sub_base_means_location_is_not_issued(session, network, monkeypatch):
    """(г) Адреса подписок нет: локация молча выпадет из подписки клиента."""
    node = await _node(session, host="", sub_base="")
    panel = LayerPanel()

    report = await _collect(session, monkeypatch, [(node, panel)])
    item = report.locations[0]

    assert item.verdict == tool.VERDICT_NO_CONFIGS
    assert item.sub.configured is False
    assert "Адрес сервиса подписок" in report.as_text()
    assert any("sub_base" in reason for reason in item.reasons)


async def test_inbound_ids_mismatch_is_a_setup_problem(session, network, monkeypatch):
    """(д) ID расходятся: панель и порт в порядке, ломается выдача новым клиентам."""
    node = await _node(session, inbound_ids="3")
    panel = LayerPanel(inbound_error="в панели не найдены инбаунды [3] (проверь inbound_ids)")

    report = await _collect(session, monkeypatch, [(node, panel)])
    item = report.locations[0]
    text = report.as_text()

    assert item.verdict == tool.VERDICT_IDS_MISMATCH
    assert item.missing_ids == [3]
    assert item.actual_ids == [1]
    assert "❌ нет настроенных ID: 3" in text
    assert "sync_inbounds" in text, "после правки ID клиентам нужно досоздать профили"


async def test_duplicate_panel_url_is_warned(session, network, monkeypatch):
    """(е) Та же панель под двумя странами: «NL» на самом деле DE, и это видно."""
    node = await _node(session)
    primary = LayerPanel(base_url=PANEL_URL)
    primary.location_title = "🇩🇪 Германия"

    report = await _collect(session, monkeypatch, [(None, primary), (node, LayerPanel())])

    assert any("указан дважды" in warning for warning in report.warnings)
    assert "указан дважды" in report.as_text()


async def test_location_without_address_and_stub_panel_has_nothing_to_check(session, network, monkeypatch):
    """Заглушка панели без адреса: судить не о чем — и это сказано прямо."""
    node = await _node(session, host="", panel_type="fake")

    report = await _collect(session, monkeypatch, [(node, LayerPanel())])
    item = report.locations[0]

    assert item.verdict == tool.VERDICT_NOTHING
    assert item.ids_checked is False
    assert item.sub.skipped is True
    assert "нечего проверять" in report.as_text()


# ------------------------------------------------------------------- repeat/json
async def test_repeat_shows_panel_answering_every_other_time(session, network, monkeypatch):
    """Панель «через раз»: доля успехов видна и не превращается в приговор."""
    node = await _node(session)
    panel = LayerPanel(flaky=True)

    report = await _collect(session, monkeypatch, [(node, panel)], repeat=3)
    item = report.locations[0]
    text = report.as_text()

    assert item.panel.attempts == 3
    assert item.panel.rate == "2/3"
    assert item.panel.flaky is True
    assert "отвечает через раз" in text
    assert any("через раз" in warning for warning in item.warnings)
    # Локация при этом жива: порт пускает, конфиги выдаются.
    assert item.verdict == tool.VERDICT_OK


async def test_repeated_port_failure_counts_successes(session, network, monkeypatch):
    """Порт отвечает один раз из трёх — это не «порт не пускает»."""
    node = await _node(session)
    panel = LayerPanel()
    answers = {"left": 0}

    async def half_open(host: str, port: int, *, sni: str = "", timeout: float | None = None):
        answers["left"] += 1
        if answers["left"] == 1:
            return probe_service.ProbeResult(True, ms=12, stage="tcp", detail="TCP открыт")
        return probe_service.ProbeResult(False, stage="tcp", detail="таймаут TCP (5.0 с)")

    monkeypatch.setattr(probe_service, "probe_endpoint", half_open)

    report = await _collect(session, monkeypatch, [(node, panel)], repeat=3)
    item = report.locations[0]

    assert item.ports[0].stat.rate == "1/3"
    assert item.ports[0].ok is True
    assert item.verdict == tool.VERDICT_OK
    assert any("через раз" in warning for warning in item.warnings)


async def test_json_output_is_safe_to_paste_into_chat(session, network, monkeypatch):
    """JSON можно переслать целиком: UUID клиента и пароли из адресов вырезаны."""
    node = await _node(session, panel_url="http://admin:secret@203.0.113.5:2053")

    report = await _collect(session, monkeypatch, [(node, LayerPanel())])
    payload = json.dumps(report.as_dict(), ensure_ascii=False)

    assert CLIENT_UUID not in payload
    assert "secret" not in payload
    assert f"{tool.VERDICT_OK}" in payload
    item = report.locations[0]
    assert item.sub.sample == f"vless://…@{PUBLIC_HOST}:443"
    assert item.sub.hosts == [f"{PUBLIC_HOST}:443"]


async def test_dry_run_shows_plan_without_touching_the_network(session, monkeypatch):
    """--dry-run: показываем, что будет проверено, и не делаем ни одного запроса."""
    node = await _node(session, inbound_ids="1,4")
    panel = LayerPanel()
    _wire(monkeypatch, [(node, panel)])

    async def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201 - сеть запрещена
        raise AssertionError("dry-run не должен ходить в сеть")

    monkeypatch.setattr(probe_service, "probe_endpoint", boom)
    monkeypatch.setattr(tool, "_probe_sub_service", boom)

    report = await tool.collect(session, dry_run=True)

    assert panel.calls == {}, f"панель трогали в dry-run: {panel.calls}"
    assert report.ok is True
    assert report.dry_run is True
    assert "dry-run" in report.as_text()


# ------------------------------------------------------------ крайние состояния
async def test_udp_only_location_is_not_called_dead(session, network, monkeypatch):
    """Только AmneziaWG: TCP-проба неприменима — это не авария порта."""
    node = await _node(session, inbound_ids="2")
    panel = LayerPanel(inbounds=[_udp_inbound(2)])

    report = await _collect(session, monkeypatch, [(node, panel)])
    item = report.locations[0]

    assert item.ports == []
    assert item.verdict == tool.VERDICT_OK
    assert "TCP-инбаундов" in item.ports_note
    assert "не пускает" not in report.as_text()


async def test_location_missing_for_all_clients_is_not_issued(session, network, monkeypatch):
    """Клиенты есть, но профиля на локации нет ни у одного — локация не выдаётся."""
    node = await _node(session)
    panel = LayerPanel(
        users=[_user(CLIENT_UUID, "nl-1"), _user(OTHER_UUID, "nl-2")],
        config_error="пользователь … не найден в панели 3x-ui",
    )

    report = await _collect(session, monkeypatch, [(node, panel)])
    item = report.locations[0]

    assert item.verdict == tool.VERDICT_NO_CONFIGS
    assert item.sub.clients_checked == 2
    assert "ни у одного из 2" in report.as_text()
    assert "sync_inbounds" in report.as_text()


async def test_config_host_mismatch_warns_that_client_goes_elsewhere(session, network, monkeypatch):
    """Адрес в конфигах чужой: клиент подключается не туда, где шла проверка."""
    node = await _node(session)
    panel = LayerPanel(configs=[f"vless://{CLIENT_UUID}@198.51.100.9:443?type=tcp#nl"])

    report = await _collect(session, monkeypatch, [(node, panel)])
    item = report.locations[0]

    assert item.verdict == tool.VERDICT_OK
    assert any("клиент пойдёт не туда" in warning for warning in item.warnings)
    assert "198.51.100.9:443" in report.as_text()


async def test_primary_panel_takes_client_host_from_sub_base(session, network, monkeypatch):
    """У основной панели нет nodes.host: адрес клиента берём из адреса подписок."""
    from app.config import get_settings as real_settings

    class _Settings:
        """Только те поля настроек, которые читают аудит и check_nodes."""

        panel_type = "xui"
        inbound_id_list = [1]

        def __getattr__(self, name: str):
            return getattr(real_settings(), name)

    primary = LayerPanel()
    primary.location_title = "🇩🇪 Германия"
    _wire(monkeypatch, [(None, primary)])
    monkeypatch.setattr("app.config.get_settings", lambda: _Settings())

    report = await tool.collect(session)
    item = report.locations[0]

    assert item.code == "primary"
    assert item.host == PUBLIC_HOST and item.host_source == "sub_base"
    assert item.verdict == tool.VERDICT_OK


async def test_disabled_background_probes_are_reported(session, network, monkeypatch):
    """Пробы выключены настройкой: аудит сходит на порты сам, но предупреждает.

    Иначе «нет данных пробы» в админке читается как «не успели проверить»,
    хотя на самом деле пробы не идут вовсе.
    """
    from app.config import get_settings

    node = await _node(session)
    _wire(monkeypatch, [(node, LayerPanel())])
    monkeypatch.setattr(get_settings(), "node_probe_enabled", False)

    report = await tool.collect(session)

    assert any("NODE_PROBE_ENABLED" in warning for warning in report.warnings)
    assert report.locations[0].ports[0].ok is True, "аудит проверяет порты и при выключенных пробах"


def test_safe_url_drops_credentials():
    """Токен в адресе панели не должен уехать в чат вместе с отчётом."""
    assert tool._safe_url("http://admin:secret@203.0.113.5:2053/path") == "http://203.0.113.5:2053/path"
    assert tool._safe_url("") == ""
    assert tool._safe_url("http://203.0.113.5:2053") == "http://203.0.113.5:2053"
