"""Тесты форматов подписки: Clash YAML, sing-box JSON и выбор формата по UA.

Проверяем три вещи:
  1) разбор ссылок из панели (включая реальный пример из заглушки ``app/panels/fake.py``);
  2) что сгенерированные YAML/JSON валидны и содержат группу авто-выбора;
  3) что формат отдаётся тому клиенту, который его понимает.
"""

from __future__ import annotations

import json

import pytest
import yaml

from app.web.subscription_format import (
    build_clash_yaml,
    build_singbox_json,
    detect_client_format,
    parse_config_link,
)

REALITY_LINK = (
    "vless://11111111-2222-3333-4444-555555555555@de1.example.com:443"
    "?type=tcp&security=reality&fp=chrome&pbk=PUBLICKEY123&sni=www.microsoft.com"
    "&sid=ab12&flow=xtls-rprx-vision#%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0%20%E2%9A%A1"
)
WS_LINK = (
    "vless://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee@cdn.example.com:8443"
    "?type=ws&security=tls&fp=firefox&sni=cdn.example.com&path=%2Fws%2Fkometa&host=cdn.example.com"
    "#WS-CDN"
)
XHTTP_LINK = (
    "vless://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee@x.example.com:2087"
    "?type=xhttp&security=reality&fp=chrome&pbk=KEYX&sid=9f&path=%2Fxhttp&host=x.example.com"
    "#XHTTP"
)
WIREGUARD_LINK = (
    "wireguard://cHJpdmF0ZWtleT09@wg.example.com:51820"
    "?publickey=cHVibGlja2V5&address=10.8.0.2%2F32,fd00::2%2F128&mtu=1420#WG-DE"
)


# --- 1. Разбор ссылок ----------------------------------------------------


def test_parse_vless_reality_all_fields():
    """Полная ссылка Reality разбирается во все ожидаемые поля."""
    config = parse_config_link(REALITY_LINK)

    assert config is not None
    assert config["kind"] == "vless"
    assert config["name"] == "Москва ⚡"  # фрагмент URL-декодирован
    assert config["server"] == "de1.example.com"
    assert config["port"] == 443
    assert config["uuid"] == "11111111-2222-3333-4444-555555555555"
    assert config["network"] == "tcp"
    assert config["security"] == "reality"
    assert config["sni"] == "www.microsoft.com"
    assert config["public_key"] == "PUBLICKEY123"
    assert config["short_id"] == "ab12"
    assert config["fingerprint"] == "chrome"
    assert config["flow"] == "xtls-rprx-vision"
    assert config["raw"] == REALITY_LINK
    # Для tcp нет транспортных полей.
    assert "path" not in config
    assert "host" not in config


def test_parse_vless_ws_path_and_host():
    """У WS-конфига есть path и host (нужны для ws-opts)."""
    config = parse_config_link(WS_LINK)

    assert config is not None
    assert config["network"] == "ws"
    assert config["security"] == "tls"
    assert config["path"] == "/ws/kometa"
    assert config["host"] == "cdn.example.com"
    assert config["fingerprint"] == "firefox"
    assert "flow" in config and config["flow"] == ""


def test_parse_vless_xhttp_transport():
    """XHTTP-конфиг тоже отдаёт path/host."""
    config = parse_config_link(XHTTP_LINK)

    assert config is not None
    assert config["network"] == "xhttp"
    assert config["path"] == "/xhttp"
    assert config["host"] == "x.example.com"


def test_parse_vless_without_fragment_uses_host_port():
    """Без фрагмента имя = host:port."""
    link = "vless://11111111-2222-3333-4444-555555555555@no-name.example.com:8443?type=tcp"
    config = parse_config_link(link)

    assert config is not None
    assert config["name"] == "no-name.example.com:8443"


def test_parse_vless_default_port_and_defaults():
    """Порт по умолчанию — 443, сеть — tcp, security — none."""
    link = "vless://11111111-2222-3333-4444-555555555555@bare.example.com"
    config = parse_config_link(link)

    assert config is not None
    assert config["port"] == 443
    assert config["network"] == "tcp"
    assert config["security"] == "none"
    assert config["sni"] == ""


