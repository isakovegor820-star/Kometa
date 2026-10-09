"""Общие помощники админ-панели: доступ по ролям, всплывающие сообщения, страницы.

Почему отдельный модуль: у панели десятки маршрутов, и каждый должен одинаково
проверять доступ, одинаково показывать результат действия и одинаково считать
бейджи в меню. Одна ошибка здесь = дыра в доступе, поэтому логика собрана в
одном месте и покрыта тестами.
"""

from __future__ import annotations

import base64
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Order
from app.db.session import SessionMaker
from app.services import alerts as alerts_service
from app.web import security, ui
from app.web.security import Session
from app.web.templating import templates

logger = logging.getLogger(__name__)
settings = get_settings()

def ssh_hint() -> str:
    """Подсказка при отказе по IP: панель доступна только через туннель.

    Порт и схему берём из настроек: раньше в тексте был зашит 8090, и владелец
    шёл туннелем на порт, которого на сервере уже нет.
    """
    settings = get_settings()
    scheme = "https" if (settings.web_ssl_cert and settings.web_ssl_key) else "http"
    port = settings.web_port
    return (
        "Админ-панель доступна только с localhost. Открой SSH-туннель: "
        f"ssh -N -L {port}:127.0.0.1:{port} root@IP — и заходи на {scheme}://127.0.0.1:{port}/admin. "
        "Либо добавь свой IP в ADMIN_ALLOWED_IPS."
    )


#: Совместимость: раньше это была константа.
SSH_HINT = ssh_hint()

FLASH_COOKIE = "kometa_flash"
FLASH_TTL = 30

#: Заголовок, которым панель помечает запросы из JS (тосты и обновление списка
#: без перезагрузки страницы). Обработчики про него не знают: флаг читает
#: flash_redirect, поэтому менять десятки маршрутов не пришлось.
AJAX_HEADER = "x-panel-ajax"
_ajax_request: ContextVar[bool] = ContextVar("panel_ajax", default=False)


def set_ajax(value: bool) -> None:
    """Пометить текущий запрос как ajax (вызывает middleware веб-слоя)."""
    _ajax_request.set(bool(value))


def is_ajax() -> bool:
    return _ajax_request.get()

#: Страницы пагинации: меньше 25 строк модератор листает вслепую, больше 200 — тормозит.
PER_PAGE_CHOICES = (25, 50, 100, 200)
DEFAULT_PER_PAGE = 50


@dataclass(slots=True)
class Page:
    """Страница списка: строки, всего записей и ссылки на соседние страницы."""

    items: list[Any]
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def first_index(self) -> int:
        return 0 if not self.total else (self.page - 1) * self.per_page + 1

    @property
    def last_index(self) -> int:
        return min(self.total, self.page * self.per_page)

    def window(self, size: int = 2) -> list[int | None]:
        return ui.page_range(self.page, self.pages, size)


# --------------------------------------------------------------------- доступ
def deny_if_foreign(request: Request) -> None:
    """Закрыть панель от чужих IP.

    Без домена и HTTPS панель висит на том же порту, что ссылки-подписки,
    поэтому по умолчанию пускаем только с localhost: владелец ходит через
    SSH-туннель, а пароль не улетает в открытый интернет.
    """
    if security.local_only_ok(request):
        return
    logger.warning("Отказ в доступе к админ-панели с IP %s", security.client_ip(request))
    raise HTTPException(status_code=403, detail=ssh_hint())


