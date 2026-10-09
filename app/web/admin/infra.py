"""Инфраструктура: ноды, алерты и журнал действий.

Зачем одним модулем: это три взгляда на одно и то же — «что у нас работает»
(ноды), «что сломалось» (алерты) и «кто что менял» (журнал). Во время
инцидента модератор ходит по ним по кругу, поэтому страницы собраны рядом.

Правило модуля то же, что и во всей панели: сначала `require(...)`, потом
работа, потом `audit.log_action(...)` и только затем `commit`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select, update

from app.config import get_settings
from app.db.models import Alert, Event, Node, User
from app.db.session import SessionMaker
from app.panels.base import Inbound, PanelClient, PanelError, normalize_inbound_ids, parse_inbound_ids
from app.panels.registry import registry
from app.services import alerts as alerts_service
from app.services import audit
from app.web import ui
from app.web.admin.common import filters, flash_redirect, make_page, page, parse_page, require

logger = logging.getLogger(__name__)
router = APIRouter()

#: Вкладки алертов. «Открытые» по смыслу сервиса включают и взятые в работу —
#: иначе взятый алерт исчезает с глаз, а вместе с ним и проблема.
ALERT_TABS: tuple[tuple[str, str], ...] = (
    ("open", "Открытые"),
    ("ack", "В работе"),
    ("resolved", "Закрытые"),
    ("all", "Все"),
)

ALERT_FILTER_KEYS = ("status",)
AUDIT_FILTER_KEYS = ("actor", "kind", "date_from", "date_to")

#: Типы панелей, которые умеет собирать реестр. Опечатка в форме иначе
#: превратилась бы в «панель», к которой некуда подключаться.
PANEL_TYPES = ("xui", "fake")

#: Кэш инбаундов: панель — чужой сервер, и ждать её на каждый рендер нельзя.
#: Держим короткий таймаут и помним результат минуту; кнопка «Проверить ноды»
#: кэш сбрасывает, поэтому после явной проверки данные свежие.
INBOUNDS_TTL = 60.0
INBOUNDS_TIMEOUT = 1.2
#: code+url → (момент замера, инбаунды, ошибка). URL в ключе — чтобы после
#: правки адреса панели не показывать инбаунды старого сервера.
_inbounds_cache: dict[str, tuple[float, list[Inbound], str]] = {}


def _parse_date(value: str, *, end: bool = False) -> datetime | None:
    """Дата из формы (YYYY-MM-DD) → момент времени UTC.

    Для «по» берём конец дня — та же причина, что в очереди заказов: фильтр
    «с 1 по 1 октября» иначе показывает пусто.
    """
    if not value:
        return None
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
    moment = datetime.combine(day, datetime.max.time() if end else datetime.min.time())
    return moment.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------- ноды
async def _load_inbounds(panel: PanelClient) -> list[Inbound]:
    """Инбаунды панели с жёстким таймаутом: страница не ждёт сеть бесконечно.

    Берём **сырой** список, без фильтра по ``inbound_ids``: фильтр падает как
    раз тогда, когда настройки разошлись с панелью, и показать оператору
    реальные ID стало бы неоткуда — а именно они и нужны для починки.
    """
    # Сторонняя панель может не уметь сырой список — тогда фильтрованный
    # (базовый ``PanelClient.list_all_inbounds`` и так делегирует в него).
    lister = getattr(panel, "list_all_inbounds", None) or panel.list_inbounds
    return list(await asyncio.wait_for(lister(), timeout=INBOUNDS_TIMEOUT))


def _panel_error_text(exc: BaseException) -> str:
    """Человеческий текст ошибки панели вместо общего «панель не ответила».

    Таймаут на странице короткий (``INBOUNDS_TIMEOUT``), поэтому медленная, но
    живая панель выглядит мёртвой. Текст нужен, чтобы это было видно: раньше
    сюда попадало и «в панели не найдены инбаунды [3]», и оно же превращалось
    в «проверь адрес и токен панели».
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return f"панель не ответила за {INBOUNDS_TIMEOUT:g} с"
    if isinstance(exc, PanelError):
        return str(exc)
    return f"ошибка при обращении к панели: {exc}"