def test_parse_vless_spx_only_when_present():
    """spx попадает в словарь только если он есть в ссылке."""
    with_spx = parse_config_link(
        "vless://11111111-2222-3333-4444-555555555555@a.example.com:443"
        "?type=tcp&security=reality&pbk=K&spx=%2Fspider#A"
    )
    without_spx = parse_config_link(
        "vless://11111111-2222-3333-4444-555555555555@a.example.com:443?type=tcp&security=reality&pbk=K#A"
    )

    assert with_spx is not None and with_spx["spx"] == "/spider"
    assert without_spx is not None and "spx" not in without_spx


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        "   ",
        "это просто текст",
        "http://example.com/sub",
        "trojan://pass@example.com:443#T",
        "vless://",
        "vless://@example.com:443?type=tcp",
        "vless://11111111-2222-3333-4444-555555555555@example.com:notaport",
        "://",
        None,
    ],
)
def test_parse_garbage_returns_none(garbage):
    """Мусор и неизвестные схемы не бросают исключений."""
    assert parse_config_link(garbage) is None


def test_parse_wireguard_kind_and_params():
    """WireGuard-ссылка: kind=wireguard, приватный ключ + параметры как есть."""
    config = parse_config_link(WIREGUARD_LINK)

    assert config is not None
    assert config["kind"] == "wireguard"
    assert config["name"] == "WG-DE"
    assert config["server"] == "wg.example.com"
    assert config["port"] == 51820
    assert config["raw"] == WIREGUARD_LINK
    assert config["private_key"] == "cHJpdmF0ZWtleT09"
    assert config["params"]["publickey"] == "cHVibGlja2V5"
    assert config["params"]["address"] == "10.8.0.2/32,fd00::2/128"
    assert config["mtu"] == "1420"


# --- 2. Clash YAML -------------------------------------------------------


def _clash(configs, **kwargs) -> dict:
    """Собрать Clash-подписку и разобрать её обратно в словарь."""
    return yaml.safe_load(build_clash_yaml(configs, **kwargs))


def test_clash_yaml_parses_and_has_url_test_group():
    """YAML валиден, группа url-test содержит url/interval/tolerance."""
    document = _clash([REALITY_LINK, WS_LINK])

    assert set(document) == {"proxies", "proxy-groups", "rules"}
    group = document["proxy-groups"][0]
    assert group["name"] == "Kometa"
    assert group["type"] == "url-test"
    assert group["url"] == "http://www.gstatic.com/generate_204"
    assert group["interval"] == 300
    assert group["tolerance"] == 50
    assert group["proxies"] == ["Москва ⚡", "WS-CDN"]


def test_clash_all_vless_in_proxies_and_wireguard_skipped():
    """Все VLESS попадают в proxies; WireGuard/AmneziaWG — нет."""
    document = _clash([REALITY_LINK, WS_LINK, WIREGUARD_LINK])

    proxies = document["proxies"]
    assert [proxy["name"] for proxy in proxies] == ["Москва ⚡", "WS-CDN"]
    assert {proxy["type"] for proxy in proxies} == {"vless"}
    assert document["proxy-groups"][0]["proxies"] == ["Москва ⚡", "WS-CDN"]


def test_clash_vless_fields_and_reality_opts():
    """Поля VLESS-прокси и блок reality-opts соответствуют формату Mihomo."""
    proxy = _clash([REALITY_LINK])["proxies"][0]

    assert proxy["type"] == "vless"
    assert proxy["server"] == "de1.example.com"
    assert proxy["port"] == 443
    assert proxy["uuid"] == "11111111-2222-3333-4444-555555555555"
    assert proxy["udp"] is True
    assert proxy["tls"] is True
    assert proxy["network"] == "tcp"
    assert proxy["flow"] == "xtls-rprx-vision"
    assert proxy["servername"] == "www.microsoft.com"
    assert proxy["client-fingerprint"] == "chrome"
    assert proxy["skip-cert-verify"] is False
    assert proxy["reality-opts"] == {"public-key": "PUBLICKEY123", "short-id": "ab12"}