def deny_if_cross_site(request: Request) -> None:
    """Небезопасные методы — только со своего домена (вторая линия после SameSite)."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if security.same_origin(request):
        return
    logger.warning(
        "Отклонён POST в админку с чужого origin: %s", request.headers.get("origin") or request.headers.get("referer")
    )
    raise HTTPException(status_code=403, detail="Запрос пришёл с другого сайта. Обнови страницу и повтори.")


async def require(
    request: Request,
    capability: str | None = None,
) -> Session | Response:
    """Пропустить в панель или вернуть ответ-отказ.

    Использование в маршруте::

        auth = await require(request, "orders.view")
        if isinstance(auth, Response):
            return auth
    """
    deny_if_foreign(request)
    deny_if_cross_site(request)

    async with SessionMaker() as db:  # type: AsyncSession
        if not await security.panel_available(db):
            return RedirectResponse("/admin/login", status_code=303)

    session = security.read_session(request.cookies.get(security.COOKIE_NAME))
    if session is None:
        return RedirectResponse("/admin/login", status_code=303)

    if capability and not ui.can(session.role, capability):
        return forbidden(request, session, capability)
    return session


def forbidden(request: Request, session: Session, capability: str = "") -> HTMLResponse:
    """403 с человеческим объяснением: чего не хватает роли."""
    html = templates.TemplateResponse(
        request,
        "forbidden.html",
        {
            "session_active": True,
            "admin": session.as_dict(),
            "caps": ui.capabilities(session.role),
            "cmdk_sections": ui.cmd_sections(ui.capabilities(session.role)),
            "page_title": "Нет доступа",
            "page": "forbidden",
            "nav_counts": NavCounts(0, 0),
            "capability": capability,
            "message": "",
            "error": "",
        },
        status_code=403,
    )
    return html


# ------------------------------------------------------------- уведомления
@dataclass(slots=True)
class NavCounts:
    pending: int = 0
    alerts: int = 0


async def nav_counts() -> NavCounts:
    """Числа для бейджей в меню: очередь заказов и алерты."""
    try:
        async with SessionMaker() as db:  # type: AsyncSession
            pending = int(await db.scalar(select(func.count(Order.id)).where(Order.status == "pending")) or 0)
            summary = await alerts_service.summary(db)
        return NavCounts(pending=pending, alerts=summary.attention)
    except Exception as exc:  # noqa: BLE001 - бейдж не должен ронять страницу
        logger.warning("Не смог посчитать бейджи меню: %s", exc)
        return NavCounts()


def flash_redirect(
    url: str,
    *,
    message: str = "",
    error: str = "",
    query: bool = False,
) -> RedirectResponse:
    """Redirect с сообщением в короткой cookie.

    Почему не в query-строке: текст ошибки с «&» или «#» рвётся, а внутренние
    детали исключений оседают в истории браузера и в логах прокси.

    :param query: положить сообщение в query-строку. Нужно там, где текст
        должен быть виден в логе редиректов (например, кнопка проверки выписки
        в тестах и в отладке): cookie такого не показывает.
    """
    # Запрос из JS: вместо редиректа отдаём результат словарём — панель
    # показывает тост и обновляет список, не перезагружая страницу.
    if is_ajax():
        return JSONResponse(
            {
                "ok": not error,
                "message": message or "",
                "error": error or "",
            }
        )

    if query and (message or error):
        from urllib.parse import quote

        params = []
        if message:
            params.append(f"message={quote(message)}")
        if error:
            params.append(f"error={quote(error)}")
        response = RedirectResponse(f"{url}?{'&'.join(params)}", status_code=303)
        return response

    response = RedirectResponse(url, status_code=303)
    if message or error:
        payload = base64.urlsafe_b64encode(json.dumps({"m": message, "e": error}).encode()).decode()
        response.set_cookie(FLASH_COOKIE, payload, max_age=FLASH_TTL, path="/admin", httponly=True, samesite="lax")
    return response


def _read_flash(request: Request) -> tuple[str, str]:
    """Прочитать и «погасить» сообщение из cookie (cookie удаляется в ответе)."""
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return "", ""
    try:
        padded = raw + "=" * (-len(raw) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded).decode())
        return str(data.get("m") or ""), str(data.get("e") or "")
    except Exception:  # noqa: BLE001 - битая cookie не должна ломать страницу
        return "", ""


async def page(
    request: Request,
    name: str,
    session: Session,
    *,
    status_code: int = 200,
    title: str = "",
    **context: Any,
) -> HTMLResponse:
    """Отрисовать страницу панели с общим контекстом.

    Дополнительно подтягивает сообщения из cookie (или из query — для ссылок,
    которые кто-то сохранил в закладки) и числа для меню.
    """
    context.setdefault("session_active", True)
    context.setdefault("admin", session.as_dict())
    # Права текущей роли: шаблоны не показывают кнопку, которую сервер всё
    # равно отклонит. Меню и действия берут права отсюда, а не из роли напрямую.
    context.setdefault("caps", ui.capabilities(session.role))
    # Разделы для командной палитры (⌘K) — из тех же прав.
    context.setdefault("cmdk_sections", ui.cmd_sections(context["caps"]))
    context.setdefault("page", "")
    context.setdefault("page_title", title or context.get("page_title", "Панель"))
    if "nav_counts" not in context:
        context["nav_counts"] = await nav_counts()

    query_message = request.query_params.get("message", "")
    query_error = request.query_params.get("error", "")
    flash_message, flash_error = _read_flash(request)
    context.setdefault("message", query_message or flash_message)
    context.setdefault("error", query_error or flash_error)
    context.setdefault("query", dict(request.query_params))

    response = templates.TemplateResponse(request, name, context, status_code=status_code)
    if flash_message or flash_error:
        response.delete_cookie(FLASH_COOKIE, path="/admin")
    return response


# ---------------------------------------------------------------- пагинация
def parse_page(request: Request, *, default_per_page: int = DEFAULT_PER_PAGE) -> tuple[int, int]:
    """Номер страницы и размер страницы из query-строки (с разумными границами)."""
    try:
        page_no = int(request.query_params.get("page", "1"))
    except ValueError:
        page_no = 1
    try:
        per_page = int(request.query_params.get("per_page", str(default_per_page)))
    except ValueError:
        per_page = default_per_page
    if per_page not in PER_PAGE_CHOICES:
        per_page = default_per_page
    return max(1, page_no), per_page


def make_page(items: list[Any], total: int, page_no: int, per_page: int) -> Page:
    return Page(items=items, total=int(total or 0), page=page_no, per_page=per_page)


def filters(request: Request, keys: tuple[str, ...]) -> dict[str, str]:
    """Текущие значения фильтров — чтобы форма и ссылки пагинации их сохраняли."""
    return {key: (request.query_params.get(key) or "").strip() for key in keys}


# ------------------------------------------------------------ уведомления бота
async def notify(request: Request, tg_id: int, text: str) -> bool:
    """Написать клиенту в Telegram. False — бот недоступен или клиент закрыл чат."""
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        return False
    # У анонимизированного клиента tg_id синтетический и отрицательный:
    # отправка всё равно не сработает, но лучше не тратить запрос к Telegram.
    if int(tg_id or 0) <= 0:
        logger.info("Сообщение клиенту не отправлено: персональные данные удалены")
        return False
    try:
        await bot.send_message(tg_id, text, disable_web_page_preview=True)
        return True
    except Exception as exc:  # noqa: BLE001 - клиент мог заблокировать бота
        logger.warning("Не смог отправить сообщение %s: %s", tg_id, exc)
        return False


__all__ = [
    "Page",
    "NavCounts",
    "require",
    "page",
    "forbidden",
    "flash_redirect",
    "notify",
    "parse_page",
    "deny_if_foreign",
    "deny_if_cross_site",
    "make_page",
    "filters",
    "nav_counts",
    "PER_PAGE_CHOICES",
    "SSH_HINT",
    "ssh_hint",
    "settings",
]
