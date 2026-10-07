"""Двойник панели 3x-ui 3.3.1 — «как в бою», а не «как удобно тесту».

Зачем отдельный двойник: остальные тесты используют ``FakePanel`` из
``app/panels/fake.py``, который всегда отвечает «успех». Из-за этого мимо тестов
проходят ровно те ошибки, которые ломают подключение у живых людей:

* старые маршруты клиентов (``/panel/api/inbounds/addClient`` и
  ``getClientTraffics``) в 3x-ui ≥ 3.1 удалены — панель отвечает 404, и клиент
  обязан переключиться на ``/panel/api/clients/*``;
* клиент с уже занятым ``email`` панель не создаёт: ``success: false``,
  ``msg: "Duplicate email"`` — а не молчаливое «ок»;
* ``settings``/``streamSettings`` приходят JSON-*строкой*, а ``totalGB`` —
  в байтах, ``expiryTime`` — в миллисекундах;
* сервис подписок панели (``:2096/sub/<subId>``) отдаёт base64, а адрес в
  ссылке берёт из ``shareAddr`` инбаунда (у нас — публичный IP).

Формы ответов сняты с боевой панели немецкого узла (Франкфурт,
150.241.106.75, 3x-ui 3.3.1, Xray 26.6.1) 06.10.2026; значения внутри —
синтетические.
"""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import unquote

import httpx

#: Порт, который панель по умолчанию слушает для сервиса подписок.
DEFAULT_SUB_PORT = 2096
#: Публичный адрес ноды — он же попадает в конфиг клиента (``shareAddr``).
DEFAULT_PUBLIC_HOST = "203.0.113.7"
DEFAULT_SUB_ID = "abcdefgh01234567"


def _json_env(payload: dict[str, Any], *, success: bool = True, msg: str = "") -> httpx.Response:
    """Ответ в конверте 3x-ui: ``{"success": bool, "msg": str, "obj": ...}``."""
    return httpx.Response(200, json={"success": success, "msg": msg, "obj": payload})