def node_ready(node: Node) -> bool:
    """Готова ли локация выдать клиенту рабочий профиль.

    Две независимые проверки: панель (``last_check_ok`` — авторизация, список
    инбаундов) и порт (``probe_verdict`` — TCP-соединение). Локация готова,
    когда живы обе. Проба ещё не запускалась — не считаем ни готовой, ни
    сломанной: «не измеряли» и «работает» — разные утверждения, и подменять
    одно другим значит повторить историю «сервер везде сообщает, что
    Нидерланды не работают».
    """
    from app.services import probe as probe_service

    if not node.is_active or not node.last_check_ok:
        return False
    verdict = probe_service.probe_verdict(node)
    return verdict in (probe_service.PROBE_OK, probe_service.PROBE_UNKNOWN)


def _probe_view(node: Node) -> dict:
    """Состояние пробы для шаблона: ``{state, text, ports}``.

    Обёртка над ``app.services.probe`` — шаблон получает готовые слова и не
    разбирает поля ноды сам. Именно из-за такого разбора «порт не пускает» и
    «проба не выполнена» выглядели в карточке одинаково.
    """
    from app.services import probe as probe_service

    state, text = probe_service.probe_state(node)
    return {"state": state, "text": text, "ports": probe_service.probe_ports(node)}


def _configured_ids(node: Node | None) -> list[int]:
    """Настроенные ID инбаундов: у ноды — из её строки, у основной — из .env."""
    if node is None:
        return list(get_settings().inbound_id_list)
    if (node.panel_type or "").lower() != "xui":
        # Заглушка (fake) игнорирует inbound_ids: сверять нечего, иначе на
        # странице появится ложное «нет ID 3» и совет править рабочие настройки.
        return []
    return parse_inbound_ids(node.inbound_ids)


async def _panel_views(panels: list[PanelClient], nodes: list[Node]) -> list[dict]:
    """Карточки панелей для страницы нод: подпись, адрес и список инбаундов.

    Инбаунды нужны, чтобы модератор видел, из чего собирается подписка
    («DE-Reality» или пусто) и какие настроенные ID в панели отсутствуют.
    Берём их из короткого кэша, а если кэша нет — одним параллельным запросом
    с таймаутом: одна медленная нода не должна задерживать отрисовку остальных.
    """
    now = time.monotonic()
    views: list[dict] = []
    pending: list[tuple[str, PanelClient, dict]] = []

    for index, panel in enumerate(panels):
        # Реестр отдаёт основную панель первой, дальше — активные ноды по
        # priority: тем же порядком подписываем карточки названиями из БД.
        node = nodes[index - 1] if 0 < index <= len(nodes) else None
        code = node.code if node is not None else "primary"
        title = (node.title if node is not None else "") or getattr(panel, "location_title", "") or "Основная панель"
        url = (node.panel_url if node is not None else "") or getattr(panel, "base_url", "")
        key = f"{code}|{url}"
        entry: dict = {
            "code": code, "title": title, "url": url, "node": node,
            "inbounds": [], "error": "", "fresh": False,
            "configured": _configured_ids(node), "missing": [],
        }
        views.append(entry)

        cached = _inbounds_cache.get(key)
        if cached and now - cached[0] < INBOUNDS_TTL:
            entry["inbounds"], entry["error"], entry["fresh"] = cached[1], cached[2], True
            continue
        pending.append((key, panel, entry))

    if pending:
        results = await asyncio.gather(
            *(_load_inbounds(panel) for _, panel, _ in pending), return_exceptions=True
        )
        for (key, _panel, entry), result in zip(pending, results):
            if isinstance(result, BaseException):
                reason = _panel_error_text(result)
                logger.info("Инбаунды панели %s не получены: %s", key, result)
                stale = _inbounds_cache.get(key)
                if stale:
                    # Старые данные лучше пустоты: конфиг собирается из этих
                    # инбаундов, и модератору важно видеть, из каких именно.
                    entry["inbounds"] = stale[1]
                    entry["error"] = f"{reason} — показаны данные прошлой проверки"
                else:
                    entry["error"] = reason
                continue
            entry["inbounds"] = result
            entry["fresh"] = True
            _inbounds_cache[key] = (time.monotonic(), result, "")

    # Какие настроенные ID в панели отсутствуют — это и есть причина алерта
    # «нет инбаундов»; показываем её рядом со списком, а не только в логе.
    for entry in views:
        if not entry["inbounds"]:
            continue
        actual = {item.id for item in entry["inbounds"]}
        entry["missing"] = [item for item in entry["configured"] if item not in actual]

    return views