def test_clash_fingerprint_defaults_to_chrome():
    """Без fp подставляем chrome — иначе Reality не поднимется."""
    link = "vless://11111111-2222-3333-4444-555555555555@a.example.com:443?security=reality&pbk=K#A"
    proxy = _clash([link])["proxies"][0]

    assert proxy["client-fingerprint"] == "chrome"
    assert "reality-opts" in proxy


def test_clash_ws_opts_with_host_header():
    """Для ws добавляется ws-opts с path и headers.Host."""
    proxy = _clash([WS_LINK])["proxies"][0]

    assert proxy["network"] == "ws"
    assert proxy["ws-opts"] == {"path": "/ws/kometa", "headers": {"Host": "cdn.example.com"}}
    assert "reality-opts" not in proxy  # security=tls, не reality


def test_clash_xhttp_opts():
    """Для xhttp добавляется xhttp-opts (поддержка Mihomo)."""
    proxy = _clash([XHTTP_LINK])["proxies"][0]

    assert proxy["xhttp-opts"] == {"path": "/xhttp", "headers": {"Host": "x.example.com"}}


def test_clash_rules_match():
    """Правило одно: весь трафик идёт в группу авто-выбора."""
    document = _clash([REALITY_LINK], title="Kometa Pro")

    assert document["rules"] == ["MATCH,Kometa Pro"]
    assert document["proxy-groups"][0]["name"] == "Kometa Pro"


def test_clash_custom_tuning():
    """test_url/interval/tolerance прокидываются в группу и в YAML — числа."""
    document = _clash(
        [REALITY_LINK],
        title="Комета",
        test_url="http://cp.cloudflare.com/generate_204",
        interval=120,
        tolerance=25,
    )
    group = document["proxy-groups"][0]

    assert group["url"] == "http://cp.cloudflare.com/generate_204"
    assert group["interval"] == 120
    assert group["tolerance"] == 25


def test_clash_unicode_title_stays_readable():
    """allow_unicode: кириллица в файле не превращается в \\uXXXX."""
    text = build_clash_yaml([REALITY_LINK], title="Комета")

    assert "Комета" in text
    assert "\\u" not in text


def test_clash_empty_configs_do_not_crash():
    """Пустая подписка — валидный YAML с пустыми proxies и DIRECT в группе."""
    document = _clash([])

    assert document["proxies"] == []
    assert document["proxy-groups"][0]["proxies"] == ["DIRECT"]
    assert document["rules"] == ["MATCH,Kometa"]


def test_clash_wireguard_only_gives_direct_group():
    """Если в подписке только WireGuard — остаётся группа с DIRECT."""
    document = _clash([WIREGUARD_LINK, "мусор"])

    assert document["proxies"] == []
    assert document["proxy-groups"][0]["proxies"] == ["DIRECT"]


def test_clash_duplicate_names_are_made_unique():
    """Одинаковые имена прокси ломают конфиг — дубли нумеруются."""
    link = "vless://11111111-2222-3333-4444-555555555555@a.example.com:443?security=reality&pbk=K#DE"
    document = _clash([link, link])
    names = [proxy["name"] for proxy in document["proxies"]]

    assert names == ["DE", "DE (2)"]
    assert len(set(names)) == 2
    assert document["proxy-groups"][0]["proxies"] == names


# --- 3. sing-box JSON ----------------------------------------------------


def _singbox(configs, **kwargs) -> dict:
    """Собрать sing-box-подписку и разобрать её обратно."""
    return json.loads(build_singbox_json(configs, **kwargs))


def _by_tag(document: dict, tag: str) -> dict:
    return next(outbound for outbound in document["outbounds"] if outbound["tag"] == tag)


