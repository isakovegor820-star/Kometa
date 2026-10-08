"""Проверка готовности аварийного уровня и каналы в подписке.

Тесты без сети и без панели: ``assess`` — чистая функция, а проверку точки
замера подменяем.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import yaml

from app.db.models import Node
from app.tools import emergency_check as check
from app.web.subscription_format import (
    RESERVE_MARK,
    build_clash_yaml,
    build_singbox_json,
    channel_mark,
    channel_of,
)
from tests.fakes import make_update


def node(**kwargs) -> Node:
    """Нода для проверки: заполняем всё, что читает отчёт.

    ``last_probe_stage="tcp"`` — состояние, которое пишет настоящая проба:
    порт проверялся. По этому шагу отчёт отличает «порт не пускает» от
    «пробу не удалось поставить».
    """
    data = {
        "code": "de",
        "title": "🇩🇪 Германия",
        "channel": "main",
        "priority": 100,
        "is_active": True,
        "last_probe_at": datetime.now(timezone.utc),
        "last_probe_ok": True,
        "last_probe_ms": 48,
        "last_probe_stage": "tcp",
    }
    data.update(kwargs)
    instance = Node(**data)
    instance.id = data.get("id", 1)
    return instance


# --------------------------------------------------- проверка готовности
def test_no_nodes_is_not_ready():
    report = check.assess([], ping_ok=True, ping_url="https://sub.example/ping")

    assert report.ok is False
    assert any("нет ни одной активной локации" in issue for issue in report.issues)


def test_only_main_nodes_means_no_emergency_channel():
    report = check.assess([node()], ping_ok=True, ping_url="https://sub.example/ping")

    assert report.ok is False
    assert any("аварийного канала" in issue for issue in report.issues)


def test_ready_when_reserve_node_pings_and_endpoint_answers():
    report = check.assess(
        [node(), node(id=2, code="nl", title="🇳🇱 Резерв", channel="reserve", last_probe_ms=91)],
        ping_ok=True,
        ping_url="https://sub.example/ping",
    )

    assert report.ok is True
    assert report.issues == []
    assert any("резерв" in line and "91 мс" in line for line in report.lines)


def test_cdn_channel_is_named_in_report():
    report = check.assess(
        [node(channel="cdn", title="🇷🇺 CDN-вход")],
        ping_ok=True,
        ping_url="https://sub.example/ping",
    )

    assert any("CDN" in line for line in report.lines)


def test_failed_probe_is_reported():
    report = check.assess(
        [node(channel="reserve", last_probe_ok=False, last_probe_ms=0)],
        ping_ok=True,
        ping_url="https://sub.example/ping",
    )

    assert report.ok is False
    assert any("проба не проходит" in issue for issue in report.issues)
    assert any("порт не пускает" in line for line in report.lines)


def test_stale_probe_is_reported():
    old = datetime.now(timezone.utc) - timedelta(minutes=45)
    report = check.assess(
        [node(channel="reserve", last_probe_at=old)],
        ping_ok=True,
        ping_url="https://sub.example/ping",
        probe_fresh_minutes=20,
    )

    assert report.ok is False
    assert any("устарели" in issue for issue in report.issues)


def test_missing_probe_is_reported():
    report = check.assess(
        [node(channel="reserve", last_probe_at=None, last_probe_ok=False)],
        ping_ok=True,
        ping_url="https://sub.example/ping",
    )

    assert report.ok is False
    assert any("не запускались" in issue for issue in report.issues)
    assert any("пробы ещё не было" in line for line in report.lines)


def test_unreachable_ping_endpoint_is_reported():
    report = check.assess(
        [node(channel="reserve")],
        ping_ok=False,
        ping_url="https://sub.example/ping",
    )

    assert report.ok is False
    assert any("точка замера" in issue and "/ping" in issue for issue in report.issues)


def test_missing_dns_tunnel_is_optional_not_blocking():
    """DNS-канал — дежурный: без него уровень работает, но возможностей меньше."""
    without = check.assess([node(channel="reserve")], ping_ok=True, ping_url="https://sub.example/ping")
    assert without.ok is True
    assert any("DNS-канал" in item for item in without.optional)
    assert "Можно добавить" in without.as_text()

    with_dns = check.assess(
        [node(channel="reserve")],
        ping_ok=True,
        ping_url="https://sub.example/ping",
        dns_domain="t.example.com",
    )
    assert with_dns.optional == []


async def test_run_uses_assessment(monkeypatch):
    async def fake_nodes():
        return [node(channel="reserve")]

    async def fake_ping(_url, timeout=3.0):  # noqa: ANN001 - двойник
        return True

    monkeypatch.setattr(check, "load_nodes", fake_nodes)
    monkeypatch.setattr(check, "check_ping", fake_ping)

    report = await check.run()

    assert report.ok is True
    assert "готов" in report.as_text()


async def test_bot_emergency_command_reports_readiness(session, monkeypatch, bot, dispatcher):
    """Админ проверяет готовность с телефона: /emergency присылает тот же отчёт."""

    async def fake_nodes():
        return [
            node(channel="main"),
            node(id=2, code="ru", title="🇷🇺 Резерв", channel="reserve", last_probe_ms=77),
        ]

    async def fake_ping(_url, timeout=3.0):  # noqa: ANN001 - двойник
        return True

    monkeypatch.setattr(check, "load_nodes", fake_nodes)
    monkeypatch.setattr(check, "check_ping", fake_ping)

    await dispatcher.feed_update(bot, make_update("/emergency", user_id=1))

    text = bot.session.all_text()
    assert "Аварийный уровень готов" in text
    assert "77 мс" in text


# --------------------------------------------------- каналы в подписке
def test_channel_helpers():
    assert channel_mark("reserve") == RESERVE_MARK
    assert channel_mark("cdn") == " · CDN"
    assert channel_mark("main") == ""
    assert channel_of("🇩🇪 Германия" + RESERVE_MARK) == "reserve"
    assert channel_of("🇷🇺 Вход · CDN") == "cdn"
    assert channel_of("🇩🇪 Германия") == ""


def _vless(name: str) -> str:
    return (
        "vless://11111111-2222-3333-4444-555555555555@1.2.3.4:443"
        f"?type=tcp&security=reality&fp=firefox&pbk=KEY&sni=www.microsoft.com&sid=ab12#{name}"
    )


def test_clash_makes_group_per_channel_with_own_test_url():
    document = yaml.safe_load(
        build_clash_yaml(
            [_vless("DE"), _vless("Резерв" + RESERVE_MARK), _vless("Вход · CDN")],
            title="Kometa",
            test_url="https://sub.example/ping",
            test_urls={"cdn": "https://cdn.example/ping"},
        )
    )

    groups = {group["name"]: group for group in document["proxy-groups"]}
    assert set(groups) == {"Kometa", "Kometa Резерв", "Kometa CDN"}
    assert groups["Kometa Резерв"]["url"] == "https://sub.example/ping"
    assert groups["Kometa CDN"]["url"] == "https://cdn.example/ping"
    assert groups["Kometa CDN"]["proxies"] == ["Вход · CDN"]
    assert document["rules"][-1] == "MATCH,Kometa"


def test_singbox_route_sends_allowed_services_direct():
    document = json.loads(build_singbox_json([_vless("DE")]))

    route = document["route"]
    assert route["final"] == "Kometa Auto"
    suffixes = route["rules"][0]["domain_suffix"]
    assert "gosuslugi.ru" in suffixes
    assert "sberbank.ru" in suffixes
    assert route["rules"][0]["outbound"] == "direct"
    assert "127.0.0.0/8" in route["rules"][1]["ip_cidr"]


def test_singbox_makes_group_per_channel_with_own_test_url():
    document = json.loads(
        build_singbox_json(
            [_vless("DE"), _vless("Вход · CDN")],
            test_urls={"cdn": "https://cdn.example/ping"},
        )
    )

    groups = {outbound["tag"]: outbound for outbound in document["outbounds"] if outbound["type"] == "urltest"}
    assert set(groups) == {"Kometa Auto", "Kometa CDN"}
    assert groups["Kometa CDN"]["url"] == "https://cdn.example/ping"
    assert groups["Kometa Auto"]["url"] != "https://cdn.example/ping"