@router.get("/nodes", response_class=HTMLResponse)
async def nodes_page(request: Request):
    auth = await require(request, "nodes.view")
    if isinstance(auth, Response):
        return auth

    edit_code = (request.query_params.get("edit") or "").strip()
    async with SessionMaker() as db:
        nodes = list((await db.scalars(select(Node).order_by(Node.priority, Node.id))).all())
        edit_node = next((node for node in nodes if node.code == edit_code), None) if edit_code else None
        panels = list(await registry.all_panels(db))

    # Сеть панелей — уже вне сессии БД: соединение не должно ждать чужие таймауты.
    views = await _panel_views(panels, [node for node in nodes if node.is_active])
    alive = sum(1 for node in nodes if node.is_active and node.last_check_ok)
    # «Готова» — не то же, что «отвечает»: панель может быть жива, а порт для
    # клиента закрыт. Плитка «живых локаций» считает именно готовность, иначе
    # на дашборде горело «3/3», пока клиенты не могли подключиться.
    ready = sum(1 for node in nodes if node.is_active and node_ready(node))

    return await page(
        request,
        "nodes.html",
        auth,
        title="Ноды",
        page="nodes",
        nodes=nodes,
        panels=views,
        edit_node=edit_node,
        nodes_active=sum(1 for node in nodes if node.is_active),
        nodes_alive=alive,
        nodes_ready=ready,
        # Состояние пробы одной функцией: шаблон не должен разбирать поля ноды
        # сам — именно из-за такого разбора «порт не пускает» и «проба не
        # выполнена» выглядели одинаково.
        node_probe=_probe_view,
        last_check_at=next((node.last_check_at for node in nodes if node.last_check_at), None),
        # Права передаём в контекст: в Jinja функции can() нет, а прятать кнопки
        # по роли в шаблоне — единственный способ не показывать их поддержке.
        can_nodes_act=ui.can(auth.role, "nodes.act"),
        can_nodes_secrets=ui.can(auth.role, "nodes.secrets"),
    )


