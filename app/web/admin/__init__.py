"""Админ-панель: сборка маршрутов.

Модуль разбит по смыслу, а не по размеру файла:

* :mod:`common`     — доступ по ролям, всплывающие сообщения, пагинация;
* :mod:`auth`       — вход, выход, страница входа;
* :mod:`dashboard`  — обзор: цифры дня, алерты, очередь, график;
* :mod:`orders`     — очередь заказов, подтверждение, отклонение, возвраты;
* :mod:`users`      — список клиентов и карточка клиента (заметки, метки);
* :mod:`finance`    — деньги: оборот, прибыль, каналы, выгрузки;
* :mod:`infra`      — ноды, алерты, журнал действий;
* :mod:`growth`     — рефералы, промокоды, тарифы, рассылки;
* :mod:`team`       — учётные записи команды и роли (только владелец);
* :mod:`exports`    — выгрузки CSV.

Правило: ни один маршрут не меняет данные без проверки роли и записи в журнал
действий. Это то, что делает панель пригодной для работы вдвоём-втроём.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.web.admin import auth, dashboard, exports, finance, growth, infra, orders, search, team, users

router = APIRouter(prefix="/admin")
router.include_router(auth.router)
router.include_router(search.router)
router.include_router(dashboard.router)
# `/admin` без слэша регистрируем на самом роутере: FastAPI запрещает пустой путь
# во вложенном роутере, а редирект на `/admin/` ломает закладки и привычный адрес.
router.add_api_route("", dashboard.dashboard, methods=["GET"], response_class=HTMLResponse, include_in_schema=False)
router.include_router(orders.router)
router.include_router(users.router)
router.include_router(finance.router)
router.include_router(infra.router)
router.include_router(growth.router)
router.include_router(team.router)
router.include_router(exports.router)

__all__ = ["router"]