def test_singbox_json_parses_and_has_urltest():
    """JSON валиден, urltest перечисляет все локации — это и есть автовыбор."""
    document = _singbox([REALITY_LINK, WS_LINK, WIREGUARD_LINK])

    auto = _by_tag(document, "Kometa Auto")
    assert auto["type"] == "urltest"
    assert auto["outbounds"] == ["Москва ⚡", "WS-CDN", "WG-DE"]
    assert auto["url"] == "http://www.gstatic.com/generate_204"
    assert auto["interval"] == "3m"
    assert auto["tolerance"] == 50


def test_singbox_vless_reality_tls_block():
    """У Reality-локации корректный tls.reality (public_key + short_id)."""
    outbound = _by_tag(_singbox([REALITY_LINK]), "Москва ⚡")

    assert outbound["type"] == "vless"
    assert outbound["server"] == "de1.example.com"
    assert outbound["server_port"] == 443
    assert outbound["uuid"] == "11111111-2222-3333-4444-555555555555"
    assert outbound["flow"] == "xtls-rprx-vision"
    assert outbound["packet_encoding"] == "xudp"
    assert outbound["tls"]["enabled"] is True
    assert outbound["tls"]["server_name"] == "www.microsoft.com"
    assert outbound["tls"]["utls"] == {"enabled": True, "fingerprint": "chrome"}
    assert outbound["tls"]["reality"] == {
        "enabled": True,
        "public_key": "PUBLICKEY123",
        "short_id": "ab12",
    }


def test_singbox_ws_transport():
    """Для ws-локации добавляется transport с path и Host."""
    outbound = _by_tag(_singbox([WS_LINK]), "WS-CDN")

    assert outbound["transport"] == {
        "type": "ws",
        "path": "/ws/kometa",
        "headers": {"Host": "cdn.example.com"},
    }
    assert "reality" not in outbound["tls"]  # security=tls


def test_singbox_wireguard_outbound():
    """WireGuard-ссылка становится wireguard-outbound с разобранными полями."""
    outbound = _by_tag(_singbox([WIREGUARD_LINK]), "WG-DE")

    assert outbound["type"] == "wireguard"
    assert outbound["server"] == "wg.example.com"
    assert outbound["server_port"] == 51820
    assert outbound["private_key"] == "cHJpdmF0ZWtleT09"
    assert outbound["peer_public_key"] == "cHVibGlja2V5"
    assert outbound["local_address"] == ["10.8.0.2/32", "fd00::2/128"]
    assert outbound["mtu"] == 1420


def test_singbox_has_direct_and_block():
    """Служебные outbound direct/block всегда на месте."""
    document = _singbox([REALITY_LINK])

    assert _by_tag(document, "direct") == {"type": "direct", "tag": "direct"}
    assert _by_tag(document, "block") == {"type": "block", "tag": "block"}


def test_singbox_empty_configs_do_not_crash():
    """Пустая подписка: только urltest с direct + служебные outbound."""
    document = _singbox([])

    assert [outbound["type"] for outbound in document["outbounds"]] == [
        "urltest",
        "direct",
        "block",
    ]
    assert document["outbounds"][0]["outbounds"] == ["direct"]


def test_singbox_flow_absent_means_no_xudp():
    """packet_encoding=xudp появляется только вместе с flow."""
    outbound = _by_tag(_singbox([WS_LINK]), "WS-CDN")

    assert "flow" not in outbound
    assert "packet_encoding" not in outbound


def test_singbox_custom_title_and_tuning():
    """Имя группы автовыбора и параметры замера настраиваются."""
    document = _singbox(
        [REALITY_LINK],
        title="Комета",
        test_url="http://cp.cloudflare.com/generate_204",
        interval="5m",
        tolerance=25,
    )
    auto = _by_tag(document, "Комета Auto")

    assert auto["url"] == "http://cp.cloudflare.com/generate_204"
    assert auto["interval"] == "5m"
    assert auto["tolerance"] == 25


def test_singbox_unicode_not_escaped():
    """ensure_ascii=False: имена локаций с кириллицей читаемы."""
    text = build_singbox_json([REALITY_LINK])

    assert "Москва ⚡" in text
    assert "\\u" not in text


# --- 4. Выбор формата ----------------------------------------------------