@router.post("/nodes")
async def node_save(
    request: Request,
    code: str = Form(""),
    title: str = Form(""),
    country: str = Form(""),
    host: str = Form(""),
    panel_type: str = Form("xui"),
    panel_url: str = Form(""),
    panel_token: str = Form(""),
    inbound_ids: str = Form(""),
    priority: int = Form(100),
    is_active: str = Form("1"),
    channel: str = Form("main"),
    test_url: str = Form(""),
    sub_base: str = Form(""),
):
    """Добавить ноду или обновить существующую (upsert по коду).

    Код — ключ, по которому нода живёт в реестре панелей и в ссылке-подписке,
    поэтому правится не id, а код: форма одинаково работает и для новой страны,
    и для правки адреса у уже подключённой.
    """
    auth = await require(request, "nodes.act")
    if isinstance(auth, Response):
        return auth

    code = (code or "").strip().lower()
    title = (title or "").strip()
    kind = (panel_type or "").strip().lower() or "xui"
    url = (panel_url or "").strip()
    token = (panel_token or "").strip()
    node_host = (host or "").strip()
    node_sub_base = (sub_base or "").strip()
    wants_active = str(is_active).strip().lower() not in {"0", "false", "off", "no", ""}

    if not code:
        return flash_redirect("/admin/nodes", error="Укажи код ноды: латиницей, например jp")
    if not title:
        return flash_redirect("/admin/nodes", error="Укажи название — его увидит клиент в приложении")
    if kind not in PANEL_TYPES:
        return flash_redirect("/admin/nodes", error="Неизвестный тип панели: поддерживаются xui и fake")
    if kind == "xui" and not url:
        return flash_redirect("/admin/nodes", error="Укажи адрес панели: http://IP:2053/путь")
    if node_sub_base and not node_sub_base.startswith(("http://", "https://")):
        return flash_redirect(
            "/admin/nodes",
            error="Адрес сервиса подписок должен начинаться с http:// или https:// (например http://IP:2096/sub/)",
        )
    # Пустое поле = «все инбаунды панели», поэтому опечатку («3x», «3 ,»)
    # нельзя пропускать молча: иначе выдача расширится на все инбаунды, а
    # оператор будет уверен, что ограничил ноду одним.
    cleaned_ids, ids_problem = normalize_inbound_ids(inbound_ids)
    if ids_problem:
        return flash_redirect("/admin/nodes", error=ids_problem)
    # Активная нода без адреса подписок — это локация, которая молча пропадёт
    # из подписки клиента: бот не сможет забрать у панели готовые конфиги.
    # Пока нода черновик (выключена), сохранить её можно.
    if kind == "xui" and wants_active and not (node_sub_base or node_host):
        return flash_redirect(
            "/admin/nodes",
            error=(
                "У ноды не заполнены «Адрес сервера» и «Адрес сервиса подписок» — "
                "без них локация не попадёт в подписку. Заполни адрес или сними галочку «Включена»"
            ),
        )

    async with SessionMaker() as db:
        node = await db.scalar(select(Node).where(Node.code == code))
        created = node is None
        if node is None:
            node = Node(code=code)
            db.add(node)

        node.title = title[:64]
        node.country = (country or "").strip().upper()[:8]
        node.host = node_host[:128]
        node.panel_type = kind
        node.panel_url = url[:255]
        # Пустой токен в форме — «не менять». Иначе правка названия затирала бы
        # секрет: модератор видит в поле маску, а не сам токен (старая панель
        # на этом теряла доступ к ноде).
        if token:
            node.panel_token = token[:255]
        node.inbound_ids = cleaned_ids
        node.priority = int(priority or 100)
        node.is_active = wants_active
        # Адрес сервиса подписок: пусто — соберётся из host в модели
        # (``Node.subscription_base``), поэтому храним только явное значение.
        node.sub_base = node_sub_base[:255]
        # Канал: обычная локация, резервная или CDN. Незнакомое значение не
        # ломает подписку — считаем локацию обычной.
        node.channel = (channel or "").strip().lower()
        if node.channel not in {"main", "reserve", "cdn"}:
            node.channel = "main"
        # Свой test-URL канала: пусто — берётся общий из настроек.
        node.test_url = (test_url or "").strip()[:255]

        # Галочка «Включена» в форме — тот же выключатель, что кнопка в списке:
        # без закрытия алертов они висят в «Открытых» вечно, потому что
        # выключенную ноду никто не проверяет.
        if not node.is_active:
            await alerts_service.resolve_node_alerts(
                db, code, note="нода выключена", by=auth.name
            )

        # Клиент панели кэшируется по коду: без сброса подписка молча ходила бы
        # на старый адрес или со старым токеном.
        registry.invalidate(code)
        # Кэш инбаундов держится по «код|адрес», а правка могла поменять только
        # inbound_ids — тогда на странице остался бы старый расчёт «чего не
        # хватает», и оператор проверял бы по устаревшему списку.
        _inbounds_cache.clear()
        await audit.log_action(
            db,
            "admin.node_saved",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            payload={
                "code": code,
                "created": created,
                "is_active": node.is_active,
                "channel": node.channel,
                "panel_url": node.panel_url,
                "token_changed": bool(token),
            },
        )
        await db.commit()

    action = "добавлена" if created else "сохранена"
    note = "" if token else " · токен не менялся"
    return flash_redirect("/admin/nodes", message=f"Нода «{title}» {action}{note}")


@router.post("/nodes/{node_id}/toggle")
async def node_toggle(node_id: int, request: Request):
    auth = await require(request, "nodes.act")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        node = await db.get(Node, node_id)
        if node is None:
            return flash_redirect("/admin/nodes", error="Нода не найдена")
        node.is_active = not node.is_active
        state = "включена" if node.is_active else "выключена"
        title, code, active = node.title, node.code, node.is_active
        registry.invalidate(code)
        _inbounds_cache.clear()
        # Выключенную ноду никто не проверяет, значит автозакрытие алертов для
        # неё уже не сработает: «нет инбаундов» и «порт не пускает клиента»
        # остались бы в «Открытых» навсегда.
        if not active:
            await alerts_service.resolve_node_alerts(
                db, code, note="нода выключена", by=auth.name
            )
        await audit.log_action(
            db,
            "admin.node_toggle",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            payload={"node_id": node_id, "code": code, "is_active": active},
        )
        await db.commit()

    return flash_redirect("/admin/nodes", message=f"Нода «{title}» {state}")


