"""Команда: учётные записи панели, роли и пароли.

Почему отдельный модуль и почему только владелец: эта страница раздаёт доступ
к деньгам и к данным клиентов. Ошибка здесь не «неудобство», а дыра, поэтому
весь раздел закрыт правом ``team.manage`` (есть только у владельца), а каждое
изменение попадает в журнал.

Что важнее удобства: панель не должна остаться без хозяина. Пока в системе
один активный владелец, его нельзя ни разжаловать, ни выключить — иначе
следующий вход в раздел «Команда» будет уже некому сделать.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import AdminAccount
from app.db.session import SessionMaker
from app.services import audit
from app.web import security, ui
from app.web.admin.common import flash_redirect, page, require

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()

#: Логин: только строчные латинские буквы, цифры, точка, дефис и подчёркивание.
#: Так его можно продиктовать голосом, вписать в конфиг и не спутать регистр.
LOGIN_RE = re.compile(r"^[a-z0-9._-]{3,32}$")

#: Пароль короче 12 символов для панели с деньгами — приглашение к подбору.
MIN_PASSWORD_LENGTH = 12


def _role_options() -> list[tuple[str, str, str]]:
    """Роли с пояснениями: (код, название, что роль умеет)."""
    return [(code, label, ui.ROLE_DESCRIPTIONS.get(code, "")) for code, label in ui.ROLES.items()]


def _parse_tg_id(raw: str) -> tuple[int | None, str]:
    """Telegram ID из формы. Пусто — это не ошибка: не у всех есть привязка."""
    text = (raw or "").strip().lstrip("@")
    if not text:
        return None, ""
    if not text.isdigit():
        return None, "Telegram ID — это число, например 123456789"
    return int(text), ""


async def _active_owners(db, exclude_id: int | None = None) -> int:  # noqa: ANN001
    """Сколько активных владельцев останется, если не считать указанного."""
    stmt = select(func.count(AdminAccount.id)).where(
        AdminAccount.role == ui.ROLE_OWNER,
        AdminAccount.is_active.is_(True),
    )
    if exclude_id is not None:
        stmt = stmt.where(AdminAccount.id != exclude_id)
    return int(await db.scalar(stmt) or 0)


@router.get("/team", response_class=HTMLResponse)
async def team_page(request: Request):
    """Список доступов, форма создания и памятка по безопасности панели."""
    auth = await require(request, "team.manage")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        accounts = list((await db.scalars(select(AdminAccount).order_by(AdminAccount.id))).all())
        owners = int(
            await db.scalar(
                select(func.count(AdminAccount.id)).where(
                    AdminAccount.role == ui.ROLE_OWNER,
                    AdminAccount.is_active.is_(True),
                )
            )
            or 0
        )

    return await page(
        request,
        "team.html",
        auth,
        title="Команда и роли",
        page="team",
        accounts=accounts,
        roles=_role_options(),
        active_owners=owners,
        # Кто смотрит страницу: в списке помечаем свою запись, чтобы владелец
        # не выключил себя по ошибке.
        auth_id=auth.account_id,
        # Пароль панели из .env в шаблон не передаём — только факт, что он задан:
        # это резервный вход владельца, если учётные записи потерялись.
        env_password_set=bool(settings.admin_panel_password),
        session_hours=settings.admin_session_hours,
        local_only=settings.admin_local_only,
    )


@router.post("/team")
async def team_create(
    request: Request,
    login: str = Form(""),
    display_name: str = Form(""),
    role: str = Form(""),
    password: str = Form(""),
    tg_id: str = Form(""),
):
    """Завести доступ. Пустой пароль — сгенерируем и покажем один раз."""
    auth = await require(request, "team.manage")
    if isinstance(auth, Response):
        return auth

    login = (login or "").strip().lower()
    if not LOGIN_RE.match(login):
        return flash_redirect(
            "/admin/team",
            error="Логин: 3–32 символа, только строчные латинские буквы, цифры, точка, дефис и подчёркивание",
        )
    display_name = (display_name or "").strip()[:64]
    if role not in ui.ROLES:
        return flash_redirect("/admin/team", error="Выбери роль из списка")

    password = (password or "").strip()
    generated = ""
    if password and len(password) < MIN_PASSWORD_LENGTH:
        return flash_redirect(
            "/admin/team",
            error=f"Пароль короче {MIN_PASSWORD_LENGTH} символов. Оставь поле пустым — сгенерируем надёжный.",
        )
    if not password:
        generated = password = security.new_password()

    tg_value, error = _parse_tg_id(tg_id)
    if error:
        return flash_redirect("/admin/team", error=error)

    async with SessionMaker() as db:
        existing = await db.scalar(select(AdminAccount.id).where(AdminAccount.login == login))
        if existing is not None:
            return flash_redirect("/admin/team", error=f"Логин {login} уже занят")

        account = AdminAccount(
            login=login,
            display_name=display_name or login,
            password_hash=security.hash_password(password),
            role=role,
            tg_id=tg_value,
            is_active=True,
        )
        db.add(account)
        await db.flush()
        # В журнал пишем только факт создания: пароль в журнале — это утечка,
        # которая переживёт смену пароля.
        await audit.log_action(
            db,
            "admin.team_created",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            payload={"account_id": account.id, "login": login, "role": role, "tg_id": tg_value},
        )
        await db.commit()

    if generated:
        return flash_redirect(
            "/admin/team",
            message=f"{login} создан. Пароль: {generated} — сохрани его сейчас, второй раз не покажем.",
        )
    return flash_redirect("/admin/team", message=f"Доступ для {login} создан")


@router.post("/team/{account_id}")
async def team_update(
    account_id: int,
    request: Request,
    display_name: str = Form(""),
    role: str = Form(""),
    is_active: str | None = Form(None),
    tg_id: str = Form(""),
    password: str = Form(""),
):
    """Изменить доступ: имя, роль, активность, Telegram ID и (при желании) пароль."""
    auth = await require(request, "team.manage")
    if isinstance(auth, Response):
        return auth

    if role not in ui.ROLES:
        return flash_redirect("/admin/team", error="Выбери роль из списка")
    # Поле активности может не прийти (так вызывают маршрут скрипты): тогда
    # оставляем как было. Иначе случайный POST молча выключил бы доступ.
    requested_active = None
    if is_active is not None:
        requested_active = str(is_active).strip().lower() in {"1", "on", "true", "yes", "да"}
    display_name = (display_name or "").strip()[:64]
    password = (password or "").strip()
    if password and len(password) < MIN_PASSWORD_LENGTH:
        return flash_redirect(
            "/admin/team",
            error=f"Пароль короче {MIN_PASSWORD_LENGTH} символов — оставь поле пустым, если менять не нужно",
        )
    tg_value, error = _parse_tg_id(tg_id)
    if error:
        return flash_redirect("/admin/team", error=error)

    async with SessionMaker() as db:
        account = await db.get(AdminAccount, account_id)
        if account is None:
            return flash_redirect("/admin/team", error="Учётная запись не найдена")

        new_active = account.is_active if requested_active is None else requested_active

        # Защита от потери хозяина: последнего активного владельца нельзя
        # ни разжаловать, ни выключить. Сменить ему имя или пароль — можно.
        loses_owner = account.role == ui.ROLE_OWNER and account.is_active and (
            role != ui.ROLE_OWNER or not new_active
        )
        if loses_owner and await _active_owners(db, exclude_id=account.id) == 0:
            return flash_redirect(
                "/admin/team",
                error=(
                    "Это единственный активный владелец. Сначала назначь второго владельца — "
                    "иначе панель останется без хозяина."
                ),
            )

        before = {
            "display_name": account.display_name,
            "role": account.role,
            "is_active": account.is_active,
            "tg_id": account.tg_id,
        }
        account.display_name = display_name or account.login
        account.role = role
        account.is_active = new_active
        account.tg_id = tg_value
        if password:
            account.password_hash = security.hash_password(password)
        after = {
            "display_name": account.display_name,
            "role": account.role,
            "is_active": account.is_active,
            "tg_id": account.tg_id,
            "password_changed": bool(password),
        }

        await audit.log_action(
            db,
            "admin.team_updated",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            payload={"account_id": account.id, "login": account.login, "before": before, "after": after},
        )
        await db.commit()

    tail = " Пароль обновлён." if password else ""
    return flash_redirect("/admin/team", message=f"Доступ {account.login} сохранён.{tail}")


@router.post("/team/{account_id}/password")
async def team_reset_password(account_id: int, request: Request):
    """Сбросить пароль: новый генерируем и показываем один раз.

    Так надёжнее, чем «придумай пароль»: владелец не выберет имя собаки,
    а забытый пароль всё равно придётся передавать человеку.
    """
    auth = await require(request, "team.manage")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        account = await db.get(AdminAccount, account_id)
        if account is None:
            return flash_redirect("/admin/team", error="Учётная запись не найдена")

        password = security.new_password()
        account.password_hash = security.hash_password(password)
        await audit.log_action(
            db,
            "admin.team_updated",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            payload={"account_id": account.id, "login": account.login, "action": "password_reset"},
        )
        await db.commit()
        login = account.login

    return flash_redirect(
        "/admin/team",
        message=f"Новый пароль для {login}: {password} — передай его владельцу доступа и не храни здесь.",
    )