@pytest.mark.parametrize(
    ("user_agent", "expected"),
    [
        ("clash-verge/v1.5.11", "clash"),
        ("Clash.Meta/1.18.0", "clash"),
        ("mihomo/1.18.1", "clash"),
        ("Stash/2.5.0 (macOS)", "clash"),
        ("clashmeta", "clash"),
        ("sing-box 1.10.1", "singbox"),
        ("singbox/1.9", "singbox"),
        ("Hiddify/2.0.5 (Android)", "singbox"),
        ("SFA/1.3.0", "singbox"),
        ("SFI/1.3.0", "singbox"),
        ("Karing/1.0.0", "singbox"),
        ("v2rayNG/1.8.5", "base64"),
        ("Happ/1.0.0", "base64"),
        ("Streisand/1.6", "base64"),
        ("", "base64"),
        (None, "base64"),
    ],
)
def test_detect_client_format_by_user_agent(user_agent, expected):
    """UA определяет формат; неизвестные клиенты получают base64-список."""
    assert detect_client_format(user_agent) == expected


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("clash", "clash"),
        ("CLASH", "clash"),
        ("  Clash  ", "clash"),
        ("singbox", "singbox"),
        ("SingBox", "singbox"),
        ("base64", "base64"),
        ("BASE64", "base64"),
    ],
)
def test_detect_requested_has_priority(requested, expected):
    """Явный ?format= важнее User-Agent (в том числе при чужом UA)."""
    assert detect_client_format("Hiddify/2.0.5", requested) == expected
    assert detect_client_format(None, requested) == expected


@pytest.mark.parametrize("requested", ["yaml", "auto", "", "   ", "json"])
def test_detect_unknown_requested_falls_back_to_user_agent(requested):
    """Неизвестное значение ?format= не ломает автоопределение."""
    assert detect_client_format("clash-verge/v1.5.11", requested) == "clash"
    assert detect_client_format("sing-box 1.10.1", requested) == "singbox"
    assert detect_client_format(None, requested) == "base64"


# --- 5. Реальный пример из заглушки панели -------------------------------


def test_fake_panel_configs_parse_and_reach_both_formats():
    """Строки из app/panels/fake.py (_make_configs) годятся для обоих форматов."""
    from app.panels.base import PanelUser
    from app.panels.fake import FakePanel

    host = "node-de.example.com"
    user = PanelUser(uuid="99999999-8888-7777-6666-555555555555", email="user@example.com")
    configs = FakePanel(host=host)._make_configs(user)  # noqa: SLF001 - нужен реальный пример

    assert len(configs) == 2

    vless, amneziawg = (parse_config_link(config) for config in configs)
    assert vless is not None and amneziawg is not None
    assert vless["kind"] == "vless"
    assert vless["name"] == "user@example.com-DE"
    assert vless["server"] == host
    assert vless["port"] == 443
    assert vless["security"] == "reality"
    assert vless["public_key"] == "FAKEPUBLICKEY"
    assert vless["short_id"] == "ab12"
    assert vless["sni"] == "www.microsoft.com"
    assert vless["flow"] == "xtls-rprx-vision"
    assert amneziawg["kind"] == "wireguard"
    assert amneziawg["name"] == "user@example.com-DE-WG"
    assert amneziawg["port"] == 51820

    clash = yaml.safe_load(build_clash_yaml(configs))
    assert [proxy["name"] for proxy in clash["proxies"]] == ["user@example.com-DE"]
    assert clash["proxies"][0]["reality-opts"]["public-key"] == "FAKEPUBLICKEY"
    assert clash["proxy-groups"][0]["type"] == "url-test"

    singbox = json.loads(build_singbox_json(configs))
    auto = _by_tag(singbox, "Kometa Auto")
    assert auto["outbounds"] == ["user@example.com-DE", "user@example.com-DE-WG"]
    tags = {outbound["tag"]: outbound["type"] for outbound in singbox["outbounds"]}
    assert tags["user@example.com-DE"] == "vless"
    assert tags["user@example.com-DE-WG"] == "wireguard"