@router.post("/nodes/{node_id}/delete")
async def node_delete(node_id: int, request: Request):
    auth = await require(request, "nodes.act")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        node = await db.get(Node, node_id)
        if node is None:
            return flash_redirect("/admin/nodes", error="Нода не найдена")
        title, code = node.title, node.code
        # Открытые алерты удалённой ноды закрываем сами: иначе «нет инбаундов»
        # и «порт не пускает клиента» висят в «Открытых» вечно, и настоящие
        # проблемы тонут в шуме. Закрываем все отпечатки ноды, а не только
        # доступность — у каждой проблемы свой отпечаток.
        await alerts_service.resolve_node_alerts(db, code, note="нода удалена", by=auth.name)
        # Ссылку на ноду снимаем до удаления строки: внешний ключ объявлен без
        # ON DELETE, и на PostgreSQL (о котором говорит app/db/session.py)
        # удаление ноды с алертом упало бы с IntegrityError.
        await db.execute(update(Alert).where(Alert.node_id == node.id).values(node_id=None))
        await db.delete(node)
        registry.invalidate(code)
        _inbounds_cache.clear()
        await audit.log_action(
            db,
            "admin.node_deleted",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            payload={"node_id": node_id, "code": code, "title": title},
        )
        await db.commit()

    return flash_redirect("/admin/nodes", message=f"Нода «{title}» удалена")


@router.post("/nodes/check")
async def nodes_check(request: Request):
    """Проверить основную панель и активные ноды, обновить алерты.

    Пары «нода или None (основная панель) + клиент панели» — тот же контракт,
    что у фоновой проверки: результат одинаково попадает в `Node.last_check_*`
    и в алерты, поэтому страница показывает свежее состояние без ожидания.
    """
    auth = await require(request, "nodes.act")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        nodes = list(
            (await db.scalars(select(Node).where(Node.is_active.is_(True)).order_by(Node.priority, Node.id))).all()
        )
        pairs: list[tuple[Node | None, PanelClient]] = [(None, registry.primary())]
        pairs += [(node, registry.for_node(node)) for node in nodes]

        try:
            results = await alerts_service.check_nodes(db, pairs)
        except Exception as exc:  # noqa: BLE001 - чужая панель может ответить чем угодно
            logger.error("Проверка нод не удалась: %s", exc)
            await db.rollback()
            return flash_redirect("/admin/nodes", error=f"Проверка не удалась: {exc}")

        # «Живая» — та, что готова выдать конфиг: панель может ответить и при
        # этом не найти настроенные инбаунды (тогда клиенты локацию не получат).
        alive = sum(1 for entry in results if entry.get("ready", entry["ok"]))
        failed = [entry["code"] for entry in results if not entry.get("ready", entry["ok"])]
        await audit.log_action(
            db,
            "admin.node_check",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            payload={"total": len(results), "alive": alive, "failed": failed[:10]},
        )
        await db.commit()

    # Проверка обновила состояние панелей — старый список инбаундов неактуален.
    _inbounds_cache.clear()

    if alive == 0:
        return flash_redirect(
            "/admin/nodes",
            error=f"Ни одна панель не готова выдать конфиг (проверено {len(results)})",
        )
    if failed:
        return flash_redirect(
            "/admin/nodes",
            message=f"Готовы {alive} из {len(results)}. Проблемы: {', '.join(failed[:5])}",
        )
    return flash_redirect("/admin/nodes", message=f"Все панели готовы: {alive} из {len(results)}")


# ----------------------------------------------------------------- алерты
@router.get("/alerts", response_class=HTMLResponse)
async def alerts_page(request: Request):
    auth = await require(request, "alerts.view")
    if isinstance(auth, Response):
        return auth

    current = filters(request, ALERT_FILTER_KEYS)
    status = current["status"] or "open"
    if status not in {code for code, _ in ALERT_TABS}:
        status = "open"
    page_no, per_page = parse_page(request)

    async with SessionMaker() as db:
        items = await alerts_service.list_alerts(db, status=status, limit=per_page, offset=(page_no - 1) * per_page)
        total = await alerts_service.count_alerts(db, status)
        counts = {code: await alerts_service.count_alerts(db, code) for code, _ in ALERT_TABS}
        snapshot = await alerts_service.summary(db)
        # Клиентов для алертов (например, «поступление без заказа») подтягиваем
        # одним запросом на страницу, а не по строке.
        user_ids = {alert.user_id for alert in items if alert.user_id}
        users = (
            {user.id: user for user in await db.scalars(select(User).where(User.id.in_(user_ids)))}
            if user_ids
            else {}
        )

    return await page(
        request,
        "alerts.html",
        auth,
        title="Алерты",
        page="alerts",
        items=items,
        users=users,
        current=current,
        status=status,
        tabs=ALERT_TABS,
        counts=counts,
        summary=snapshot,
        page_data=make_page(items, total, page_no, per_page),
        can_alerts_act=ui.can(auth.role, "alerts.act"),
    )


