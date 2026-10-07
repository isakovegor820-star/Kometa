"""Клиент реальной панели 3x-ui (https://github.com/MHSanaei/3x-ui).

Класс :class:`XuiPanel` реализует контракт :class:`app.panels.base.PanelClient`
поверх HTTP API панели 3x-ui.

Авторизация (поддерживаются оба способа):

* **API-токен** — заголовок ``Authorization: Bearer <token>``
  (``checkAPIAuth`` в ``internal/web/controller/api.go``). Если токен задан,
  он используется всегда, логин по паролю не выполняется;
* **логин/пароль** — ``POST /login``, дальше cookie-сессия. Cookie хранит сам
  ``httpx.AsyncClient``, нам достаточно один раз авторизоваться. Начиная с
  3x-ui v3.x ``POST /login`` закрыт CSRF-мидлварью
  (``internal/web/middleware/security.go``), поэтому перед логином клиент
  пытается получить токен через ``GET /csrf-token`` и передать его в заголовке
  ``X-CSRF-Token``; на старых панелях этого маршрута нет — тогда логин
  выполняется как раньше, без CSRF.

Два поколения API управления клиентами:

* «старое» (3x-ui ≤ v3.0.x, описано в ТЗ): ``/panel/api/inbounds/addClient``,
  ``/updateClient/<uuid>``, ``/<inbound_id>/delClient/<uuid>``,
  ``/getClientTraffics/<email>``;
* «новое» (3x-ui ≥ v3.1.0, включая актуальную v3.9.0):
  ``/panel/api/clients/add``, ``/update/<email>``, ``/del/<email>``,
  ``/traffic/<email>`` — старые маршруты из контроллера инбаундов удалены.

Клиент по умолчанию работает по старой схеме (как требует ТЗ), а при ответе
404/405 на старый маршрут один раз переключается на новую и больше
отсутствующий маршрут не дёргает. Это позволяет одним кодом работать и со
старыми панелями, и с актуальной v3.9.x, где старых маршрутов уже нет.

Все сетевые вызовы идут через ``httpx.AsyncClient``, который можно передать
снаружи (в тестах — с ``httpx.MockTransport``); созданный внутри клиент
закрывается в :meth:`XuiPanel.close`, переданный извне — нет.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import secrets
import string
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from app.panels.base import Inbound, PanelClient, PanelError, PanelUser, UserSpec

#: Байт в гибибайте. Панель хранит лимит в поле ``totalGB`` именно в байтах
#: (см. ``ldap_sync_job.go``: ``TotalGB: int64(defGB) * 1024 * 1024 * 1024``).
GIB = 1024**3

#: Миллисекунд в сутках: ``expiryTime`` в 3x-ui задаётся в миллисекундах
#: (``time.UnixMilli(...)``, везде в коде делится на 1000).
DAY_MS = 86_400_000

#: Алфавит и длина генерируемого ``subId`` (16 символов, как у самой панели).
SUB_ID_ALPHABET = string.ascii_lowercase + string.digits
SUB_ID_LENGTH = 16

#: Схемы, строки с которых считаются готовыми конфигами подписки.
KNOWN_SCHEMES: tuple[str, ...] = (
    "vless://",
    "vmess://",
    "trojan://",
    "ss://",
    "wireguard://",
    "amneziawg://",
    "hysteria2://",
    "hysteria://",
)

#: flow XTLS Vision — нужен только VLESS + TCP + Reality.
VISION_FLOW = "xtls-rprx-vision"


class _RouteMissing(Exception):
    """Маршрут эндпоинта отсутствует в этой версии панели (HTTP 404/405)."""


def _looks_like_auth_failure(status_code: int) -> bool:
    """Похож ли ответ на «сессия недействительна».

    3x-ui отвечает 401/403 на запрос с неверным Bearer-токеном и просто 302/404,
    если cookie-сессия кончилась (``checkAPIAuth`` в
    ``internal/web/controller/api.go``: без логина неизвестный путь отдаёт 404).
    Поэтому 404 тоже считается поводом перелогиниться — но только один раз.
    """
    return status_code in (401, 403, 404, 405) or 300 <= status_code < 400


# ---------------------------------------------------------------------------
#  Вспомогательные функции
# ---------------------------------------------------------------------------
def _as_json_dict(value: Any) -> dict[str, Any]:
    """Привести ``settings``/``streamSettings`` к словарю.

    3x-ui ≤ v3.0 отдаёт эти поля JSON-*строкой*, v3.9 — уже вложенным объектом
    (``Inbound.MarshalJSON`` в ``internal/database/model/model.go``). Понимаем
    оба варианта, как и сама панель на приёме.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _ms_to_datetime(ms: Any) -> datetime | None:
    """Миллисекунды эпохи → ``datetime`` в UTC (``0`` = бессрочно → ``None``)."""
    try:
        value = int(ms or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _datetime_to_ms(moment: datetime) -> int:
    """``datetime`` → миллисекунды эпохи (целое)."""
    return int(moment.timestamp() * 1000)


def _random_sub_id() -> str:
    """Случайный ``subId`` из строчных латинских букв и цифр.

    Такой алфавит заведомо проходит серверную проверку
    ``validateClientSubID`` (запрещены ``/``, ``\\``, пробелы и управляющие
    символы) и безопасен в URL подписки.
    """
    return "".join(secrets.choice(SUB_ID_ALPHABET) for _ in range(SUB_ID_LENGTH))


def _flow_for_inbound(inbound: dict[str, Any]) -> str:
    """flow для клиента в конкретном инбаунде.

    Vision (``xtls-rprx-vision``) имеет смысл только для VLESS + TCP + Reality
    (та же логика в ``inboundCanEnableTlsFlow``,
    ``internal/web/service/inbound_protocol.go``). Если в инбаунде flow явно
    выключен флагом ``disableFlow``, не навязываем его.
    """
    if inbound.get("disableFlow"):
        return ""
    if str(inbound.get("protocol") or "").lower() != "vless":
        return ""
    stream = _as_json_dict(inbound.get("streamSettings"))
    if str(stream.get("network") or "").lower() != "tcp":
        return ""
    if str(stream.get("security") or "").lower() != "reality":
        return ""
    return VISION_FLOW


def _try_base64(text: str) -> str | None:
    """Попытаться декодировать base64 (обычный или url-safe). Иначе ``None``."""
    compact = "".join(text.split())
    if not compact:
        return None
    padded = compact + "=" * (-len(compact) % 4)
    for candidate in (padded, padded.replace("-", "+").replace("_", "/")):
        try:
            raw = base64.b64decode(candidate, validate=True)
        except (binascii.Error, ValueError):
            continue
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        # Подписка в base64 почти всегда содержит ссылки; если «декод» вышел
        # без ссылок, это скорее случайное совпадение — считаем текст plain.
        if "://" in decoded:
            return decoded
    return None


def _decode_subscription(text: str) -> str:
    """Вернуть содержимое подписки как plain-text.

    Сервис подписок отдаёт base64, если включена настройка ``subEncrypt``, и
    обычный текст, если выключена (``internal/sub/controller.go``), поэтому
    поддерживаем оба варианта.
    """
    decoded = _try_base64(text)
    return decoded if decoded is not None else text


def _extract_configs(text: str) -> list[str]:
    """Выбрать из ответа подписки непустые строки с известными схемами."""
    configs: list[str] = []
    for line in _decode_subscription(text).splitlines():
        stripped = line.strip()
        if stripped.startswith(KNOWN_SCHEMES):
            configs.append(stripped)
    return configs


class XuiPanel(PanelClient):
    """Клиент панели 3x-ui.

    :param base_url: адрес панели, например ``http://1.2.3.4:54321``
        (можно с префиксом пути: ``http://host:54321/mypanel``).
    :param token: API-токен панели (Настройки → API-токен). Если задан,
        используется вместо логина по паролю.
    :param username: логин администратора панели.
    :param password: пароль администратора панели.
    :param inbound_ids: инбаунды, в которые добавляется клиент и из которых
        собирается подписка. Пустой список = все инбаунды панели.
    :param sub_base: базовый адрес сервиса подписок, например
        ``http://1.2.3.4:2096/sub/``. Ссылка клиента = ``sub_base + subId``.
    :param timeout: таймаут HTTP-запроса в секундах.
    :param client: готовый ``httpx.AsyncClient`` (в тестах — с
        ``MockTransport``). Если не передан, клиент создаётся и закрывается
        самим ``XuiPanel``.
    """

    name = "xui"

    def __init__(
        self,
        base_url: str,
        token: str = "",
        username: str = "",
        password: str = "",
        inbound_ids: list[int] | None = None,
        sub_base: str = "",
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        base = (base_url or "").strip()
        if not base:
            raise PanelError("не задан адрес панели 3x-ui (base_url)")
        self.base_url = base.rstrip("/")
        self.token = (token or "").strip()
        self.username = (username or "").strip()
        self.password = password or ""
        self.inbound_ids: list[int] = [int(item) for item in (inbound_ids or [])]
        self.sub_base = self._normalize_sub_base(sub_base)
        self.timeout = float(timeout)

        self._client = client if client is not None else httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=False
        )
        self._owns_client = client is None

        self._logged_in = False
        self._login_lock = asyncio.Lock()
        # None — ещё не знаем, False — старых маршрутов нет (панель ≥ v3.1).
        self._legacy_clients_api: bool | None = None

    # ------------------------------------------------------------------
    #  Служебное
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_sub_base(sub_base: str) -> str:
        """Привести базу подписки к виду, пригодному для склейки с ``subId``."""
        base = (sub_base or "").strip()
        if base and not base.endswith("/"):
            base += "/"
        return base

    def _url(self, path: str) -> str:
        """Полный URL эндпоинта панели."""
        return f"{self.base_url}{path}"

    def _auth_headers(self) -> dict[str, str]:
        """Заголовок авторизации для режима API-токена."""
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    def _subscription_url(self, sub_id: Any) -> str:
        """Ссылка на подписку по ``subId`` (пустая, если база не настроена)."""
        sub_id_str = str(sub_id or "").strip()
        if not sub_id_str or not self.sub_base:
            return ""
        return f"{self.sub_base}{sub_id_str}"

    async def _send(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        json_body: Any = None,
        url: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Выполнить HTTP-запрос, превратив сетевые сбои в ``PanelError``."""
        target = url or self._url(path)
        request_headers = self._auth_headers()
        if headers:
            request_headers.update(headers)
        try:
            return await self._client.request(
                method, target, data=data, json=json_body, headers=request_headers
            )
        except httpx.TimeoutException as exc:
            raise PanelError(
                f"панель {self.base_url} не ответила за {self.timeout:g} с ({method} {path})"
            ) from exc
        except httpx.HTTPError as exc:
            raise PanelError(
                f"сетевая ошибка при обращении к панели ({method} {path}): {exc}"
            ) from exc

    @staticmethod
    def _parse_json(response: httpx.Response, what: str) -> dict[str, Any]:
        """Разобрать JSON-ответ панели, проверяя его форму."""
        try:
            payload = response.json()
        except ValueError as exc:
            raise PanelError(
                f"панель вернула не-JSON ответ на {what} — проверь base_url и авторизацию"
            ) from exc
        if not isinstance(payload, dict):
            raise PanelError(f"панель вернула неожиданный JSON на {what}: {type(payload).__name__}")
        return payload

    async def _ensure_auth(self) -> None:
        """Гарантировать авторизацию: токен уже есть либо логинимся по паролю."""
        if self.token or self._logged_in:
            return
        await self._login()

    async def _relogin(self) -> str:
        """Перелогиниться, если сессия протухла.

        :return: пустая строка при успехе, иначе текст ошибки логина. Ошибку не
            поднимаем: по ответу панели нужно ещё отличить протухшую cookie от
            реально отсутствующего маршрута (3x-ui отдаёт 404 в обоих случаях).
        """
        try:
            await self._login(force=True)
        except PanelError as exc:
            return str(exc)
        return ""

    async def _fetch_csrf_token(self) -> str:
        """Получить CSRF-токен панели (нужен для ``POST /login`` в 3x-ui v3.x).

        На старых панелях маршрута ``/csrf-token`` нет — возвращаем пустую
        строку и логинимся без CSRF.
        """
        try:
            response = await self._send("GET", "/csrf-token")
        except PanelError:
            return ""
        if response.status_code != 200:
            return ""
        try:
            payload = response.json()
        except ValueError:
            return ""
        if isinstance(payload, dict):
            token = payload.get("obj")
            if isinstance(token, str):
                return token
        return ""

    async def _login(self, force: bool = False) -> None:
        """Авторизоваться по логину/паролю, сохранив cookie в httpx-клиенте."""
        if self.token:
            return
        async with self._login_lock:
            if self._logged_in and not force:
                return
            self._logged_in = False
            if not self.username or not self.password:
                raise PanelError(
                    "не заданы ни API-токен панели, ни логин с паролем — авторизоваться нечем"
                )

            csrf = await self._fetch_csrf_token()
            data: dict[str, Any] = {"username": self.username, "password": self.password}
            headers: dict[str, str] = {}
            if csrf:
                data["_csrf"] = csrf
                headers["X-CSRF-Token"] = csrf

            response = await self._send("POST", "/login", data=data, headers=headers)
            if response.status_code >= 400:
                raise PanelError(
                    f"панель отклонила логин (HTTP {response.status_code}) — "
                    "проверь логин, пароль и доступность /login"
                )
            payload = self._parse_json(response, "POST /login")
            if payload.get("success") is not True:
                reason = str(payload.get("msg") or "неверный логин или пароль")
                raise PanelError(f"не удалось авторизоваться в панели: {reason}")
            self._logged_in = True

    async def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        json_body: Any = None,
        legacy_route: bool = False,
    ) -> Any:
        """Запрос к API панели с авторизацией и разбором конверта ``success/obj``.

        :param legacy_route: помечает «старые» маршруты клиентов. Если такой
            маршрут отвечает 404/405, поднимается :class:`_RouteMissing`, и
            вызывающий код переключается на API ``/panel/api/clients/*``.
        :raises PanelError: сеть, таймаут, HTTP-ошибка, ``success=false``.
        """
        if not self.token:
            await self._ensure_auth()
        response = await self._send(method, path, data=data, json_body=json_body)

        if not self.token and _looks_like_auth_failure(response.status_code):
            # Протухшая сессия: 3x-ui отвечает 401/403/302, а на /panel/api/*
            # без валидной сессии — вообще 404. Пробуем перелогиниться и
            # повторить запрос ровно один раз.
            error = await self._relogin()
            if error:
                raise PanelError(f"не удалось восстановить сессию панели: {error}")
            response = await self._send(method, path, data=data, json_body=json_body)

        if legacy_route and response.status_code in (404, 405):
            raise _RouteMissing(f"{method} {path}")
        if response.status_code == 401:
            raise PanelError(
                "панель отклонила запрос (HTTP 401) — проверь API-токен или логин с паролем"
            )
        if response.status_code >= 400:
            raise PanelError(f"панель ответила HTTP {response.status_code} на {method} {path}")

        payload = self._parse_json(response, f"{method} {path}")
        if payload.get("success") is False:
            reason = str(payload.get("msg") or "причина не указана")
            raise PanelError(f"панель отклонила запрос {method} {path}: {reason}")
        return payload.get("obj")

    # ------------------------------------------------------------------
    #  Инбаунды и клиенты панели
    # ------------------------------------------------------------------
    async def _fetch_inbounds(self) -> list[dict[str, Any]]:
        """Сырой список инбаундов панели (``GET /panel/api/inbounds/list``)."""
        obj = await self._request("GET", "/panel/api/inbounds/list")
        if obj is None:
            return []
        if not isinstance(obj, list):
            raise PanelError("неожиданный формат ответа /panel/api/inbounds/list: ожидался список")
        return [item for item in obj if isinstance(item, dict)]

    async def _target_inbounds(self) -> list[dict[str, Any]]:
        """Инбаунды, в которые добавляем клиента и из которых собираем подписку."""
        inbounds = await self._fetch_inbounds()
        if not inbounds:
            raise PanelError("в панели 3x-ui нет ни одного инбаунда")
        if not self.inbound_ids:
            return inbounds
        wanted = set(self.inbound_ids)
        selected = [item for item in inbounds if int(item.get("id") or 0) in wanted]
        missing = sorted(wanted - {int(item.get("id") or 0) for item in selected})
        if missing:
            raise PanelError(f"в панели не найдены инбаунды {missing} (проверь inbound_ids)")
        return selected

    @staticmethod
    def _find_client(
        inbounds: list[dict[str, Any]], uuid: str
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Найти клиента по uuid во всех инбаундах.

        Клиенты лежат в ``settings.clients``; в 3x-ui ≤ v3.0 ``settings`` —
        JSON-строка, в v3.9 — уже объект, поэтому разбор идёт через
        :func:`_as_json_dict`.
        """
        return XuiPanel._find_clients(inbounds, "id", uuid)

    @staticmethod
    def _find_client_by_email(
        inbounds: list[dict[str, Any]], email: str
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Найти клиента по email во всех инбаундах."""
        return XuiPanel._find_clients(inbounds, "email", email)

    @staticmethod
    def _find_clients(
        inbounds: list[dict[str, Any]], field: str, value: str
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Найти клиентов с заданным значением поля (``id`` или ``email``)."""
        found: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for inbound in inbounds:
            clients = _as_json_dict(inbound.get("settings")).get("clients")
            if not isinstance(clients, list):
                continue
            for client in clients:
                if isinstance(client, dict) and str(client.get(field) or "") == value:
                    found.append((inbound, client))
        return found

    # ------------------------------------------------------------------
    #  Вызовы API клиентов: старые маршруты и API v3
    # ------------------------------------------------------------------
    async def _legacy_add_client(self, inbound_id: int, client: dict[str, Any]) -> None:
        """``POST /panel/api/inbounds/addClient`` (старый API)."""
        await self._request(
            "POST",
            "/panel/api/inbounds/addClient",
            data={"id": str(inbound_id), "settings": json.dumps({"clients": [client]}, ensure_ascii=False)},
            legacy_route=True,
        )

    async def _legacy_update_client(self, inbound_id: int, client: dict[str, Any]) -> None:
        """``POST /panel/api/inbounds/updateClient/<uuid>`` (старый API)."""
        uuid = quote(str(client.get("id") or ""), safe="")
        await self._request(
            "POST",
            f"/panel/api/inbounds/updateClient/{uuid}",
            data={"id": str(inbound_id), "settings": json.dumps({"clients": [client]}, ensure_ascii=False)},
            legacy_route=True,
        )

    async def _legacy_delete_client(self, inbound_id: int, uuid: str) -> None:
        """``POST /panel/api/inbounds/<inbound_id>/delClient/<uuid>``."""
        await self._request(
            "POST",
            f"/panel/api/inbounds/{int(inbound_id)}/delClient/{quote(uuid, safe='')}",
            legacy_route=True,
        )

    async def _v3_add_client(self, client: dict[str, Any], inbound_ids: list[int]) -> None:
        """``POST /panel/api/clients/add`` (3x-ui ≥ v3.1.0)."""
        await self._request(
            "POST",
            "/panel/api/clients/add",
            json_body={"client": client, "inboundIds": [int(item) for item in inbound_ids]},
        )

    async def _v3_update_client(self, email: str, client: dict[str, Any]) -> None:
        """``POST /panel/api/clients/update/<email>`` (3x-ui ≥ v3.1.0)."""
        await self._request(
            "POST",
            f"/panel/api/clients/update/{quote(email, safe='')}",
            json_body=client,
        )

    async def _v3_delete_client(self, email: str) -> None:
        """``POST /panel/api/clients/del/<email>`` (3x-ui ≥ v3.1.0)."""
        await self._request("POST", f"/panel/api/clients/del/{quote(email, safe='')}")

    async def _client_traffic(self, email: str) -> dict[str, Any]:
        """Трафик клиента: старый ``getClientTraffics`` либо ``clients/traffic``."""
        quoted = quote(email, safe="")
        if self._legacy_clients_api is not False:
            try:
                obj = await self._request(
                    "GET", f"/panel/api/inbounds/getClientTraffics/{quoted}", legacy_route=True
                )
                self._legacy_clients_api = True
                return obj if isinstance(obj, dict) else {}
            except _RouteMissing:
                self._legacy_clients_api = False
        obj = await self._request("GET", f"/panel/api/clients/traffic/{quoted}")
        return obj if isinstance(obj, dict) else {}

    # ------------------------------------------------------------------
    #  Контракт PanelClient
    # ------------------------------------------------------------------
    async def health(self) -> bool:
        """Панель отвечает и авторизация проходит (дешёвый список инбаундов)."""
        try:
            await self._request("GET", "/panel/api/inbounds/list")
        except PanelError:
            return False
        except Exception:  # noqa: BLE001 - health обязан вернуть bool, а не упасть
            return False
        return True

    async def list_inbounds(self) -> list[Inbound]:
        """Инбаунды, из которых собирается подписка (с учётом ``inbound_ids``)."""
        inbounds = await self._target_inbounds()
        result: list[Inbound] = []
        for item in inbounds:
            stream = _as_json_dict(item.get("streamSettings"))
            result.append(
                Inbound(
                    id=int(item.get("id") or 0),
                    remark=str(item.get("remark") or ""),
                    protocol=str(item.get("protocol") or ""),
                    port=int(item.get("port") or 0),
                    network=str(stream.get("network") or ""),
                    security=str(stream.get("security") or ""),
                )
            )
        return result

    async def list_users(self) -> list[PanelUser]:
        """Все клиенты целевых инбаундов — для аудита и контроля аномалий.

        Трафик берём из ``clientStats`` того же ответа панели: отдельный запрос
        на каждого клиента превратил бы суточный аудит в N+1 обращений, а на
        сотне клиентов это заметная нагрузка.
        """
        inbounds = await self._target_inbounds()
        result: list[PanelUser] = []
        seen: set[str] = set()

        for inbound in inbounds:
            settings = _as_json_dict(inbound.get("settings"))
            stats = {
                str(item.get("email") or ""): item
                for item in (inbound.get("clientStats") or [])
                if isinstance(item, dict)
            }
            for client in settings.get("clients") or []:
                if not isinstance(client, dict):
                    continue
                uuid = str(client.get("id") or "")
                email = str(client.get("email") or "")
                if not uuid or uuid in seen:
                    continue
                seen.add(uuid)
                stat = stats.get(email) or {}
                result.append(
                    PanelUser(
                        uuid=uuid,
                        email=email,
                        enabled=bool(client.get("enable", True)),
                        expires_at=_ms_to_datetime(client.get("expiryTime")),
                        traffic_limit_bytes=int(client.get("totalGB") or 0),
                        devices_limit=int(client.get("limitIp") or 0),
                        used_bytes=int(stat.get("up") or 0) + int(stat.get("down") or 0),
                        subscription_url=self._subscription_url(client.get("subId")),
                        raw={
                            "client": client,
                            "inbound_id": int(inbound.get("id") or 0),
                            "last_online_ms": int(stat.get("lastOnline") or 0),
                        },
                    )
                )
        return result

    async def create_user(self, spec: UserSpec) -> PanelUser:
        """Создать клиента с одним uuid/subId во всех целевых инбаундах.

        ``days=0`` и ``traffic_gb=0`` означают бессрочный доступ и безлимит
        (``expiryTime=0`` и ``totalGB=0`` соответственно).
        """
        inbounds = await self._target_inbounds()
        uid = str(spec.uuid or uuid_lib.uuid4())
        sub_id = _random_sub_id()
        expiry_ms = _datetime_to_ms(datetime.now(timezone.utc) + timedelta(days=spec.days)) if spec.days > 0 else 0
        total_bytes = int(spec.traffic_gb) * GIB

        base_client: dict[str, Any] = {
            "id": uid,
            "email": spec.email,
            "limitIp": int(spec.devices),
            "totalGB": total_bytes,
            "expiryTime": expiry_ms,
            "enable": True,
            "flow": "",
            "subId": sub_id,
            "tgId": 0,
            "reset": 0,
        }
        if spec.note:
            base_client["comment"] = spec.note

        used_legacy = self._legacy_clients_api is not False
        if used_legacy:
            try:
                for inbound in inbounds:
                    client = {**base_client, "flow": _flow_for_inbound(inbound)}
                    await self._legacy_add_client(int(inbound.get("id") or 0), client)
                self._legacy_clients_api = True
            except _RouteMissing:
                # Панель ≥ v3.1: старых маршрутов нет. Ни один клиент ещё не
                # добавлен (404 прилетел на первом же вызове), поэтому просто
                # повторяем операцию через новый API.
                self._legacy_clients_api = False
                used_legacy = False

        if not used_legacy:
            flows = {_flow_for_inbound(item) for item in inbounds}
            v3_client = {**base_client, "flow": VISION_FLOW if VISION_FLOW in flows else ""}
            await self._v3_add_client(v3_client, [int(item.get("id") or 0) for item in inbounds])

        return PanelUser(
            uuid=uid,
            email=spec.email,
            enabled=True,
            expires_at=_ms_to_datetime(expiry_ms),
            traffic_limit_bytes=total_bytes,
            devices_limit=int(spec.devices),
            used_bytes=0,
            subscription_url=self._subscription_url(sub_id),
            raw={
                "sub_id": sub_id,
                "inbound_ids": [int(item.get("id") or 0) for item in inbounds],
                "api": "legacy" if used_legacy else "v3",
            },
        )

    async def get_user(self, uuid: str) -> PanelUser | None:
        """Состояние клиента или ``None``, если такого uuid в панели нет.

        Трафик берётся из ``getClientTraffics``: если записи о трафике ещё нет
        (или эндпоинт недоступен), ``used_bytes`` остаётся нулём, а текст
        ошибки попадает в ``raw["traffic_error"]`` — состояние клиента при этом
        не теряется.
        """
        inbounds = await self._fetch_inbounds()
        found = self._find_client(inbounds, uuid)
        if not found:
            return None
        return await self._build_panel_user(found)

    async def find_user_by_email(self, email: str) -> PanelUser | None:
        """Найти клиента по email (нужно для восстановления после сбоя БД бота).

        Панель хранит email в поле ``email`` клиента; поиск идёт по всем
        инбаундам панели (не только по ``inbound_ids``), чтобы найти клиента,
        даже если состав инбаундов с тех пор поменялся.
        """
        inbounds = await self._fetch_inbounds()
        found = self._find_client_by_email(inbounds, email)
        if not found:
            return None
        return await self._build_panel_user(found)

    async def _build_panel_user(
        self, found: list[tuple[dict[str, Any], dict[str, Any]]]
    ) -> PanelUser:
        """Собрать :class:`PanelUser` по найденным вхождениям клиента."""
        inbound, client = found[0]
        email = str(client.get("email") or "")
        used_bytes = 0
        traffic_error = ""
        if email:
            try:
                traffic = await self._client_traffic(email)
                used_bytes = int(traffic.get("up") or 0) + int(traffic.get("down") or 0)
            except PanelError as exc:
                traffic_error = str(exc)

        return PanelUser(
            uuid=str(client.get("id") or ""),
            email=email,
            enabled=bool(client.get("enable", True)),
            expires_at=_ms_to_datetime(client.get("expiryTime")),
            traffic_limit_bytes=int(client.get("totalGB") or 0),
            devices_limit=int(client.get("limitIp") or 0),
            used_bytes=used_bytes,
            subscription_url=self._subscription_url(client.get("subId")),
            raw={
                "client": client,
                "inbound_id": int(inbound.get("id") or 0),
                "inbound_ids": [int(item.get("id") or 0) for item, _ in found],
                "api": "legacy" if self._legacy_clients_api is not False else "v3",
                "traffic_error": traffic_error,
            },
        )

    async def update_user(
        self,
        uuid: str,
        *,
        extend_days: int | None = None,
        traffic_gb: int | None = None,
        devices: int | None = None,
        enable: bool | None = None,
    ) -> PanelUser:
        """Продлить/изменить лимиты/включить-выключить доступ.

        ``extend_days`` прибавляется к текущему ``expiryTime``; если срок уже
        истёк — отсчёт идёт от «сейчас». Бессрочный доступ (``expiryTime=0``)
        продление не превращает в ограниченный.
        """
        inbounds = await self._fetch_inbounds()
        found = self._find_client(inbounds, uuid)
        if not found:
            raise PanelError(f"пользователь {uuid} не найден в панели 3x-ui")

        changes: dict[str, Any] = {}
        if extend_days:
            current_ms = int(found[0][1].get("expiryTime") or 0)
            if current_ms > 0:
                now_ms = _datetime_to_ms(datetime.now(timezone.utc))
                base_ms = current_ms if current_ms > now_ms else now_ms
                changes["expiryTime"] = base_ms + int(extend_days) * DAY_MS
        if traffic_gb is not None:
            changes["totalGB"] = int(traffic_gb) * GIB
        if devices is not None:
            changes["limitIp"] = int(devices)
        if enable is not None:
            changes["enable"] = bool(enable)

        email = str(found[0][1].get("email") or "")

        if self._legacy_clients_api is not False:
            try:
                for inbound, client in found:
                    await self._legacy_update_client(
                        int(inbound.get("id") or 0), {**client, **changes}
                    )
                self._legacy_clients_api = True
            except _RouteMissing:
                self._legacy_clients_api = False
        if self._legacy_clients_api is False:
            if not email:
                raise PanelError(f"у клиента {uuid} нет email — API v3 не сможет его обновить")
            await self._v3_update_client(email, {**found[0][1], **changes})

        updated = await self.get_user(uuid)
        if updated is not None:
            return updated
        # Панель уже приняла изменения, но клиента не видно (например, гонка
        # при перечитывании) — отдаём состояние, посчитанное локально.
        client = {**found[0][1], **changes}
        return PanelUser(
            uuid=uuid,
            email=email,
            enabled=bool(client.get("enable", True)),
            expires_at=_ms_to_datetime(client.get("expiryTime")),
            traffic_limit_bytes=int(client.get("totalGB") or 0),
            devices_limit=int(client.get("limitIp") or 0),
            subscription_url=self._subscription_url(client.get("subId")),
            raw={"client": client},
        )

    async def delete_user(self, uuid: str) -> None:
        """Удалить клиента из всех инбаундов.

        Операция идемпотентна: если клиента в панели уже нет, ничего не делаем
        (как и заглушка :class:`app.panels.fake.FakePanel`).
        """
        inbounds = await self._fetch_inbounds()
        found = self._find_client(inbounds, uuid)
        if not found:
            return

        if self._legacy_clients_api is not False:
            try:
                for inbound, _client in found:
                    await self._legacy_delete_client(int(inbound.get("id") or 0), uuid)
                self._legacy_clients_api = True
                return
            except _RouteMissing:
                self._legacy_clients_api = False
        email = str(found[0][1].get("email") or "")
        if not email:
            raise PanelError(f"у клиента {uuid} нет email — API v3 не сможет его удалить")
        await self._v3_delete_client(email)

    async def get_configs(self, uuid: str) -> list[str]:
        """Готовые строки конфигов из сервиса подписок панели.

        Сначала находим ``subId`` клиента в инбаундах, затем запрашиваем
        ``<sub_base><subId>`` и разбираем ответ (base64 или plain-text).
        """
        if not self.sub_base:
            raise PanelError("не задан sub_base — адрес сервиса подписок панели 3x-ui")

        inbounds = await self._fetch_inbounds()
        found = self._find_client(inbounds, uuid)
        if not found:
            raise PanelError(f"пользователь {uuid} не найден в панели 3x-ui")

        sub_id = str(found[0][1].get("subId") or "").strip()
        if not sub_id:
            raise PanelError(f"у клиента {uuid} не заполнен subId — подписка недоступна")

        url = self._subscription_url(sub_id)
        try:
            response = await self._client.get(url, follow_redirects=True)
        except httpx.TimeoutException as exc:
            raise PanelError(f"сервис подписок не ответил за {self.timeout:g} с: {url}") from exc
        except httpx.HTTPError as exc:
            raise PanelError(f"не удалось обратиться к сервису подписок {url}: {exc}") from exc

        if 300 <= response.status_code < 400:
            raise PanelError(
                f"сервис подписок {url} отвечает редиректом (HTTP {response.status_code}) — "
                "укажи в sub_base адрес, который отдаёт подписку напрямую"
            )
        if response.status_code >= 400:
            raise PanelError(
                f"сервис подписок вернул HTTP {response.status_code} для {url} — "
                "проверь sub_base и доступность подписки"
            )

        configs = _extract_configs(response.text)
        if not configs:
            raise PanelError(f"сервис подписок {url} не вернул ни одного конфига")
        return configs

    async def close(self) -> None:
        """Закрыть HTTP-клиент, если он создан внутри ``XuiPanel``."""
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()
