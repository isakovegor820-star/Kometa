"""Тесты серверной инвалидации админ-сессий (H6).

Что было не так. Cookie панели живёт 12 часов и подписана, но серверная сторона
её не помнила: выход удалял cookie только в браузере, смена пароля не мешала
работать прежней сессии, а понижение роли не отменяло прежних прав. Украденная
cookie (или просто открытая вкладка) продолжала делать то, что уже нельзя.

Что проверяем:

* активная сессия продолжает работать — мы не «выкидываем всех» на каждом входе;
* выход, смена пароля, смена роли и деактивация гасят выданную ранее cookie
  (303 на ``/admin/login``);
* проверка идёт по базе на каждом запросе, а не по данным внутри cookie;
* cookie без версии (выданная старой версией панели) не принимается.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.db.models import AdminAccount
from app.web import security
from app.web.sub import build_app

settings = get_settings()
PASSWORD = "test-admin-password"
MOD_PASSWORD = "moderator-pass-1"


@pytest.fixture(autouse=True)
def admin_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_panel_password", PASSWORD)
    monkeypatch.setattr(settings, "admin_panel_secret", "test-session-secret")
    security.login_throttle._attempts.clear()
    yield


async def _new_client() -> httpx.AsyncClient:
    app = await build_app(bot=None)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver", follow_redirects=False)


@pytest.fixture
async def owner_client():
    async with await _new_client() as client:
        yield client


@pytest.fixture
async def mod_client():
    async with await _new_client() as client:
        yield client


async def make_account(session, login: str, password: str, role: str, **kwargs) -> AdminAccount:  # noqa: ANN003
    account = AdminAccount(
        login=login,
        display_name=login.capitalize(),
        role=role,
        password_hash=security.hash_password(password),
        **kwargs,
    )
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account


async def login_owner(client: httpx.AsyncClient) -> None:
    response = await client.post("/admin/login", data={"password": PASSWORD})
    assert response.status_code == 303


async def login_account(client: httpx.AsyncClient, login: str, password: str) -> None:
    response = await client.post("/admin/login", data={"login": login, "password": password})
    assert response.status_code == 303
    assert security.COOKIE_NAME in response.cookies


def cookie_token(client: httpx.AsyncClient) -> str:
    token = client.cookies.get(security.COOKIE_NAME)
    assert token
    return token


def assert_login_redirect(response: httpx.Response) -> None:
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


# ------------------------------------------------------------- рабочая сессия
async def test_active_session_keeps_working(owner_client, session):
    """Обычная работа: активная сессия не должна ломаться от самой проверки."""
    await login_owner(owner_client)

    first = await owner_client.get("/admin")
    second = await owner_client.get("/admin/users")

    assert first.status_code == 200
    assert second.status_code == 200


async def test_account_session_keeps_working(mod_client, session):
    """Сессия учётной записи с версией работает, пока учётку не меняли."""
    await make_account(session, "moder", MOD_PASSWORD, "moderator")
    await login_account(mod_client, "moder", MOD_PASSWORD)

    response = await mod_client.get("/admin")

    assert response.status_code == 200


# ------------------------------------------------------------------- logout
async def test_logout_invalidates_the_cookie(mod_client, session):
    """Выход гасит сессию на сервере: старая cookie больше не принимается."""
    await make_account(session, "modout", MOD_PASSWORD, "moderator")
    await login_account(mod_client, "modout", MOD_PASSWORD)
    stolen = cookie_token(mod_client)

    logout = await mod_client.post("/admin/logout")
    assert logout.status_code == 303

    mod_client.cookies.set(security.COOKIE_NAME, stolen)
    response = await mod_client.get("/admin")

    assert_login_redirect(response)


# ------------------------------------------------------------ смена пароля
async def test_password_change_invalidates_old_cookie(owner_client, mod_client, session):
    """Сменили пароль — прежние сессии (в том числе краденые) недействительны."""
    account = await make_account(session, "modpass", MOD_PASSWORD, "moderator")
    await login_account(mod_client, "modpass", MOD_PASSWORD)
    stolen = cookie_token(mod_client)

    await login_owner(owner_client)
    changed = await owner_client.post(
        f"/admin/team/{account.id}",
        data={
            "display_name": "Модер",
            "role": "moderator",
            "is_active": "on",
            "tg_id": "",
            "password": "brand-new-pass-1",
        },
    )
    assert changed.status_code == 303

    mod_client.cookies.set(security.COOKIE_NAME, stolen)
    assert_login_redirect(await mod_client.get("/admin"))

    # И новый пароль работает: человека не «выключили», а только разлогинили.
    fresh = await _new_client()
    async with fresh:
        await login_account(fresh, "modpass", "brand-new-pass-1")
        assert (await fresh.get("/admin")).status_code == 200


# -------------------------------------------------------------- смена роли
async def test_role_downgrade_invalidates_old_cookie(owner_client, mod_client, session):
    """Понижение роли не оставляет прежних прав в уже выданной cookie."""
    account = await make_account(session, "modrole", MOD_PASSWORD, "moderator")
    await login_account(mod_client, "modrole", MOD_PASSWORD)
    stolen = cookie_token(mod_client)

    await login_owner(owner_client)
    downgraded = await owner_client.post(
        f"/admin/team/{account.id}",
        data={"display_name": "Модер", "role": "support", "is_active": "on", "tg_id": "", "password": ""},
    )
    assert downgraded.status_code == 303

    mod_client.cookies.set(security.COOKIE_NAME, stolen)
    assert_login_redirect(await mod_client.get("/admin"))

    # Свежий вход даёт уже роль поддержки — 403 на действие модератора.
    fresh = await _new_client()
    async with fresh:
        await login_account(fresh, "modrole", MOD_PASSWORD)
        assert (await fresh.get("/admin")).status_code == 200
        denied = await fresh.post("/admin/broadcast", data={"text": "привет", "audience": "all"})
        assert denied.status_code == 403


# -------------------------------------------------------------- деактивация
async def test_deactivation_invalidates_old_cookie(owner_client, mod_client, session):
    """Выключенная учётка не работает ни одной своей прежней cookie."""
    account = await make_account(session, "modoff", MOD_PASSWORD, "moderator")
    await login_account(mod_client, "modoff", MOD_PASSWORD)
    stolen = cookie_token(mod_client)

    await login_owner(owner_client)
    # В форме панели есть скрытое поле is_active=0 и чекбокс =1: снятая
    # галочка приходит как 0, и учётка выключается.
    disabled = await owner_client.post(
        f"/admin/team/{account.id}",
        data={"display_name": "Модер", "role": "moderator", "is_active": "0", "tg_id": "", "password": ""},
    )
    assert disabled.status_code == 303

    mod_client.cookies.set(security.COOKIE_NAME, stolen)
    assert_login_redirect(await mod_client.get("/admin"))


# ------------------------------------------ проверка идёт по базе, не по cookie
async def test_db_change_alone_invalidates_session(mod_client, session):
    """Даже если cookie не трогали, изменение учётки в БД закрывает доступ."""
    account = await make_account(session, "moddb", MOD_PASSWORD, "moderator")
    await login_account(mod_client, "moddb", MOD_PASSWORD)
    assert (await mod_client.get("/admin")).status_code == 200

    # Имитируем смену состояния вне маршрутов панели (скрипт, миграция, другой процесс).
    account.is_active = False
    account.session_version = int(account.session_version or 1) + 1
    await session.commit()

    assert_login_redirect(await mod_client.get("/admin"))


async def test_cookie_without_version_is_rejected(mod_client, session):
    """Cookie старого формата (без версии) для учётной записи недействительна."""
    account = await make_account(session, "modold", MOD_PASSWORD, "moderator")
    await login_account(mod_client, "modold", MOD_PASSWORD)

    legacy = security.issue_session(
        name="Модер", role="moderator", account_id=account.id, tg_id=None  # без version
    )
    mod_client.cookies.set(security.COOKIE_NAME, legacy)

    assert_login_redirect(await mod_client.get("/admin"))


async def test_owner_password_session_is_not_tied_to_accounts(owner_client, session):
    """Владелец по паролю из .env работает и без учётной записи в БД."""
    await login_owner(owner_client)

    assert (await owner_client.get("/admin")).status_code == 200
    assert (await owner_client.get("/admin/team")).status_code == 200


async def test_tampered_version_is_rejected(mod_client, session):
    """Версию в cookie нельзя подделать: подпись не сойдётся."""
    account = await make_account(session, "modsign", MOD_PASSWORD, "moderator")
    await login_account(mod_client, "modsign", MOD_PASSWORD)

    token = cookie_token(mod_client)
    expires, encoded, signature = token.split(".", 2)
    forged = security.issue_session(
        name="Модер",
        role="moderator",
        account_id=account.id,
        version=int(account.session_version or 1) + 99,
    )
    # Подпись чужого токена к своей полезной нагрузке не подходит.
    mod_client.cookies.set(security.COOKIE_NAME, f"{forged.rsplit('.', 1)[0]}.{signature}")

    assert_login_redirect(await mod_client.get("/admin"))