@router.post("/alerts/{alert_id}/ack")
async def alert_ack(alert_id: int, request: Request):
    """Взять алерт в работу: проблема остаётся открытой, но видно, что ею заняты."""
    auth = await require(request, "alerts.act")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        alert = await db.get(Alert, alert_id)
        if alert is None:
            return flash_redirect("/admin/alerts", error="Алерт не найден")
        if alert.status == alerts_service.STATUS_RESOLVED:
            return flash_redirect("/admin/alerts", error="Алерт уже закрыт")
        await alerts_service.ack_alert(db, alert, by=auth.name)
        await audit.log_action(
            db,
            "admin.alert_ack",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            payload={"alert_id": alert_id, "kind": alert.kind},
        )
        await db.commit()
        title = alert.title

    return flash_redirect("/admin/alerts", message=f"Алерт взят в работу: {title}")


@router.post("/alerts/{alert_id}/resolve-note")
async def alert_resolve_note(alert_id: int, request: Request, note: str = Form("")):
    """Закрыть алерт с пояснением.

    Путь отличается от `/alerts/{id}/resolve` из обзора: там закрытие в один
    клик, здесь — с причиной, и два одинаковых маршрута конфликтовали бы.
    """
    auth = await require(request, "alerts.act")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        alert = await db.get(Alert, alert_id)
        if alert is None:
            return flash_redirect("/admin/alerts", error="Алерт не найден")
        await alerts_service.resolve_alert(db, alert, by=auth.name, note=note)
        await audit.log_action(
            db,
            "admin.alert_resolved",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            payload={
                "alert_id": alert_id,
                "kind": alert.kind,
                "note": note,
                "repeat_count": int(alert.repeat_count or 1),
            },
        )
        await db.commit()
        title = alert.title

    return flash_redirect("/admin/alerts", message=f"Алерт закрыт: {title}")


# ----------------------------------------------------------------- журнал
def _audit_statement(current: dict[str, str]):  # noqa: ANN202 - SQLAlchemy expression
    """Условия журнала: только действия команды (kind с префиксом ``admin.``).

    Своя выборка вместо `audit.recent_actions`: там нет фильтра по датам, а
    разбор инцидента начинается именно с «что было вчера вечером».
    """
    stmt = select(Event).where(Event.kind.like(f"{audit.ACTION_PREFIX}%"))
    if current["actor"]:
        stmt = stmt.where(Event.actor_name == current["actor"])
    if current["kind"]:
        stmt = stmt.where(Event.kind == current["kind"])
    date_from = _parse_date(current["date_from"])
    date_to = _parse_date(current["date_to"], end=True)
    if date_from:
        stmt = stmt.where(Event.created_at >= date_from)
    if date_to:
        stmt = stmt.where(Event.created_at <= date_to)
    return stmt


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request):
    auth = await require(request, "audit.view")
    if isinstance(auth, Response):
        return auth

    current = filters(request, AUDIT_FILTER_KEYS)
    page_no, per_page = parse_page(request)

    async with SessionMaker() as db:
        stmt = _audit_statement(current)
        total = int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        events = list(
            (await db.scalars(stmt.order_by(Event.id.desc()).limit(per_page).offset((page_no - 1) * per_page))).all()
        )
        # Список имён для фильтра — из самих событий: команда меняется, а
        # справочник в коде пришлось бы править руками.
        actors = [
            name
            for name in (
                await db.scalars(
                    select(Event.actor_name)
                    .where(Event.kind.like(f"{audit.ACTION_PREFIX}%"), Event.actor_name.is_not(None))
                    .group_by(Event.actor_name)
                    .order_by(Event.actor_name)
                )
            ).all()
            if name
        ]
        user_ids = {event.user_id for event in events if event.user_id}
        users = (
            {user.id: user for user in await db.scalars(select(User).where(User.id.in_(user_ids)))}
            if user_ids
            else {}
        )

    return await page(
        request,
        "audit.html",
        auth,
        title="Журнал действий",
        page="audit",
        events=events,
        users=users,
        actors=actors,
        current=current,
        actions=ui.ADMIN_ACTION_LABELS,
        page_data=make_page(events, total, page_no, per_page),
    )


