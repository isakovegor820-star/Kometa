"""Вход в панель: пароль (и логин, если в команде больше одного человека)."""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db.session import SessionMaker
from app.services import audit
from app.web import security
from app.config import get_settings
from app.web.admin.common import deny_if_cross_site, deny_if_foreign
from app.web.security import Session
from app.web.templating import templates

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()


def _fail(reason: str) -> RedirectResponse:
    """Ошибка входа — в query-строке: так ссылку можно переслать и показать в логе."""
    return RedirectResponse(f"/admin/login?error={quote(reason)}", status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    deny_if_foreign(request)
    deny_if_cross_site(request)

    async with SessionMaker() as db:
        available = await security.panel_available(db)
        accounts = None
        if available:
            from sqlalchemy import select

            from app.db.models import AdminAccount

            accounts = (
                await db.scalars(
                    select(AdminAccount).where(AdminAccount.is_active.is_(True)).order_by(AdminAccount.login)
                )
            ).all()

    if available and security.read_session(request.cookies.get(security.COOKIE_NAME)):
        return RedirectResponse("/admin", status_code=303)

    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "enabled": available,
            "message": "",
            "error": request.query_params.get("error", ""),
            "session_active": False,
            # Логин нужен только когда в команде есть учётные записи: один
            # владелец с паролем из .env вводит только пароль.
            "accounts": [account.login for account in accounts] if accounts else [],
        },
    )


@router.post("/login")
async def login_submit(request: Request, password: str = Form(""), login: str = Form("")):
    deny_if_foreign(request)
    deny_if_cross_site(request)

    client_key = security.client_ip(request) or "unknown"
    if security.login_throttle.blocked(client_key):
        return _fail("Слишком много попыток, подожди 10 минут")

    async with SessionMaker() as db:
        available = await security.panel_available(db)
        if not available:
            return _fail("Панель выключена: не задан ADMIN_PANEL_PASSWORD")

        session: Session | None = await security.authenticate(db, login, password)
        if session is None:
            security.login_throttle.register_failure(client_key)
            await audit.log_action(
                db,
                "admin.login_failed",
                actor=audit.Actor(
                    name=login.strip() or "неизвестный",
                    role="unknown",
                    ip=security.client_ip(request),
                ),
                payload={"attempts": security.login_throttle.failures(client_key)},
            )
            await db.commit()
            logger.warning("Неудачный вход в панель с %s (логин %r)", client_key, login)
            left = security.MAX_LOGIN_ATTEMPTS - security.login_throttle.failures(client_key)
            hint = f"Неверный пароль. Осталось попыток: {left}" if left > 0 else "Неверный пароль"
            return _fail(hint)

        security.login_throttle.reset(client_key)
        await audit.log_action(
            db,
            "admin.login",
            actor=audit.Actor(
                name=session.name, role=session.role, tg_id=session.tg_id, ip=security.client_ip(request)
            ),
            payload={"role": session.role},
        )
        await db.commit()

    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        security.COOKIE_NAME,
        security.issue_session(
            name=session.name, role=session.role, account_id=session.account_id, tg_id=session.tg_id
        ),
        httponly=True,
        samesite="lax",
        secure=security.cookie_secure(request),
        max_age=settings.admin_session_hours * 3600,
    )
    return response


@router.post("/logout")
async def logout_post(request: Request):
    deny_if_foreign(request)
    deny_if_cross_site(request)
    session = security.read_session(request.cookies.get(security.COOKIE_NAME))
    if session is not None:
        try:
            async with SessionMaker() as db:
                await audit.log_action(
                    db,
                    "admin.logout",
                    actor=audit.Actor(
                        name=session.name, role=session.role, tg_id=session.tg_id, ip=security.client_ip(request)
                    ),
                )
                await db.commit()
        except Exception as exc:  # noqa: BLE001 - выход не должен падать из-за журнала
            logger.warning("Не записал выход в журнал: %s", exc)

    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(security.COOKIE_NAME)
    return response


@router.get("/logout")
async def logout_page(request: Request):
    """GET-выход оставлен для ссылок в закладках, но сам сессию не рушит.

    Менять состояние по GET нельзя: достаточно открыть картинку с чужого сайта,
    чтобы человека разлогинило. Поэтому GET только показывает подтверждение.
    """
    deny_if_foreign(request)
    session = security.read_session(request.cookies.get(security.COOKIE_NAME))
    if session is None:
        return RedirectResponse("/admin/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "logout.html",
        {
            "session_active": True,
            "admin": session.as_dict(),
            "page": "logout",
            "page_title": "Выход",
            "nav_counts": None,
            "message": "",
            "error": "",
        },
    )
