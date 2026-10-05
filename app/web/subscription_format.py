"""Форматы подписки: Clash YAML и sing-box JSON с авто-выбором локации.

Зачем модуль:
  * сейчас ``/sub/<token>`` отдаёт только base64-список ``vless://`` — из него
    клиент не строит группу авто-выбора, и пользователь вручную перебирает
    локации, чтобы найти быструю;
  * Clash/Mihomo (``url-test``) и sing-box (``urltest``) умеют сами измерять
    задержку и переключаться на быстрейший узел — но только если подписка
    отдана в их формате.

Модуль чистый: только стандартная библиотека и pyyaml, без сети и без БД —
поэтому он легко тестируется и не тянет за собой настройки приложения.

Поддержанные схемы конфигов:
  * ``vless://`` — основной протокол наших нод (TCP/Reality, WS, XHTTP);
  * ``wireguard://`` / ``amneziawg://`` — поддерживает sing-box, но не Clash
    (см. пояснение в шапке Clash-файла).
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, unquote, urlparse

import yaml

#: Порт по умолчанию, если в ссылке он не указан.
DEFAULT_PORT = 443
#: Отпечаток TLS по умолчанию (uTLS): без него Reality не работает.
DEFAULT_FINGERPRINT = "chrome"
#: URL проверки задержки: лёгкий 204 от Google, не отдаёт контент.
DEFAULT_TEST_URL = "http://www.gstatic.com/generate_204"
#: Интервал проверки задержки в Clash — секунды.
DEFAULT_CLASH_INTERVAL = 300
#: Интервал проверки задержки в sing-box — строка формата "3m".
DEFAULT_SINGBOX_INTERVAL = "3m"
#: Разброс задержек (мс), в пределах которого узел не переключается.
DEFAULT_TOLERANCE = 50

#: Схемы, которые разбираются в VLESS-конфиг.
VLESS_SCHEMES = ("vless",)
#: Схемы WireGuard-семейства (sing-box понимает их как ``wireguard``).
WIREGUARD_SCHEMES = ("wireguard", "amneziawg")
SUPPORTED_SCHEMES = VLESS_SCHEMES + WIREGUARD_SCHEMES

#: Сети (``type=``), для которых в Clash нужны отдельные opts-блоки.
TRANSPORT_NETWORKS = ("ws", "xhttp")

#: Признаки Clash-клиентов в User-Agent (сравниваются в нижнем регистре).
CLASH_UA_MARKERS = ("clash", "mihomo", "stash", "clashmeta", "clash-verge")
#: Признаки sing-box-клиентов в User-Agent.
SINGBOX_UA_MARKERS = ("sing-box", "singbox", "hiddify", "sfa", "sfi", "karing", "sing")
#: Явные значения ``?format=`` — приоритетнее User-Agent.
FORMAT_BY_REQUEST = {"clash": "clash", "singbox": "singbox", "base64": "base64"}

#: Комментарий в начале Clash-файла: YAML-комментарии safe_dump не сохраняет,
#: поэтому шапку добавляем текстом.
CLASH_HEADER = (
    "# Kometa — подписка для Clash / Mihomo.\n"
    "# Локация выбирается автоматически: группа url-test замеряет задержку\n"
    "# и держит быстрейший узел (tolerance — чтобы не дёргаться на мелочах).\n"
    "# WireGuard и AmneziaWG здесь пропущены: ядро Clash/Mihomo не поддерживает\n"
    "# исходящие типа wireguard в подписке (таких прокси нет в списке типов\n"
    "# outbound). Эти локации доступны в подписке sing-box — ?format=singbox.\n"
)


def _first(params: dict[str, list[str]], key: str, default: str = "") -> str:
    """Первое значение query-параметра (пустая строка, если параметра нет)."""
    values = params.get(key)
    if not values:
        return default
    return (values[0] or "").strip()


def _as_int(value: str) -> int | None:
    """Превратить строку в int; None, если это не целое число."""
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return None


def _as_int_list(value: str) -> list[int] | None:
    """Разобрать список целых через запятую (например ``reserved=1,2,3``)."""
    parts = [part.strip() for part in value.split(",") if part.strip()]
    numbers = [_as_int(part) for part in parts]
    if not numbers or any(number is None for number in numbers):
        return None
    return [number for number in numbers if number is not None]


def parse_config_link(uri: str | None) -> dict | None:
    """Разобрать строку конфига в словарь.

    Поддерживаются схемы ``vless://``, ``wireguard://`` и ``amneziawg://``.
    Мусор, пустая строка и неизвестная схема дают ``None`` — исключений нет:
    подписка может содержать что угодно, падать из-за одной строки нельзя.

    Для VLESS возвращаются поля: ``kind``, ``name`` (фрагмент, URL-декодированный;
    по умолчанию ``host:port``), ``server``, ``port``, ``uuid``, ``flow``,
    ``network`` (из ``type``), ``security``, ``sni``, ``public_key`` (``pbk``),
    ``short_id`` (``sid``), ``fingerprint`` (``fp``), ``spx`` (если есть),
    ``path``/``host`` (для ws/xhttp, если есть), ``raw``.

    Для WireGuard-семейства: ``kind="wireguard"``, ``name``, ``server``, ``port``,
    ``raw``, ``private_key`` и все query-параметры как есть (в ``params`` и
    продублированные в верхнем уровне).

    :param uri: строка конфига из панели.
    :return: словарь с разобранными полями или ``None``.
    """
    if not uri or not isinstance(uri, str):
        return None
    raw = uri.strip()
    if not raw:
        return None

    try:
        parsed = urlparse(raw)
        server = (parsed.hostname or "").strip()
        port = parsed.port or DEFAULT_PORT
    except ValueError:
        # Битый порт или некорректный IPv6-литерал.
        return None

    scheme = (parsed.scheme or "").lower()
    if scheme not in SUPPORTED_SCHEMES or not server:
        return None

    params = parse_qs(parsed.query, keep_blank_values=True)
    name = unquote(parsed.fragment).strip() or f"{server}:{port}"

    if scheme in VLESS_SCHEMES:
        uuid = unquote(parsed.username or "").strip()
        if not uuid:
            # VLESS без UUID бесполезен — считаем строку мусором.
            return None

        config: dict = {
            "kind": "vless",
            "name": name,
            "server": server,
            "port": port,
            "uuid": uuid,
            "flow": _first(params, "flow"),
            "network": (_first(params, "type", "tcp") or "tcp").lower(),
            "security": (_first(params, "security", "none") or "none").lower(),
            "sni": _first(params, "sni"),
            "public_key": _first(params, "pbk"),
            "short_id": _first(params, "sid"),
            "fingerprint": _first(params, "fp"),
            "raw": raw,
        }
        spx = _first(params, "spx")
        if spx:
            config["spx"] = spx
        path = _first(params, "path")
        if path:
            config["path"] = path
        host = _first(params, "host")
        if host:
            config["host"] = host
        return config

    # WireGuard / AmneziaWG: sing-box умеет такой outbound, Clash — нет.
    # Логин в ссылке — приватный ключ клиента, остальное забираем из query.
    flat_params = {key: values[0] for key, values in params.items() if values}
    config = {
        "kind": "wireguard",
        "name": name,
        "server": server,
        "port": port,
        "raw": raw,
        "private_key": unquote(parsed.username or "").strip(),
        "params": flat_params,
    }
    for key, value in flat_params.items():
        config.setdefault(key, value)
    return config


def _unique_names(names: list[str]) -> list[str]:
    """Сделать имена уникальными.

    Clash и sing-box не терпят двух прокси с одинаковым именем: конфиг либо
    не загрузится, либо потеряет узел. Дубли аккуратно нумеруем.
    """
    seen: dict[str, int] = {}
    result: list[str] = []
    for name in names:
        count = seen.get(name, 0) + 1
        seen[name] = count
        result.append(name if count == 1 else f"{name} ({count})")
    return result


def _collect(configs: list[str]) -> list[dict]:
    """Разобрать все строки и выдать валидные конфиги с уникальными именами."""
    parsed = [config for config in (parse_config_link(item) for item in configs) if config is not None]
    names = _unique_names([config["name"] for config in parsed])
    for config, name in zip(parsed, names):
        config["name"] = name
    return parsed


def _clash_transport_opts(config: dict) -> dict | None:
    """Блок ``ws-opts``/``xhttp-opts`` для Clash (path и заголовок Host)."""
    network = config.get("network", "")
    if network not in TRANSPORT_NETWORKS:
        return None
    opts: dict = {}
    if config.get("path"):
        opts["path"] = config["path"]
    if config.get("host"):
        opts["headers"] = {"Host": config["host"]}
    if not opts:
        return None
    return {f"{network}-opts": opts}


def _clash_vless_proxy(config: dict) -> dict:
    """Собрать один VLESS-прокси для Clash/Mihomo."""
    proxy: dict = {
        "name": config["name"],
        "type": "vless",
        "server": config["server"],
        "port": config["port"],
        "uuid": config["uuid"],
        "udp": True,
        "tls": True,
        "network": config["network"],
    }
    if config["flow"]:
        # xtls-rprx-vision — единственный flow, который понимают наши ноды.
        proxy["flow"] = config["flow"]
    transport_opts = _clash_transport_opts(config)
    if transport_opts is not None:
        proxy.update(transport_opts)
    if config["sni"]:
        proxy["servername"] = config["sni"]
    proxy["client-fingerprint"] = config["fingerprint"] or DEFAULT_FINGERPRINT
    if config["security"] == "reality":
        reality = {"public-key": config["public_key"]}
        if config["short_id"]:
            reality["short-id"] = config["short_id"]
        proxy["reality-opts"] = reality
    proxy["skip-cert-verify"] = False
    return proxy


def build_clash_yaml(
    configs: list[str],
    *,
    title: str = "Kometa",
    test_url: str = DEFAULT_TEST_URL,
    interval: int = DEFAULT_CLASH_INTERVAL,
    tolerance: int = DEFAULT_TOLERANCE,
) -> str:
    """Собрать подписку в формате Clash / Mihomo (YAML).

    Все VLESS-локации попадают в ``proxies``, а группа ``url-test`` с именем
    ``title`` сама выбирает быстрейшую. WireGuard/AmneziaWG пропускаем — см.
    комментарий в шапке результата.

    :param configs: строки конфигов из панели (могут содержать мусор).
    :param title: имя группы авто-выбора (оно же используется в ``rules``).
    :param test_url: URL замера задержки.
    :param interval: период замера в секундах.
    :param tolerance: разброс задержек в мс, в пределах которого узел не меняется.
    :return: YAML-текст подписки.
    """
    proxies: list[dict] = []
    names: list[str] = []
    for config in _collect(configs):
        if config["kind"] != "vless":
            # WireGuard/AmneziaWG: ядро Clash/Mihomo не умеет wireguard-прокси,
            # такая запись сломала бы весь конфиг — просто пропускаем её.
            continue
        proxy = _clash_vless_proxy(config)
        proxies.append(proxy)
        names.append(proxy["name"])

    document = {
        "proxies": proxies,
        "proxy-groups": [
            {
                "name": title,
                "type": "url-test",
                "url": test_url,
                "interval": interval,
                "tolerance": tolerance,
                # Пустая группа ломает конфиг — тогда хотя бы DIRECT.
                "proxies": names or ["DIRECT"],
            }
        ],
        "rules": [f"MATCH,{title}"],
    }
    body = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    return CLASH_HEADER + body


def _singbox_vless_outbound(config: dict) -> dict:
    """Собрать один VLESS-outbound для sing-box."""
    outbound: dict = {
        "type": "vless",
        "tag": config["name"],
        "server": config["server"],
        "server_port": config["port"],
        "uuid": config["uuid"],
    }
    if config["flow"]:
        outbound["flow"] = config["flow"]
        # Vision требует XUDP, иначе часть соединений (QUIC) отваливается.
        outbound["packet_encoding"] = "xudp"

    tls: dict = {"enabled": True}
    if config["sni"]:
        tls["server_name"] = config["sni"]
    tls["utls"] = {
        "enabled": True,
        "fingerprint": config["fingerprint"] or DEFAULT_FINGERPRINT,
    }
    if config["security"] == "reality":
        reality = {"enabled": True, "public_key": config["public_key"]}
        if config["short_id"]:
            reality["short_id"] = config["short_id"]
        tls["reality"] = reality
    outbound["tls"] = tls

    if config["network"] in TRANSPORT_NETWORKS:
        transport: dict = {"type": config["network"]}
        if config.get("path"):
            transport["path"] = config["path"]
        if config.get("host"):
            transport["headers"] = {"Host": config["host"]}
        outbound["transport"] = transport
    return outbound


def _singbox_wireguard_outbound(config: dict) -> dict:
    """Собрать WireGuard/AmneziaWG-outbound для sing-box.

    Берём только те параметры, которые sing-box действительно понимает:
    неизвестные ключи (например ``obfs``) он отвергнет вместе со всем конфигом.
    Если разобрать нечего — остаются базовые поля.
    """
    params: dict[str, str] = config.get("params", {})

    def param(*keys: str) -> str:
        for key in keys:
            value = (params.get(key) or "").strip()
            if value:
                return value
        return ""

    outbound: dict = {
        "type": "wireguard",
        "tag": config["name"],
        "server": config["server"],
        "server_port": config["port"],
    }

    private_key = config.get("private_key") or param("private_key", "privatekey", "key", "secret")
    if private_key:
        outbound["private_key"] = private_key
    peer_public_key = param("publickey", "public_key", "peer_public_key", "pk")
    if peer_public_key:
        outbound["peer_public_key"] = peer_public_key
    pre_shared_key = param("presharedkey", "pre_shared_key", "psk")
    if pre_shared_key:
        outbound["pre_shared_key"] = pre_shared_key

    address = param("address", "local_address", "addresses", "ip")
    if address:
        outbound["local_address"] = [item.strip() for item in address.split(",") if item.strip()]
    reserved = param("reserved")
    if reserved:
        reserved_list = _as_int_list(reserved)
        if reserved_list:
            outbound["reserved"] = reserved_list
    mtu = _as_int(param("mtu"))
    if mtu:
        outbound["mtu"] = mtu

    # Параметры обфускации AmneziaWG — sing-box 1.12+ понимает их как есть.
    for key in ("jc", "jmin", "jmax", "s1", "s2", "h1", "h2", "h3", "h4"):
        value = _as_int(param(key))
        if value is not None:
            outbound[key] = value
    return outbound


def build_singbox_json(
    configs: list[str],
    *,
    title: str = "Kometa",
    test_url: str = DEFAULT_TEST_URL,
    interval: str = DEFAULT_SINGBOX_INTERVAL,
    tolerance: int = DEFAULT_TOLERANCE,
) -> str:
    """Собрать подписку в формате sing-box (JSON).

    Кроме самих локаций добавляется outbound ``urltest`` — это и есть
    авто-выбор быстрейшей локации по задержке, — а также служебные
    ``direct`` и ``block``.

    :param configs: строки конфигов из панели (могут содержать мусор).
    :param title: префикс имени группы авто-выбора (``"<title> Auto"``).
    :param test_url: URL замера задержки.
    :param interval: период замера строкой (``"3m"``).
    :param tolerance: разброс задержек в мс, в пределах которого узел не меняется.
    :return: JSON-текст подписки.
    """
    outbounds: list[dict] = []
    tags: list[str] = []
    for config in _collect(configs):
        if config["kind"] == "vless":
            outbound = _singbox_vless_outbound(config)
        elif config["kind"] == "wireguard":
            outbound = _singbox_wireguard_outbound(config)
        else:  # pragma: no cover - защита от будущих схем
            continue
        outbounds.append(outbound)
        tags.append(outbound["tag"])

    outbounds.append(
        {
            "type": "urltest",
            "tag": f"{title} Auto",
            "outbounds": tags or ["direct"],
            "url": test_url,
            "interval": interval,
            "tolerance": tolerance,
        }
    )
    outbounds.append({"type": "direct", "tag": "direct"})
    outbounds.append({"type": "block", "tag": "block"})
    return json.dumps({"outbounds": outbounds}, ensure_ascii=False, indent=2)


def detect_client_format(user_agent: str | None, requested: str | None = None) -> str:
    """Определить, в каком формате отдавать подписку.

    Приоритет — явный параметр ``?format=`` (``clash`` / ``singbox`` / ``base64``,
    регистр не важен). Неизвестное значение и отсутствие параметра означают
    автоопределение по User-Agent; по умолчанию отдаём base64-список, который
    понимают v2rayNG, Happ, Streisand и прочие клиенты.

    :param user_agent: заголовок ``User-Agent`` запроса (может быть ``None``).
    :param requested: значение query-параметра ``format``.
    :return: ``"clash"``, ``"singbox"`` или ``"base64"``.
    """
    if requested:
        explicit = requested.strip().lower()
        if explicit in FORMAT_BY_REQUEST:
            return FORMAT_BY_REQUEST[explicit]

    agent = (user_agent or "").strip().lower()
    if not agent:
        return "base64"
    if any(marker in agent for marker in CLASH_UA_MARKERS):
        return "clash"
    if any(marker in agent for marker in SINGBOX_UA_MARKERS):
        return "singbox"
    return "base64"