# ------------------------------------------------- компенсация простоя
@router.get("/downtime", response_class=HTMLResponse)
async def downtime_page(request: Request):
    """Компенсация простоя: открытый период, история и ручное начисление.

    Почему в панели, а не только командой бота: это деньги клиентов, и решать
    по ним удобнее там же, где смотрят подписки и алерты.
    """
    from app.services import downtime as downtime_service

    auth = await require(request, "downtime.manage")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        opened = await downtime_service.current(db)
        history = await downtime_service.recent(db, 12)

    return await page(
        request,
        "downtime.html",
        auth,
        title="Компенсация простоя",
        page="downtime",
        opened=opened,
        history=history,
        max_days=downtime_service.MAX_GRANT_DAYS,
    )


@router.post("/downtime/start")
async def downtime_start(request: Request, note: str = Form("")):
    """Открыть период простоя: у абонентов не работает связь."""
    from app.services import downtime as downtime_service

    auth = await require(request, "downtime.manage")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        period, created = await downtime_service.start(
            db, note=note, actor=f"web:{auth.name}"
        )
        await audit.log_action(
            db,
            "admin.downtime_start",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id, source="web"),
            payload={"id": period.id, "created": created, "note": period.note},
        )
        await db.commit()
        started = period.started_at

    if not created:
        return flash_redirect(
            "/admin/downtime",
            message=f"Период уже открыт с {started:%d.%m %H:%M} UTC — закрывать его же",
        )
    return flash_redirect("/admin/downtime", message=f"Простой открыт ({started:%d.%m %H:%M} UTC)")


@router.post("/downtime/end")
async def downtime_end(request: Request, days: str = Form("")):
    """Закрыть период и начислить компенсацию всем активным подпискам."""
    from app.services import downtime as downtime_service
    from app.services import subscriptions as subscriptions_service

    auth = await require(request, "downtime.manage")
    if isinstance(auth, Response):
        return auth

    explicit = int(days) if (days or "").strip().isdigit() else None
    async with SessionMaker() as db:
        panels = await subscriptions_service.all_user_panels(db)
        result = await downtime_service.finish(
            db,
            actor=f"web:{auth.name}",
            days=explicit,
            panels=panels,
        )
        await audit.log_action(
            db,
            "admin.downtime_end",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id, source="web"),
            payload={"days": result.days, "users": result.users, "ok": result.ok},
        )
        await db.commit()

    if not result.ok:
        return flash_redirect("/admin/downtime", error=result.as_text())
    note = result.as_text()
    if result.days > 0:
        # Из панели сообщения клиентам не уходят: их отправляет бот, чтобы
        # рассылка шла через живого Bot и попадала в отчёт о доставке.
        note += " · сообщи клиентам рассылкой из бота (/broadcast)"
    return flash_redirect("/admin/downtime", message=note)


@router.post("/downtime/grant")
async def downtime_grant(
    request: Request,
    days: int = Form(0),
    note: str = Form(""),
):
    """Начислить дни вручную, без периода: разовая компенсация."""
    from app.services import downtime as downtime_service
    from app.services import subscriptions as subscriptions_service

    auth = await require(request, "downtime.manage")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        panels = await subscriptions_service.all_user_panels(db)
        result = await downtime_service.grant(
            db,
            days,
            note=note,
            actor=f"web:{auth.name}",
            panels=panels,
        )
        await audit.log_action(
            db,
            "admin.downtime_grant",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id, source="web"),
            payload={"days": result.days, "users": result.users, "ok": result.ok, "note": note},
        )
        await db.commit()

    if not result.ok:
        return flash_redirect("/admin/downtime", error=result.as_text())
    return flash_redirect("/admin/downtime", message=result.as_text())