class FakeXuiServer:
    """Мини-панель 3x-ui в памяти: инбаунды, клиенты и сервис подписок.

    :param public_host: адрес, который панель подставляет в ссылку клиента
        (``shareAddr``). Именно он уходит пользователю, поэтому тесты проверяют
        его отдельно: если тут окажется ``127.0.0.1``, у клиента не будет
        подключения, хотя все ответы — 200 OK.
    :param legacy_routes: отдавать ли старые маршруты клиентов. По умолчанию
        ``False`` — как на нашей панели 3.3.1, где они уже удалены.
    """

    def __init__(
        self,
        *,
        base_url: str = "http://panel.test:9999",
        public_host: str = DEFAULT_PUBLIC_HOST,
        sub_port: int = DEFAULT_SUB_PORT,
        legacy_routes: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.public_host = public_host
        self.sub_base = f"http://panel.test:{sub_port}/sub/"
        self.legacy_routes = legacy_routes

        #: email → запись клиента (то, что лежит в ``settings.clients``).
        self.clients: dict[str, dict[str, Any]] = {}
        #: Журнал запросов: (метод, путь) — по нему видно, каким API шёл клиент.
        self.requests: list[tuple[str, str]] = []
        self.inbound_enabled = True

    # ------------------------------------------------------------------
    #  Управление состоянием (для подготовки сценария)
    # ------------------------------------------------------------------
    def add_client(
        self,
        *,
        email: str,
        uuid: str = "11111111-2222-3333-4444-555555555555",
        sub_id: str = DEFAULT_SUB_ID,
        days_left: int = 3,
        traffic_gb: int = 10,
        devices: int = 1,
        enable: bool = True,
        flow: str = "xtls-rprx-vision",
    ) -> dict[str, Any]:
        """Положить клиента в панель напрямую (например, «БД бота потеряли»)."""
        import time

        client = {
            "id": uuid,
            "email": email,
            "enable": enable,
            "expiryTime": int((time.time() + days_left * 86400) * 1000),
            "totalGB": int(traffic_gb) * 1024**3,
            "limitIp": devices,
            "flow": flow,
            "subId": sub_id,
            "tgId": 0,
            "reset": 0,
        }
        self.clients[email] = client
        return client

    def remove_client(self, email: str) -> None:
        self.clients.pop(email, None)

    # ------------------------------------------------------------------
    #  Транспорт
    # ------------------------------------------------------------------
    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _inbound(self) -> dict[str, Any]:
        """Инбаунд в форме 3x-ui 3.3.1 (``settings`` — JSON-строка)."""
        return {
            "id": 1,
            "up": 0,
            "down": 0,
            "total": 0,
            "remark": "DE-REALITY-firefox",
            "enable": self.inbound_enabled,
            "expiryTime": 0,
            "listen": "0.0.0.0",
            "port": 443,
            "protocol": "vless",
            "settings": json.dumps({"clients": list(self.clients.values()), "decryption": "none"}),
            "streamSettings": json.dumps(
                {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {
                        "serverNames": ["www.cloudflare.com"],
                        "shortIds": ["0259e235"],
                        "settings": {"publicKey": "PUBLIC-KEY", "fingerprint": "firefox"},
                    },
                }
            ),
            "tag": "inbound-443",
            "sniffing": json.dumps({"enabled": True}),
            "shareAddrStrategy": "custom",
            "shareAddr": self.public_host,
        }

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        self.requests.append((method, path))

        # --- сервис подписок панели: /sub/<subId> -------------------------
        if path.startswith("/sub/"):
            sub_id = path.rsplit("/", 1)[-1]
            client = next((c for c in self.clients.values() if c.get("subId") == sub_id), None)
            if client is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, text=self._subscription_body(client))

        # --- старые маршруты (3x-ui ≤ 3.0): на 3.3.1 их нет --------------
        if path.startswith("/panel/api/inbounds/addClient") and not self.legacy_routes:
            return httpx.Response(404, json={"success": False, "msg": "Not Found"})
        if path.startswith("/panel/api/inbounds/getClientTraffics/") and not self.legacy_routes:
            return httpx.Response(404, json={"success": False, "msg": "Not Found"})

        if path.endswith("/panel/api/inbounds/list"):
            return httpx.Response(200, json={"success": True, "msg": "", "obj": [self._inbound()]})

        if path.endswith("/panel/api/clients/add"):
            payload = json.loads(request.content or b"{}")
            client = payload.get("client") or {}
            email = str(client.get("email") or "")
            if not email:
                return _json_env(None, success=False, msg="email is required")
            if email in self.clients:
                # Именно так панель отвечает на попытку создать дубль.
                return _json_env(None, success=False, msg=f"Duplicate email: {email}")
            self.clients[email] = dict(client)
            return _json_env(None)

        if "/panel/api/clients/update/" in path:
            email = unquote(path.rsplit("/", 1)[-1])
            if email not in self.clients:
                return _json_env(None, success=False, msg="client not found")
            payload = json.loads(request.content or b"{}")
            self.clients[email].update(payload)
            return _json_env(None)

        if "/panel/api/clients/del/" in path:
            email = unquote(path.rsplit("/", 1)[-1])
            self.clients.pop(email, None)
            return _json_env(None)

        if "/panel/api/clients/traffic/" in path:
            email = unquote(path.rsplit("/", 1)[-1])
            client = self.clients.get(email)
            if client is None:
                return _json_env(None, success=False, msg="client not found")
            return _json_env({"email": email, "up": 1024, "down": 2048, "total": client["totalGB"]})

        if "/panel/api/inbounds/getClientTraffics/" in path and self.legacy_routes:
            email = unquote(path.rsplit("/", 1)[-1])
            if email not in self.clients:
                return _json_env(None, success=False, msg="not found")
            return _json_env({"email": email, "up": 1024, "down": 2048})

        return httpx.Response(404, json={"success": False, "msg": f"unexpected {method} {path}"})

    def _subscription_body(self, client: dict[str, Any]) -> str:
        """Тело подписки панели: base64-строка с ``vless://`` (как в бою)."""
        link = (
            f"vless://{client['id']}@{self.public_host}:443"
            "?flow=xtls-rprx-vision&fp=firefox&pbk=PUBLIC-KEY&security=reality"
            f"&sid=0259e235&sni=www.cloudflare.com&spx=%2F&type=tcp#{client['email']}"
        )
        return base64.b64encode(link.encode()).decode()
