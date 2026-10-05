"""Тесты клиента панели 3x-ui (:class:`app.panels.xui.XuiPanel`).

Сеть не используется вообще: все запросы уходят в ``httpx.MockTransport``, а
обработчик играет роль мини-панели 3x-ui — принимает ровно те маршруты и
форматы, что описаны в ТЗ и подтверждены исходниками панели
(см. docstring ``app/panels/xui.py``).
"""

from __future__ import annotations

import base64
import inspect
import json
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from app.panels.base import PanelError, UserSpec
from app.panels.xui import GIB, XuiPanel

BASE = "http://panel.test:54321"
SUB_BASE = "http://panel.test:2096/sub/"
TOKEN = "secret-api-token"
CSRF_TOKEN = "csrf-token-value"

DAY_MS = 86_400_000


def _form(request: httpx.Request) -> dict[str, str]:
    """Разобрать form-urlencoded тело запроса."""
    return dict(urllib.parse.parse_qsl(request.content.decode("utf-8")))


def _as_dict(value: Any) -> dict[str, Any]:
    """Привести settings/streamSettings к словарю (панель отдаёт и строку, и объект)."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        return json.loads(value)
    return {}


def _vless_inbound(inbound_id: int = 1) -> dict[str, Any]:
    """Инбаунд VLESS + TCP + Reality (как их отдаёт и старая, и новая панель)."""
    return {
        "id": inbound_id,
        "remark": "DE-Reality",
        "protocol": "vless",
        "port": 443,
        # ≤ v3.0 — JSON-строка, v3.9 — вложенный объект. Проверяем оба вида.
        "settings": json.dumps({"clients": [], "decryption": "none"}),
        "streamSettings": json.dumps({"network": "tcp", "security": "reality"}),
        "clientStats": [],
    }


def _wireguard_inbound(inbound_id: int = 2) -> dict[str, Any]:
    """Инбаунд WireGuard: flow для него не нужен."""
    return {
        "id": inbound_id,
        "remark": "DE-AmneziaWG",
        "protocol": "wireguard",
        "port": 51820,
        "settings": {"clients": [], "secretKey": "fake"},  # вложенный объект (v3.9)
        "streamSettings": {"network": "udp", "security": ""},
        "clientStats": [],
    }


class FakeXui:
    """Мини-панель 3x-ui в памяти для тестов."""

    def __init__(
        self,
        *,
        inbounds: list[dict[str, Any]] | None = None,
        token: str = "",
        username: str = "admin",
        password: str = "hunter2",
        legacy: bool = True,
        csrf: bool = False,
        subscription: str | None = None,
        subscription_status: int = 200,
        fail_list: str = "",
        require_auth: bool = False,
    ) -> None:
        raw_inbounds = inbounds if inbounds is not None else [_vless_inbound(), _wireguard_inbound()]
        # Внутри держим settings/streamSettings словарями (чтобы можно было
        # менять список клиентов), а в ответе отдаём в той форме, в какой их
        # прислали: строкой (3x-ui <= v3.0) или объектом (v3.9).
        self.inbounds: list[dict[str, Any]] = []
        self._string_settings: dict[int, bool] = {}
        for item in raw_inbounds:
            inbound = dict(item)
            self._string_settings[inbound["id"]] = isinstance(inbound.get("settings"), str)
            inbound["settings"] = _as_dict(inbound.get("settings"))
            inbound["streamSettings"] = _as_dict(inbound.get("streamSettings"))
            self.inbounds.append(inbound)
        self.token = token
        self.username = username
        self.password = password
        self.legacy = legacy
        self.csrf = csrf
        self.subscription = subscription
        self.subscription_status = subscription_status
        self.fail_list = fail_list
        self.require_auth = require_auth

        self.requests: list[httpx.Request] = []
        self.login_count = 0
        self.session_valid = True
        self.session_cookie = "session-abc"
        self.unhandled: list[str] = []
        self.traffic: dict[str, dict[str, int]] = {}

    # --- инфраструктура -------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.setdefault("timeout", 5.0)
        return httpx.AsyncClient(transport=self.transport(), **kwargs)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def wire_inbounds(self) -> list[dict[str, Any]]:
        """Инбаунды в том виде, в каком их отдаёт ``/panel/api/inbounds/list``."""
        result = []
        for inbound in self.inbounds:
            item = dict(inbound)
            if self._string_settings.get(inbound["id"], True):
                item["settings"] = json.dumps(inbound["settings"])
                item["streamSettings"] = json.dumps(inbound["streamSettings"])
            result.append(item)
        return result

    def clients_of(self, inbound_id: int) -> list[dict[str, Any]]:
        for inbound in self.inbounds:
            if inbound["id"] == inbound_id:
                return inbound["settings"]["clients"]
        raise AssertionError(f"нет инбаунда {inbound_id}")

    def find_client(self, uuid: str) -> dict[str, Any] | None:
        for inbound in self.inbounds:
            for client in self.clients_of(inbound["id"]):
                if client.get("id") == uuid:
                    return client
        return None

    def _authed(self, request: httpx.Request) -> bool:
        if not self.require_auth:
            return True
        auth = request.headers.get("Authorization", "")
        if self.token and auth == f"Bearer {self.token}":
            return True
        if self.token and auth.startswith("Bearer "):
            return False
        return self.session_valid and self.session_cookie in request.headers.get("Cookie", "")

    @staticmethod
    def _ok(obj: Any = None, msg: str = "") -> httpx.Response:
        return httpx.Response(200, json={"success": True, "msg": msg, "obj": obj})

    @staticmethod
    def _fail(msg: str = "something went wrong", status: int = 200) -> httpx.Response:
        return httpx.Response(status, json={"success": False, "msg": msg, "obj": None})

    # --- маршрутизация ---------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        method = request.method

        if method == "GET" and path == "/csrf-token":
            if not self.csrf:
                return httpx.Response(404, text="not found")
            return httpx.Response(
                200,
                json={"success": True, "obj": CSRF_TOKEN},
                headers={"Set-Cookie": f"{self.session_cookie}=1; Path=/"},
            )

        if method == "POST" and path == "/login":
            return self._login(request)

        if path.startswith("/panel/api/"):
            if not self._authed(request):
                # 3x-ui на неавторизованный запрос без Bearer отдаёт 404.
                return httpx.Response(404, json={"success": False, "msg": "not found"})
            return self._api(request)

        if path.startswith("/sub/"):
            return self._subscription(request)

        self.unhandled.append(f"{method} {path}")
        return httpx.Response(404, text="unhandled")

    def _login(self, request: httpx.Request) -> httpx.Response:
        self.login_count += 1
        form = _form(request)
        if self.csrf and form.get("_csrf") != CSRF_TOKEN:
            return httpx.Response(403, text="csrf")
        if form.get("username") != self.username or form.get("password") != self.password:
            return self._fail("Wrong username or password")
        self.session_valid = True
        return httpx.Response(
            200,
            json={"success": True, "msg": "Logged in successfully", "obj": None},
            headers={"Set-Cookie": f"{self.session_cookie}=1; Path=/"},
        )

    def _api(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if method == "GET" and path == "/panel/api/inbounds/list":
            if self.fail_list:
                return self._fail(self.fail_list)
            return self._ok(self.wire_inbounds())

        # --- старое API (3x-ui <= v3.0.x) ------------------------------
        if self.legacy:
            if method == "POST" and path == "/panel/api/inbounds/addClient":
                return self._legacy_add(_form(request))
            if method == "POST" and path.startswith("/panel/api/inbounds/updateClient/"):
                uuid = urllib.parse.unquote(path.rsplit("/", 1)[-1])
                return self._legacy_update(uuid, _form(request))
            if method == "POST" and "/delClient/" in path:
                parts = path.split("/")
                inbound_id = int(parts[4])
                uuid = urllib.parse.unquote(parts[-1])
                return self._legacy_delete(inbound_id, uuid)
            if method == "GET" and path.startswith("/panel/api/inbounds/getClientTraffics/"):
                email = urllib.parse.unquote(path.rsplit("/", 1)[-1])
                return self._traffic(email)

        # --- API v3 (3x-ui >= v3.1.0) -----------------------------------
        if method == "POST" and path == "/panel/api/clients/add":
            return self._v3_add(json.loads(request.content.decode()))
        if method == "POST" and path.startswith("/panel/api/clients/update/"):
            email = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            return self._v3_update(email, json.loads(request.content.decode()))
        if method == "POST" and path.startswith("/panel/api/clients/del/"):
            email = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            return self._v3_delete(email)
        if method == "GET" and path.startswith("/panel/api/clients/traffic/"):
            email = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            return self._traffic(email)

        self.unhandled.append(f"{method} {path}")
        return httpx.Response(404, json={"success": False, "msg": "route not found"})

    # --- операции --------------------------------------------------------
    def _legacy_add(self, form: dict[str, str]) -> httpx.Response:
        inbound_id = int(form["id"])
        client = json.loads(form["settings"])["clients"][0]
        clients = self.clients_of(inbound_id)
        # Один и тот же клиент (uuid/subId) в разных инбаундах — это норма,
        # повторная вставка того же email в тот же инбаунд — ошибка.
        if any(item.get("email") == client["email"] for item in clients):
            return self._fail(f"email already exists: {client['email']}")
        clients.append(dict(client))
        return self._ok(None, "Inbound client added successfully")

    def _legacy_update(self, uuid: str, form: dict[str, str]) -> httpx.Response:
        inbound_id = int(form["id"])
        client = json.loads(form["settings"])["clients"][0]
        clients = self.clients_of(inbound_id)
        for index, existing in enumerate(clients):
            if existing.get("id") == uuid:
                clients[index] = client
                return self._ok(None, "Inbound client updated successfully")
        return self._fail("client not found")

    def _legacy_delete(self, inbound_id: int, uuid: str) -> httpx.Response:
        clients = self.clients_of(inbound_id)
        for index, existing in enumerate(clients):
            if existing.get("id") == uuid:
                clients.pop(index)
                return self._ok(None, "Inbound client deleted successfully")
        return self._fail("client not found")

    def _v3_add(self, payload: dict[str, Any]) -> httpx.Response:
        client = payload["client"]
        for inbound_id in payload["inboundIds"]:
            self.clients_of(inbound_id).append(dict(client))
        return self._ok(None, "Inbound client added successfully")

    def _v3_update(self, email: str, client: dict[str, Any]) -> httpx.Response:
        for inbound in self.inbounds:
            for index, existing in enumerate(self.clients_of(inbound["id"])):
                if existing.get("email") == email:
                    self.clients_of(inbound["id"])[index] = dict(client)
        return self._ok(None, "Inbound client updated successfully")

    def _v3_delete(self, email: str) -> httpx.Response:
        for inbound in self.inbounds:
            clients = self.clients_of(inbound["id"])
            clients[:] = [item for item in clients if item.get("email") != email]
        return self._ok(None, "Inbound client deleted successfully")

    def _traffic(self, email: str) -> httpx.Response:
        counters = self.traffic.get(email)
        if counters is None:
            return self._fail("no traffic record")
        return self._ok(
            {
                "email": email,
                "up": counters["up"],
                "down": counters["down"],
                "total": 0,
                "expiryTime": 0,
                "enable": True,
            }
        )

    def _subscription(self, request: httpx.Request) -> httpx.Response:
        if self.subscription_status != 200:
            return httpx.Response(self.subscription_status, text="nope")
        return httpx.Response(200, text=self.subscription or "")


def make_panel(fake: FakeXui, **kwargs: Any) -> tuple[XuiPanel, httpx.AsyncClient]:
    """Собрать панель на общем httpx-клиенте с MockTransport (без сети).

    По умолчанию панель авторизуется API-токеном: тестам, которые проверяют не
    авторизацию, логин по паролю только мешал бы. Тесты авторизации передают
    свои ``token``/``username``/``password``.
    """
    client = fake.client()
    kwargs.setdefault("sub_base", SUB_BASE)
    if not kwargs.get("username") and not kwargs.get("token"):
        kwargs["token"] = TOKEN
    panel = XuiPanel(BASE, client=client, **kwargs)
    return panel, client


# ---------------------------------------------------------------------------
#  Авторизация
# ---------------------------------------------------------------------------
async def test_login_by_password_sends_form_and_keeps_cookie():
    """Логин по паролю: POST /login формой, cookie едет в следующих запросах."""
    fake = FakeXui(token="", csrf=False, require_auth=True)
    panel, client = make_panel(fake, username="admin", password="hunter2")
    async with client:
        assert await panel.health() is True

    login_requests = [r for r in fake.requests if r.url.path == "/login"]
    assert len(login_requests) == 1
    form = _form(login_requests[0])
    assert form["username"] == "admin"
    assert form["password"] == "hunter2"

    list_requests = [r for r in fake.requests if r.url.path == "/panel/api/inbounds/list"]
    assert list_requests, "список инбаундов так и не был запрошен"
    assert fake.session_cookie in list_requests[0].headers.get("Cookie", "")
    assert fake.login_count == 1, "повторный логин при живой сессии не нужен"


async def test_login_uses_csrf_token_when_panel_requires_it():
    """3x-ui v3.x закрывает /login CSRF-мидлварью: токен берём из /csrf-token."""
    fake = FakeXui(token="", csrf=True, require_auth=True)
    panel, client = make_panel(fake, username="admin", password="hunter2")
    async with client:
        assert await panel.health() is True

    assert "/csrf-token" in fake.paths()
    login_request = next(r for r in fake.requests if r.url.path == "/login")
    assert login_request.headers["X-CSRF-Token"] == CSRF_TOKEN
    assert _form(login_request)["_csrf"] == CSRF_TOKEN


async def test_token_mode_uses_bearer_and_never_logs_in():
    """С API-токеном логин не выполняется, а заголовок Bearer уходит всегда."""
    fake = FakeXui(token=TOKEN, require_auth=True)
    panel, client = make_panel(fake, token=TOKEN, username="admin", password="hunter2")
    async with client:
        assert await panel.health() is True

    assert fake.login_count == 0
    assert all(r.headers.get("Authorization") == f"Bearer {TOKEN}" for r in fake.requests)


async def test_expired_session_causes_relogin_and_retry():
    """Протухшая cookie: панель отвечает 404, клиент логинится заново и повторяет."""
    fake = FakeXui(token="", csrf=False, require_auth=True)
    panel, client = make_panel(fake, username="admin", password="hunter2")
    async with client:
        assert await panel.health() is True
        fake.session_valid = False  # сессия истекла
        inbounds = await panel.list_inbounds()

    assert [item.id for item in inbounds] == [1, 2]
    assert fake.login_count == 2


# ---------------------------------------------------------------------------
#  Инбаунды
# ---------------------------------------------------------------------------
async def test_list_inbounds_parses_settings_as_string_and_object():
    """``settings``/``streamSettings`` понимаются и строкой, и вложенным объектом."""
    fake = FakeXui()
    panel, client = make_panel(fake)
    async with client:
        inbounds = await panel.list_inbounds()

    assert [(i.id, i.protocol, i.port, i.network, i.security) for i in inbounds] == [
        (1, "vless", 443, "tcp", "reality"),
        (2, "wireguard", 51820, "udp", ""),
    ]
    assert inbounds[0].remark == "DE-Reality"


async def test_list_inbounds_filters_by_inbound_ids():
    """При заданных inbound_ids подписка собирается только из них."""
    fake = FakeXui()
    panel, client = make_panel(fake, inbound_ids=[2])
    async with client:
        inbounds = await panel.list_inbounds()

    assert [item.id for item in inbounds] == [2]


async def test_list_inbounds_unknown_id_raises_panel_error():
    """Несуществующий inbound_id — понятная ошибка, а не тихий пропуск."""
    fake = FakeXui()
    panel, client = make_panel(fake, inbound_ids=[1, 99])
    async with client:
        with pytest.raises(PanelError, match="99"):
            await panel.list_inbounds()


# ---------------------------------------------------------------------------
#  Создание пользователя
# ---------------------------------------------------------------------------
async def test_create_user_request_body_has_limits_and_expiry_in_ms():
    """Тело addClient: totalGB в байтах, expiryTime в ms, limitIp, subId, tgId, reset."""
    fake = FakeXui()
    panel, client = make_panel(fake, inbound_ids=[1, 2])
    spec = UserSpec(email="user_777", days=30, traffic_gb=50, devices=4, note="тариф «Месяц»")

    before = int(time.time() * 1000)
    async with client:
        created = await panel.create_user(spec)
    after = int(time.time() * 1000)

    add_requests = [r for r in fake.requests if r.url.path == "/panel/api/inbounds/addClient"]
    assert len(add_requests) == 2, "клиент должен появиться в обоих инбаундах"

    for request, inbound_id in zip(add_requests, (1, 2)):
        form = _form(request)
        assert form["id"] == str(inbound_id)
        assert isinstance(form["settings"], str), "settings уходит JSON-строкой"
        sent = json.loads(form["settings"])["clients"][0]

        assert sent["email"] == "user_777"
        assert sent["totalGB"] == 50 * GIB  # 50 ГиБ в байтах, как ждёт панель
        assert sent["limitIp"] == 4
        assert sent["enable"] is True
        assert sent["tgId"] == 0
        assert sent["reset"] == 0
        assert sent["comment"] == "тариф «Месяц»"
        assert len(sent["subId"]) >= 16
        assert sent["id"] == created.uuid

        expiry = sent["expiryTime"]
        expected = before + 30 * DAY_MS
        assert abs(expiry - expected) < 10_000, "expiryTime должен быть в миллисекундах"
        assert expiry < after + 30 * DAY_MS + 10_000

    # flow только у VLESS + TCP + Reality
    vless_client = json.loads(_form(add_requests[0])["settings"])["clients"][0]
    wg_client = json.loads(_form(add_requests[1])["settings"])["clients"][0]
    assert vless_client["flow"] == "xtls-rprx-vision"
    assert wg_client["flow"] == ""

    # один uuid и один subId на все инбаунды — одна подписка на все протоколы
    assert vless_client["id"] == wg_client["id"] == created.uuid
    assert vless_client["subId"] == wg_client["subId"]

    assert created.expires_at is not None
    delta = created.expires_at - datetime.now(timezone.utc)
    assert timedelta(days=29, hours=23) < delta < timedelta(days=30, minutes=1)
    assert created.traffic_limit_bytes == 50 * GIB
    assert created.devices_limit == 4
    assert created.subscription_url == f"{SUB_BASE}{vless_client['subId']}"
    assert created.enabled is True


async def test_create_user_flow_disabled_and_unlimited():
    """Бессрочный безлимит: expiryTime=0, totalGB=0; disableFlow снимает flow."""
    inbound = _vless_inbound()
    inbound["disableFlow"] = True
    fake = FakeXui(inbounds=[inbound])
    panel, client = make_panel(fake, inbound_ids=[1])

    async with client:
        created = await panel.create_user(UserSpec(email="trial_1", days=0, traffic_gb=0, devices=1))

    add_request = next(r for r in fake.requests if r.url.path.endswith("/addClient"))
    sent = json.loads(_form(add_request)["settings"])["clients"][0]
    assert sent["expiryTime"] == 0
    assert sent["totalGB"] == 0
    assert sent["flow"] == ""
    assert created.expires_at is None
    assert created.traffic_limit_bytes == 0


async def test_create_user_panel_rejects_email_with_panel_error():
    """``success=false`` от панели превращается в PanelError с текстом панели."""
    fake = FakeXui()
    panel, client = make_panel(fake, inbound_ids=[1])
    async with client:
        await panel.create_user(UserSpec(email="dup@example.com", days=1))
        with pytest.raises(PanelError, match="already exists"):
            await panel.create_user(UserSpec(email="dup@example.com", days=1))


async def test_create_user_falls_back_to_v3_clients_api():
    """Панель v3.9: старых маршрутов нет (404) — уходим на /panel/api/clients/add."""
    fake = FakeXui(legacy=False)
    panel, client = make_panel(fake, inbound_ids=[1, 2])
    async with client:
        created = await panel.create_user(UserSpec(email="v3_user", days=10, traffic_gb=5, devices=2))

        add_requests = [r for r in fake.requests if r.url.path == "/panel/api/clients/add"]
        assert len(add_requests) == 1, "в API v3 клиент добавляется одним вызовом"
        payload = json.loads(add_requests[0].content.decode())
        assert payload["inboundIds"] == [1, 2]
        assert payload["client"]["email"] == "v3_user"
        assert payload["client"]["totalGB"] == 5 * GIB
        assert payload["client"]["limitIp"] == 2
        assert payload["client"]["id"] == created.uuid
        assert payload["client"]["flow"] == "xtls-rprx-vision"

        # повторное обращение к старым маршрутам больше не происходит
        legacy_calls = [r for r in fake.requests if r.url.path.endswith("/addClient")]
        assert len(legacy_calls) == 1
        await panel.create_user(UserSpec(email="v3_user_2", days=10))
        assert len([r for r in fake.requests if r.url.path.endswith("/addClient")]) == 1

        # read-путь тоже переключился на API v3
        user = await panel.get_user(created.uuid)
        assert user is not None and user.email == "v3_user"


async def test_v3_api_used_for_update_and_delete_when_legacy_missing():
    """Если старых маршрутов нет, через API v3 идут и обновление, и удаление."""
    fake = FakeXui(legacy=False)
    panel, client = make_panel(fake, inbound_ids=[1, 2])
    async with client:
        created = await panel.create_user(UserSpec(email="v3_full", days=3, traffic_gb=1, devices=1))
        updated = await panel.update_user(created.uuid, extend_days=30, enable=False, devices=9)
        assert updated.enabled is False
        assert updated.devices_limit == 9

        await panel.delete_user(created.uuid)
        assert await panel.get_user(created.uuid) is None

    assert "/panel/api/clients/update/v3_full" in fake.paths()
    assert "/panel/api/clients/del/v3_full" in fake.paths()
    dead_routes = [p for p in fake.paths() if "updateClient" in p or "delClient" in p]
    assert not dead_routes, "старые маршруты больше не должны дёргаться"


# ---------------------------------------------------------------------------
#  Чтение и изменение пользователя
# ---------------------------------------------------------------------------
async def test_get_user_returns_traffic_limits_and_subscription():
    """get_user: срок из ms, лимиты, used_bytes = up + down, ссылка на подписку."""
    fake = FakeXui()
    panel, client = make_panel(fake, inbound_ids=[1, 2])
    async with client:
        created = await panel.create_user(UserSpec(email="user_1", days=7, traffic_gb=3, devices=2))
        fake.traffic["user_1"] = {"up": 1000, "down": 2048}
        user = await panel.get_user(created.uuid)

    assert user is not None
    assert user.uuid == created.uuid
    assert user.email == "user_1"
    assert user.enabled is True
    assert user.used_bytes == 3048
    assert user.traffic_limit_bytes == 3 * GIB
    assert user.devices_limit == 2
    assert user.subscription_url.startswith(SUB_BASE)
    delta = user.expires_at - datetime.now(timezone.utc)
    assert timedelta(days=6, hours=23) < delta < timedelta(days=7, minutes=1)


async def test_get_user_returns_none_for_unknown_uuid():
    """Неизвестный uuid — это ``None``, а не исключение."""
    fake = FakeXui()
    panel, client = make_panel(fake)
    async with client:
        assert await panel.get_user("00000000-0000-0000-0000-000000000000") is None


async def test_find_user_by_email_returns_state_or_none():
    """Поиск по email (нужен боту для восстановления после сбоя БД)."""
    fake = FakeXui()
    panel, client = make_panel(fake, inbound_ids=[1, 2])
    async with client:
        created = await panel.create_user(UserSpec(email="restore_me", days=4, devices=2))
        found = await panel.find_user_by_email("restore_me")
        missing = await panel.find_user_by_email("nobody@example.com")

    assert found is not None
    assert found.uuid == created.uuid
    assert found.email == "restore_me"
    assert found.subscription_url == created.subscription_url
    assert missing is None


async def test_update_user_extends_active_subscription_from_current_expiry():
    """Продление активной подписки идёт от текущей даты окончания."""
    fake = FakeXui()
    panel, client = make_panel(fake, inbound_ids=[1, 2])
    async with client:
        created = await panel.create_user(UserSpec(email="user_2", days=10))
        before = await panel.get_user(created.uuid)
        updated = await panel.update_user(created.uuid, extend_days=30)

    expected = before.expires_at + timedelta(days=30)
    assert abs((updated.expires_at - expected).total_seconds()) < 5

    forms = [
        _form(r)
        for r in fake.requests
        if r.url.path.startswith("/panel/api/inbounds/updateClient/")
    ]
    assert len(forms) == 2, "обновляем клиента во всех инбаундах"
    for form in forms:
        sent = json.loads(form["settings"])["clients"][0]
        assert abs(sent["expiryTime"] - int(expected.timestamp() * 1000)) < 5_000


async def test_update_user_extends_expired_subscription_from_now():
    """Если срок истёк — продление считается от «сейчас»."""
    fake = FakeXui()
    panel, client = make_panel(fake, inbound_ids=[1])
    async with client:
        created = await panel.create_user(UserSpec(email="user_3", days=1))
        # искусственно «старим» подписку на 30 дней назад
        past_ms = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000)
        for inbound in fake.inbounds:
            for record in fake.clients_of(inbound["id"]):
                record["expiryTime"] = past_ms
        updated = await panel.update_user(created.uuid, extend_days=7)

    delta = updated.expires_at - datetime.now(timezone.utc)
    assert timedelta(days=6, hours=23) < delta < timedelta(days=7, minutes=1)


async def test_update_user_can_enable_disable_and_change_limits():
    """enable/disable и смена лимитов уходят в панель и возвращаются в PanelUser."""
    fake = FakeXui()
    panel, client = make_panel(fake, inbound_ids=[1, 2])
    async with client:
        created = await panel.create_user(UserSpec(email="user_4", days=5, traffic_gb=1, devices=1))
        disabled = await panel.update_user(created.uuid, enable=False, traffic_gb=20, devices=5)

    assert disabled.enabled is False
    assert disabled.traffic_limit_bytes == 20 * GIB
    assert disabled.devices_limit == 5
    assert all(
        record["enable"] is False and record["limitIp"] == 5
        for record in fake.clients_of(1)
    ), "новые значения должны быть записаны в панель"


async def test_update_user_unknown_uuid_raises():
    """Обновление неизвестного пользователя — PanelError."""
    fake = FakeXui()
    panel, client = make_panel(fake)
    async with client:
        with pytest.raises(PanelError, match="не найден"):
            await panel.update_user("nope", extend_days=1)


async def test_delete_user_removes_client_from_every_inbound():
    """Удаление чистит клиента во всех инбаундах и идемпотентно."""
    fake = FakeXui()
    panel, client = make_panel(fake, inbound_ids=[1, 2])
    async with client:
        created = await panel.create_user(UserSpec(email="user_5", days=3))
        await panel.delete_user(created.uuid)
        assert await panel.get_user(created.uuid) is None
        await panel.delete_user(created.uuid)  # повторное удаление не падает


# ---------------------------------------------------------------------------
#  Конфиги подписки
# ---------------------------------------------------------------------------
async def test_get_configs_decodes_base64_subscription():
    """Base64-подписка декодируется, мусорные строки отбрасываются."""
    configs = [
        "vless://uuid-1@1.2.3.4:443?type=tcp&security=reality#DE",
        "wireguard://key@1.2.3.4:51820#DE-WG",
        "не-конфиг просто текст",
    ]
    encoded = base64.b64encode("\n".join(configs).encode()).decode()
    fake = FakeXui(subscription=encoded)
    panel, client = make_panel(fake, inbound_ids=[1])
    async with client:
        created = await panel.create_user(UserSpec(email="user_6", days=1))
        result = await panel.get_configs(created.uuid)

    assert result == configs[:2]
    sub_requests = [r for r in fake.requests if r.url.path.startswith("/sub/")]
    assert len(sub_requests) == 1
    assert sub_requests[0].url.path == f"/sub/{created.raw['sub_id']}"


async def test_get_configs_supports_plain_text_subscription():
    """Если subEncrypt выключен, сервис подписок отдаёт plain-text."""
    configs = ["trojan://pass@1.2.3.4:443#DE", "amneziawg://key@1.2.3.4:51820#AWG"]
    fake = FakeXui(subscription="\n".join(configs) + "\n")
    panel, client = make_panel(fake, inbound_ids=[1])
    async with client:
        created = await panel.create_user(UserSpec(email="user_7", days=1))
        result = await panel.get_configs(created.uuid)

    assert result == configs


async def test_get_configs_raises_when_subscription_service_unavailable():
    """Недоступный сервис подписок — понятный PanelError."""
    fake = FakeXui(subscription_status=502)
    panel, client = make_panel(fake, inbound_ids=[1])
    async with client:
        created = await panel.create_user(UserSpec(email="user_8", days=1))
        with pytest.raises(PanelError, match="сервис подписок"):
            await panel.get_configs(created.uuid)


async def test_get_configs_without_sub_base_raises():
    """Без sub_base ссылку на подписку собрать не из чего."""
    fake = FakeXui()
    panel, client = make_panel(fake, inbound_ids=[1], sub_base="")
    async with client:
        created = await panel.create_user(UserSpec(email="user_9", days=1))
        with pytest.raises(PanelError, match="sub_base"):
            await panel.get_configs(created.uuid)


# ---------------------------------------------------------------------------
#  Надёжность
# ---------------------------------------------------------------------------
async def test_success_false_becomes_panel_error_with_panel_message():
    """``success=false`` → PanelError с сообщением панели (а не «тихий» пустой ответ)."""
    fake = FakeXui(token=TOKEN, require_auth=True, fail_list="database is locked")
    panel, client = make_panel(fake, token=TOKEN)
    async with client:
        with pytest.raises(PanelError, match="database is locked"):
            await panel.list_inbounds()
        assert await panel.health() is False


async def test_get_user_survives_missing_traffic_record():
    """Нет записи о трафике — used_bytes=0, состояние клиента не теряется."""
    fake = FakeXui()
    panel, client = make_panel(fake, inbound_ids=[1])
    async with client:
        created = await panel.create_user(UserSpec(email="user_11", days=2))
        user = await panel.get_user(created.uuid)

    assert user is not None
    assert user.used_bytes == 0
    assert "no traffic record" in user.raw["traffic_error"]


async def test_http_error_and_unreachable_panel_raise_panel_error():
    """HTTP 500 и сетевой сбой дают PanelError, а health() — просто False."""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network is unreachable", request=request)

    unreachable_client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    unreachable = XuiPanel(BASE, token=TOKEN, client=unreachable_client)
    async with unreachable_client:
        assert await unreachable.health() is False
        with pytest.raises(PanelError, match="сетевая ошибка"):
            await unreachable.list_inbounds()

    def panic(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"success": False, "msg": "panic"})

    broken_client = httpx.AsyncClient(transport=httpx.MockTransport(panic))
    broken = XuiPanel(BASE, token=TOKEN, client=broken_client)
    async with broken_client:
        with pytest.raises(PanelError, match="HTTP 500"):
            await broken.list_inbounds()
        assert await broken.health() is False


async def test_health_returns_false_on_bad_token_instead_of_raising():
    """health() не бросает исключений даже при отказе авторизации."""
    fake = FakeXui(token=TOKEN, require_auth=True)
    panel, client = make_panel(fake, token="wrong-token")
    async with client:
        assert await panel.health() is False


async def test_close_closes_only_own_client():
    """Внешний httpx-клиент не закрывается, свой — закрывается."""
    fake = FakeXui()
    external_client = fake.client()
    panel = XuiPanel(BASE, token=TOKEN, client=external_client)
    async with external_client:
        await panel.close()
        assert not external_client.is_closed

    own_panel = XuiPanel(BASE, token=TOKEN)
    await own_panel.close()
    assert own_panel._client.is_closed  # noqa: SLF001


async def test_xui_panel_follows_panel_client_contract():
    """XuiPanel реализует контракт PanelClient: методы и сигнатуры совпадают."""
    from app.panels.base import PanelClient

    assert not inspect.isabstract(XuiPanel), "все абстрактные методы должны быть реализованы"
    for method in (
        "health",
        "list_inbounds",
        "create_user",
        "get_user",
        "find_user_by_email",
        "update_user",
        "delete_user",
        "get_configs",
        "close",
    ):
        expected = inspect.signature(getattr(PanelClient, method))
        actual = inspect.signature(getattr(XuiPanel, method))
        assert [
            (p.name, p.kind, p.default) for p in expected.parameters.values()
        ] == [
            (p.name, p.kind, p.default) for p in actual.parameters.values()
        ], f"сигнатура {method} разошлась с контрактом"


async def test_constructor_requires_base_url():
    """Пустой base_url — ошибка сразу, без сетевых вызовов."""
    with pytest.raises(PanelError, match="base_url"):
        XuiPanel("")
    with pytest.raises(PanelError, match="base_url"):
        XuiPanel("   ")
