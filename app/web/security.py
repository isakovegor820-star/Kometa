"""Безопасность админ-панели: роли, подписанная cookie, защита от подбора.

Принципы:
  * **fail-closed**: пока не задан ни пароль в ``ADMIN_PANEL_PASSWORD``, ни одна
    учётная запись команды — панель не выполняет ни одного действия, а
    показывает страницу входа с объяснением;
  * пароли команды хранятся только хэшем (PBKDF2-HMAC-SHA256, 240 000 итераций);
  * сравнение пароля — в постоянном времени (``hmac.compare_digest``);
  * сессия — подписанная cookie со сроком жизни, ролью и именем;
  * подбор пароля ограничен по IP;
  * небезопасные методы дополнительно проверяются на same-origin.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AdminAccount, utcnow
from app.web.ui import ROLE_MODERATOR, ROLE_OWNER, ROLE_SUPPORT, ROLES, role_label

COOKIE_NAME = "kometa_admin"
#: Не больше N неудачных попыток входа с одного IP за окно.
MAX_LOGIN_ATTEMPTS = 10
LOGIN_WINDOW_SECONDS = 600

#: Параметры хэширования паролей команды. 240k итераций — ~0.1 c на проверку:
#: вход не тормозит, а перебор на украденном дампе БД становится дорогим.
PBKDF2_ITERATIONS = 240_000
_HASH_PREFIX = "pbkdf2_sha256"


# --------------------------------------------------------------------- пароли
def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS, salt: bytes | None = None) -> str:
    """Хэш пароля в формате ``pbkdf2_sha256$итерации$соль$хэш`` (всё в hex)."""
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{_HASH_PREFIX}${iterations}${salt.hex()}${digest.hex()}"


def verify_password_hash(password: str, stored: str | None) -> bool:
    """Проверить пароль против сохранённого хэша (постоянное время сравнения)."""
    if not stored:
        return False
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != _HASH_PREFIX:
        return False
    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected = bytes.fromhex(parts[3])
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(digest, expected)


def check_password(candidate: str) -> bool:
    """Пароль владельца из ``.env`` — обратная совместимость со старой панелью."""
    expected = get_settings().admin_panel_password
    if not expected:
        return False
    return hmac.compare_digest((candidate or "").strip(), expected)


# ------------------------------------------------------------------- сессия
class Session:
    """Кто вошёл в панель: имя, роль, учётная запись и версия её сессий."""

    __slots__ = ("name", "role", "account_id", "tg_id", "expires_at", "version")

    def __init__(
        self,
        name: str,
        role: str,
        *,
        account_id: int | None = None,
        tg_id: int | None = None,
        expires_at: int = 0,
        version: int = 0,
    ) -> None:
        self.name = name
        self.role = role
        self.account_id = account_id
        self.tg_id = tg_id
        self.expires_at = expires_at
        #: Версия сессий учётной записи на момент входа. Сверяется с БД при
        #: каждом запросе: выход, смена пароля, роли и деактивация её поднимают.
        self.version = int(version or 0)

    @property
    def role_label(self) -> str:
        return role_label(self.role)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "role_label": self.role_label,
            "account_id": self.account_id,
            "tg_id": self.tg_id,
            "version": self.version,
        }


def _secret() -> bytes:
    settings = get_settings()
    raw = settings.admin_panel_secret or settings.bot_token or "kometa-insecure-default"
    return raw.encode()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def issue_session(
    now: float | None = None,
    *,
    name: str = "владелец",
    role: str = ROLE_OWNER,
    account_id: int | None = None,
    tg_id: int | None = None,
    version: int = 0,
) -> str:
    """Создать значение cookie: ``<истекает>.<данные>.<подпись>``.

    Полезная нагрузка (имя, роль, учётная запись и версия её сессий) подписана,
    но не шифруется: секретов в ней нет, зато в журнале видно, кто действовал.
    """
    ttl = get_settings().admin_session_hours * 3600
    expires_at = int((now if now is not None else time.time()) + ttl)
    body = json.dumps(
        {"n": name[:64], "r": role, "a": account_id, "t": tg_id, "v": int(version or 0)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    encoded = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    payload = f"{expires_at}.{encoded}"
    return f"{payload}.{_sign(payload)}"


def read_session(token: str | None) -> Session | None:
    """Разобрать cookie. None — cookie нет, подпись не сходится или срок вышел."""
    if not token or token.count(".") < 2:
        return None
    expires_raw, encoded, signature = token.split(".", 2)
    if not expires_raw.isdigit():
        return None
    payload = f"{expires_raw}.{encoded}"
    if not hmac.compare_digest(signature, _sign(payload)):
        return None
    expires_at = int(expires_raw)
    if expires_at <= time.time():
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded).decode())
    except Exception:  # noqa: BLE001 - битая cookie не должна ронять панель
        return None
    role = str(data.get("r") or ROLE_OWNER)
    if role not in ROLES:
        role = ROLE_SUPPORT
    return Session(
        name=str(data.get("n") or "админ"),
        role=role,
        account_id=data.get("a"),
        tg_id=data.get("t"),
        expires_at=expires_at,
        version=int(data.get("v") or 0),
    )


def verify_session(token: str | None) -> bool:
    """Обратная совместимость: есть ли валидная сессия."""
    return read_session(token) is not None


# -------------------------------------------------------------------- доступ
def panel_enabled() -> bool:
    """Задан пароль владельца из .env (старый способ входа)."""
    return bool(get_settings().admin_panel_password)


async def any_account_exists(session: AsyncSession) -> bool:
    return (
        await session.scalar(select(AdminAccount.id).where(AdminAccount.is_active.is_(True)).limit(1))
    ) is not None


async def panel_available(session: AsyncSession) -> bool:
    """Панель настроена: есть пароль владельца или хотя бы одна учётная запись."""
    if panel_enabled():
        return True
    return await any_account_exists(session)


async def authenticate(session: AsyncSession, login: str, password: str) -> Session | None:
    """Проверить логин и пароль.

    * пустой логин — старый способ: пароль владельца из ``ADMIN_PANEL_PASSWORD``;
    * логин указан — ищем активную учётную запись команды.

    Возвращает сессию либо None. Никогда не говорит, что именно неверно.
    """
    login = (login or "").strip()
    password = (password or "").strip()

    if not login:
        if check_password(password):
            return Session(name="владелец", role=ROLE_OWNER)
        # Запасной путь: в панели одна учётная запись и человек вводит только пароль.
        account = await session.scalar(
            select(AdminAccount).where(AdminAccount.is_active.is_(True)).order_by(AdminAccount.id).limit(1)
        )
        if account is not None and verify_password_hash(password, account.password_hash):
            account.last_login_at = utcnow()
            return _session_for(account)
        return None

    account = await session.scalar(
        select(AdminAccount).where(AdminAccount.login == login.lower(), AdminAccount.is_active.is_(True))
    )
    if account is None or not verify_password_hash(password, account.password_hash):
        return None
    account.last_login_at = utcnow()
    return _session_for(account)


async def current_account(session: AsyncSession, auth: Session) -> AdminAccount | None:
    """Перечитать учётную запись по сессии: активна ли, та ли роль и версия.

    Зачем на каждый запрос. Cookie живёт 12 часов. За это время учётку могут
    выключить, понизить в роли или сменить ей пароль — и без перечитывания
    украденная (или просто старая) cookie продолжала бы работать. Здесь мы
    отвечаем на вопрос «эта сессия ещё действительна?» по базе, а не по данным
    внутри cookie, которые могли устареть.

    Для сессии владельца по паролю из .env учётной записи нет — такие сессии
    проверяются настройками панели, и здесь возвращается ``None`` с признаком
    «аккаунта нет» (см. :func:`session_is_valid`).
    """
    if auth.account_id is None:
        return None
    account = await session.get(AdminAccount, int(auth.account_id))
    if account is None or not account.is_active:
        return None
    return account


def session_matches_account(auth: Session, account: AdminAccount | None) -> bool:
    """Совпадают ли роль и версия сессии с текущим состоянием учётной записи."""
    if auth.account_id is None:
        # Владелец по паролю из .env: учётной записи нет, проверять нечего.
        return True
    if account is None:
        return False
    if int(account.session_version or 1) != int(auth.version or 0):
        return False
    return account.role == auth.role


async def bump_session_version(session: AsyncSession, account: AdminAccount) -> int:
    """Поднять версию сессий учётной записи — все её cookie станут недействительны.

    Вызывается при выходе, смене пароля, смене роли и деактивации. Возвращает
    новую версию, чтобы вызывающий код при желании выдал свежую cookie.
    """
    account.session_version = int(account.session_version or 1) + 1
    await session.flush()
    return account.session_version


def _session_for(account: AdminAccount) -> Session:
    return Session(
        name=account.name,
        role=account.role if account.role in ROLES else ROLE_MODERATOR,
        account_id=account.id,
        tg_id=account.tg_id,
        version=int(account.session_version or 1),
    )


# ------------------------------------------------------------------ IP и CSRF
def client_ip(request) -> str:  # noqa: ANN001 - starlette Request
    """IP клиента с учётом реверс-прокси.

    За nginx все запросы приходят с 127.0.0.1, и проверка «только localhost»
    перестала бы что-либо значить. Заголовку верим только если владелец явно
    сказал ``ADMIN_TRUST_PROXY=true``: иначе его подделает кто угодно.
    """
    settings = get_settings()
    if settings.admin_trust_proxy:
        forwarded = request.headers.get("x-forwarded-for") or ""
        if forwarded:
            return forwarded.split(",")[0].strip()
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
    return request.client.host if request.client else ""


def allowed_admin_ips() -> set[str]:
    settings = get_settings()
    base = {"127.0.0.1", "::1", "testclient"}
    base |= {ip.strip() for ip in settings.admin_allowed_ips.split(",") if ip.strip()}
    return base


def local_only_ok(request) -> bool:  # noqa: ANN001 - starlette Request
    settings = get_settings()
    if not settings.admin_local_only:
        return True
    return client_ip(request) in allowed_admin_ips()


def same_origin(request) -> bool:  # noqa: ANN001 - starlette Request
    """Проверка same-origin для POST/PUT/DELETE.

    Cookie сессии стоит с ``SameSite=Lax`` — этого достаточно против обычного
    кросс-сайтового POST. Дополнительный барьер нужен на случай, когда браузер
    старый или кука живёт в поддомене: сверяем Origin/Referer с хостом запроса.
    Запросы без Origin (curl, тесты, скрипты) не блокируем — они не CSRF.
    """
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if not origin:
        return True
    host = request.headers.get("host") or ""
    if not host:
        return True
    try:
        from urllib.parse import urlsplit

        return urlsplit(origin).netloc == host
    except Exception:  # noqa: BLE001
        return False


def cookie_secure(request=None) -> bool:  # noqa: ANN001 - starlette Request | None
    """Ставить ли флаг ``Secure`` у cookie сессии.

    Решаем по фактической схеме запроса, а не по настройкам: панель может
    работать и по HTTPS (сертификат задан), и по HTTP за туннелем. Если флаг
    поставить «на всякий случай», браузер перестанет отправлять cookie при
    доступе по HTTP, и вход будет выглядеть сломанным.

    Заголовку ``X-Forwarded-Proto`` верим только при ``ADMIN_TRUST_PROXY=true``:
    иначе его подделает кто угодно.
    """
    if request is None:
        return False
    settings = get_settings()
    if request.url.scheme == "https":
        return True
    if settings.admin_trust_proxy:
        forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if forwarded == "https":
            return True
    return False


# ------------------------------------------------------------------ троттлинг
class LoginThrottle:
    """Простое ограничение попыток входа: в памяти процесса."""

    def __init__(self) -> None:
        self._attempts: dict[str, list[float]] = {}

    def blocked(self, key: str) -> bool:
        now = time.time()
        recent = [t for t in self._attempts.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
        self._attempts[key] = recent
        return len(recent) >= MAX_LOGIN_ATTEMPTS

    def register_failure(self, key: str) -> None:
        self._attempts.setdefault(key, []).append(time.time())

    def reset(self, key: str) -> None:
        self._attempts.pop(key, None)

    def failures(self, key: str) -> int:
        now = time.time()
        return len([t for t in self._attempts.get(key, []) if now - t < LOGIN_WINDOW_SECONDS])


login_throttle = LoginThrottle()


def new_password(length: int = 16) -> str:
    """Пароль для новой учётной записи: его показываем один раз при создании."""
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))
