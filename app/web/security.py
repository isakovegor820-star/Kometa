"""Безопасность админ-панели: подписанная cookie и защита от подбора пароля.

Принципы:
  * панель выключена, пока не задан пароль (``ADMIN_PANEL_PASSWORD``);
  * пароль сравнивается в постоянном времени (``hmac.compare_digest``);
  * сессия — подписанная cookie с сроком жизни, без хранения состояния на сервере;
  * подбор пароля ограничен по IP.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from app.config import get_settings

COOKIE_NAME = "kometa_admin"
#: Не больше N неудачных попыток входа с одного IP за окно.
MAX_LOGIN_ATTEMPTS = 10
LOGIN_WINDOW_SECONDS = 600


def panel_enabled() -> bool:
    return bool(get_settings().admin_panel_password)


def _secret() -> bytes:
    settings = get_settings()
    raw = settings.admin_panel_secret or settings.bot_token or "kometa-insecure-default"
    return raw.encode()


def check_password(candidate: str) -> bool:
    expected = get_settings().admin_panel_password
    if not expected:
        return False
    return hmac.compare_digest(candidate.strip(), expected)


def issue_session(now: float | None = None) -> str:
    """Создать значение cookie: '<истекает>.<подпись>'."""
    ttl = get_settings().admin_session_hours * 3600
    expires_at = int((now if now is not None else time.time()) + ttl)
    payload = str(expires_at)
    signature = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{signature}"


def verify_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    payload, signature = token.rsplit(".", 1)
    if not payload.isdigit():
        return False
    expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(signature, expected):
        return False
    return int(payload) > time.time()


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


login_throttle = LoginThrottle()
