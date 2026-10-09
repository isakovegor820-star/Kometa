"""Алерты панели: проблемы видны в интерфейсе, а не только в Telegram.

Зачем: сообщение в чат легко пропустить, а «нода недоступна» или «пришло
199.13 ₽, заказ не найден» — это работа, которую нельзя терять. Алерты
дедуплицируются по отпечатку: пока открыт алерт ``node:de``, повторные
проверки увеличивают счётчик, а не создают сотню строк.
"""

from __future__ import annotations

import json

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Alert, Node
from app.panels.base import PanelClient, PanelError, PanelInboundMissing

STATUS_OPEN = "open"
STATUS_ACK = "ack"
STATUS_RESOLVED = "resolved"

SEV_ERR = "err"
SEV_WARN = "warn"
SEV_INFO = "info"

#: Справочник видов алертов: (код, важность, заголовок).
KINDS: dict[str, tuple[str, str]] = {
    "node_down": (SEV_ERR, "Нода не отвечает"),
    "node_degraded": (SEV_WARN, "Нода отвечает с ошибкой"),
    "node_probe_failed": (SEV_ERR, "Нода не пускает клиента"),
    #: Часть портов закрыта, но подключиться есть куда. Отдельный вид, потому
    #: что действие оператора другое: не «спасай ноду», а «открой порт в фаерволе»,
    #: и пугать клиента этим не нужно — у него рабочий профиль есть.
    "node_probe_degraded": (SEV_WARN, "Часть портов ноды закрыта"),
    "payment_unmatched": (SEV_WARN, "Поступление без заказа"),
    "statement_error": (SEV_ERR, "Не читается выписка"),
    "panel_error": (SEV_WARN, "Ошибка панели"),
    "errors_spike": (SEV_WARN, "Всплеск ошибок"),
    "sharing_suspect": (SEV_INFO, "Подозрение на шеринг"),
    "sales_disabled": (SEV_INFO, "Продажи выключены"),
}


@dataclass(slots=True)
class AlertCounts:
    open_err: int
    open_warn: int
    open_info: int
    ack: int

    @property
    def open_total(self) -> int:
        return self.open_err + self.open_warn + self.open_info

    @property
    def attention(self) -> int:
        """Сколько алертов реально требуют человека (для бейджа в меню)."""
        return self.open_err + self.open_warn


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def raise_alert(
    session: AsyncSession,
    kind: str,
    title: str = "",
    *,
    severity: str | None = None,
    message: str | None = None,
    fingerprint: str = "",
    node_id: int | None = None,
    user_id: int | None = None,
    reopen_ack: bool = True,
) -> Alert:
    """Поднять алерт. Повтор с тем же отпечатком не плодит строк, а обновляет её.

    :param reopen_ack: переоткрывать ли алерт, который человек взял в работу.
        Для одноразовых событий (платёж без заказа) это осмысленно, для
        повторяющихся проверок состояния — нет: карточка «в работе» теряла
        владельца через пять минут после того, как её взяли.
    """
    default_sev, default_title = KINDS.get(kind, (SEV_WARN, kind))
    fingerprint = fingerprint or kind
    alert = await session.scalar(
        select(Alert)
        .where(Alert.fingerprint == fingerprint, Alert.status != STATUS_RESOLVED)
        .order_by(Alert.id.desc())
        .limit(1)
    )
    if alert is not None:
        alert.repeat_count = int(alert.repeat_count or 1) + 1
        alert.last_seen_at = _now()
        if reopen_ack and alert.status == STATUS_ACK:
            alert.status = STATUS_OPEN
        if message:
            alert.message = message
        return alert

    alert = Alert(
        kind=kind,
        severity=severity or default_sev,
        title=(title or default_title)[:160],
        message=message,
        fingerprint=fingerprint[:120],
        node_id=node_id,
        user_id=user_id,
        status=STATUS_OPEN,
    )
    session.add(alert)
    await session.flush()
    return alert


async def resolve_by_fingerprint(
    session: AsyncSession,
    fingerprint: str,
    *,
    note: str = "проблема ушла сама",
    by: str = "система",
) -> Alert | None:
    """Закрыть алерт, если проблема исчезла (например, нода снова отвечает)."""
    alert = await session.scalar(
        select(Alert)
        .where(Alert.fingerprint == fingerprint, Alert.status != STATUS_RESOLVED)
        .order_by(Alert.id.desc())
        .limit(1)
    )
    if alert is None:
        return None
    alert.status = STATUS_RESOLVED
    alert.resolved_at = _now()
    alert.resolved_by = by
    alert.resolve_note = note
    return alert


async def ack_alert(session: AsyncSession, alert: Alert, *, by: str) -> Alert:
    alert.status = STATUS_ACK
    alert.resolved_by = by
    return alert


async def resolve_alert(session: AsyncSession, alert: Alert, *, by: str, note: str = "") -> Alert:
    alert.status = STATUS_RESOLVED
    alert.resolved_at = _now()
    alert.resolved_by = by
    alert.resolve_note = note or None
    return alert


async def list_alerts(
    session: AsyncSession,
    *,
    status: str = "open",
    limit: int = 100,
    offset: int = 0,
) -> list[Alert]:
    stmt = select(Alert).order_by(Alert.last_seen_at.desc()).limit(limit).offset(offset)
    if status == "open":
        stmt = stmt.where(Alert.status.in_([STATUS_OPEN, STATUS_ACK]))
    elif status != "all":
        stmt = stmt.where(Alert.status == status)
    return list((await session.scalars(stmt)).all())


async def count_alerts(session: AsyncSession, status: str = "open") -> int:
    stmt = select(func.count(Alert.id))
    if status == "open":
        stmt = stmt.where(Alert.status.in_([STATUS_OPEN, STATUS_ACK]))
    elif status != "all":
        stmt = stmt.where(Alert.status == status)
    return int(await session.scalar(stmt) or 0)


async def summary(session: AsyncSession) -> AlertCounts:
    rows = (
        await session.execute(
            select(Alert.status, Alert.severity, func.count(Alert.id)).group_by(Alert.status, Alert.severity)
        )
    ).all()
    counts = {"err": 0, "warn": 0, "info": 0, "ack": 0}
    for status, severity, total in rows:
        if status == STATUS_RESOLVED:
            continue
        if status == STATUS_ACK:
            counts["ack"] += int(total)
        elif severity in counts:
            counts[severity] += int(total)
    return AlertCounts(
        open_err=counts["err"], open_warn=counts["warn"], open_info=counts["info"], ack=counts["ack"]
    )


# ------------------------------------------------------------------ проверки
#: Отпечатки, которые относятся к ноде целиком. Основной — доступность,
#: дальше по видам проблем: инбаунды, панель, проба «глазами клиента».
NODE_FINGERPRINT_SUFFIXES: tuple[str, ...] = ("", ":inbounds", ":panel", ":probe")


def node_fingerprints(code: str) -> list[str]:
    """Все отпечатки алертов ноды: доступность, инбаунды, панель, проба."""
    return [f"node:{code}{suffix}" for suffix in NODE_FINGERPRINT_SUFFIXES]


async def resolve_node_alerts(
    session: AsyncSession,
    code: str,
    *,
    note: str = "нода убрана из проверки",
    by: str = "система",
) -> int:
    """Закрыть все алерты ноды. Возвращает число закрытых.

    Нужно при удалении и выключении ноды: автозакрытие срабатывает только на
    успешной проверке, а выключенную ноду никто не проверяет — алерты
    «нет инбаундов» и «порт не пускает клиента» оставались в «Открытых»
    навсегда, и закрыть их штатной кнопкой было нельзя.
    """
    closed = 0
    for fingerprint in node_fingerprints(code):
        if await resolve_by_fingerprint(session, fingerprint, note=note, by=by) is not None:
            closed += 1
    return closed


async def _panel_health(panel: PanelClient) -> tuple[bool, str]:
    """``(отвечает, текст ошибки)`` — с запасным вариантом для чужих панелей.

    Реальные клиенты наследуют ``PanelClient.check_health``; заглушки и
    сторонние реализации могут уметь только ``health()``.
    """
    checker = getattr(panel, "check_health", None)
    if callable(checker):
        ok, error = await checker()
        return bool(ok), str(error or "")
    try:
        return bool(await panel.health()), ""
    except Exception as exc:  # noqa: BLE001 - health чужой панели может упасть
        return False, str(exc)


async def check_nodes(session: AsyncSession, panels: list[tuple[Node | None, PanelClient]]) -> list[dict]:
    """Проверить доступность нод и завести/закрыть алерты.

    Пишем ``Node.last_check_at``/``last_check_ok``/``last_check_error`` — на
    дашборде видно, когда ноду проверяли в последний раз, и не нужно гадать,
    свежие ли данные. ``last_check_ok`` истинно только если панель ответила
    **и** выдала пригодный список инбаундов: иначе получается зелёный бейдж
    «отвечает» рядом с алертом «нет инбаундов».

    :param panels: пары (нода из БД или None для основной панели, клиент панели).
    """
    now = _now()
    results: list[dict] = []
    for node, panel in panels:
        label = node.title if node is not None else (panel.location_title or "Основная панель")
        code = node.code if node is not None else "primary"
        entry = {"code": code, "title": label, "ok": False, "error": "", "inbounds": 0, "cause": ""}

        if node is not None and not node.is_active:
            # Выключенную ноду не проверяем, но и алерты за ней не оставляем.
            await resolve_node_alerts(session, code, note="нода выключена")
            continue

        try:
            entry["ok"], health_error = await _panel_health(panel)
            entry["error"] = health_error
            if entry["ok"]:
                inbounds = await panel.list_inbounds()
                entry["inbounds"] = len(inbounds)
                if not inbounds:
                    entry["error"] = "панель отвечает, но инбаундов нет — клиенты не получат конфиг"
                    entry["cause"] = "inbounds"
        except PanelInboundMissing as exc:
            # Панель жива и ответила — выдать конфиг нечем: расходятся настройки.
            entry["error"] = str(exc)
            entry["cause"] = "inbounds"
        except PanelError as exc:
            entry["error"] = str(exc)
            entry["cause"] = "panel"
        except Exception as exc:  # noqa: BLE001 - чужая панель может ответить чем угодно
            entry["error"] = str(exc)
            entry["cause"] = "panel"

        if node is not None:
            node.last_check_at = now
            node.last_check_ok = bool(entry["ok"]) and not entry["error"]
            node.last_check_error = (entry["error"] or "")[:300]
        #: Панель готова выдать конфиг — по этому признаку считают живые ноды
        #: админка и Telegram: «ответила, но инбаунды разошлись» это не «жива».
        entry["ready"] = bool(entry["ok"]) and not entry["error"]

        fingerprint = f"node:{code}"
        if not entry["ok"]:
            await raise_alert(
                session,
                "node_down",
                f"Нода «{label}» не отвечает",
                severity=SEV_ERR,
                message=entry["error"] or "панель не ответила на проверку",
                fingerprint=fingerprint,
                node_id=node.id if node is not None else None,
                reopen_ack=False,
            )
            # Нода недоступна — про инбаунды и ошибки панели ничего не известно:
            # противоречивая пара «не отвечает» + «нет инбаундов» не нужна.
            await resolve_by_fingerprint(session, f"{fingerprint}:inbounds", note="нода не отвечает")
            await resolve_by_fingerprint(session, f"{fingerprint}:panel", note="нода не отвечает")
        elif entry["error"]:
            if entry["cause"] == "inbounds":
                await raise_alert(
                    session,
                    "node_degraded",
                    f"Нода «{label}»: нет инбаундов",
                    severity=SEV_WARN,
                    message=entry["error"],
                    fingerprint=f"{fingerprint}:inbounds",
                    node_id=node.id if node is not None else None,
                    reopen_ack=False,
                )
                await resolve_by_fingerprint(session, f"{fingerprint}:panel", note="причина — инбаунды")
            else:
                # Панель отвечает, но с ошибкой (формат ответа, HTTP 500 на
                # втором запросе): это не «нет инбаундов» — заголовок и
                # действие оператора другие.
                await raise_alert(
                    session,
                    "panel_error",
                    f"Нода «{label}»: панель отвечает с ошибкой",
                    severity=SEV_WARN,
                    message=entry["error"],
                    fingerprint=f"{fingerprint}:panel",
                    node_id=node.id if node is not None else None,
                    reopen_ack=False,
                )
                await resolve_by_fingerprint(session, f"{fingerprint}:inbounds", note="причина — панель")
            await resolve_by_fingerprint(session, fingerprint)
        else:
            await resolve_by_fingerprint(session, fingerprint, note="нода снова отвечает")
            await resolve_by_fingerprint(session, f"{fingerprint}:inbounds")
            await resolve_by_fingerprint(session, f"{fingerprint}:panel")

        results.append(entry)
    return results


async def check_probes(session: AsyncSession, panels: list[tuple[Node | None, PanelClient]]) -> list[dict]:
    """Проверить ноды «глазами клиента» и завести алерт, если порт не пускает.

    Панель может отвечать по API, а порт инбаунда — нет (закрыли в фаерволе,
    сгорела подсеть, оператор режет адрес). Это ровно тот случай, когда
    ``node_down`` молчит, а клиенты уже не подключаются.

    Заодно пишем задержку: она видна в админке и на странице подключения.

    Важно: пробу надо ещё **суметь поставить**. Если панель не отдала список
    инбаундов, до порта дело не дошло — состояние порта неизвестно, и писать
    «порт не пускает клиента» нельзя. Такой случай не поднимает свой алерт
    (причину панели показывает :func:`check_nodes`) и не стирает замер молча:
    текст причины остаётся в ``Node.last_probe_error``.
    """
    from app.config import get_settings
    from app.services import probe as probe_service

    settings = get_settings()
    results: list[dict] = []
    if not settings.node_probe_enabled:
        return results

    now = _now()
    for node, panel in panels:
        if node is None or not node.is_active:
            continue
        label = node.title or node.code
        detailed = await probe_service.probe_panel_detailed(panel, node.host)
        result = detailed.best
        measured = bool(result.measured)
        node.last_probe_at = now
        # Шаг нужен интерфейсам: по нему отличают «порт не пускает» (tcp/tls)
        # от «пробу не удалось поставить» (panel/config), а «ok» — успех.
        node.last_probe_stage = str(result.stage or "")[:16]
        node.last_probe_error = "" if result.ok else (result.detail or "")[:300]
        # Замер по каждому порту — доказательство диагноза «какой порт закрыт»,
        # когда соседний открыт. Без него «ок» скрывало закрытый порт.
        node.last_probe_ports = json.dumps(
            [item.as_dict() for item in detailed.ports], ensure_ascii=False
        )[:2000]

        # Два факта вместо одного, как в реальности:
        # * «порт открыт» (ok) — есть куда подключиться, клиент не в беде;
        # * «часть портов закрыта» (degraded) — беды ещё нет, но добрая половина
        #   профилей в подписке ведёт в стену, и об этом надо узнать заранее.
        workspace_open = bool(detailed.open_ports)
        degraded = bool(detailed.closed_ports) and workspace_open

        entry = {
            "code": node.code,
            "title": label,
            "ok": bool(result.ok),
            "ms": int(result.ms or 0) if result.ok else 0,
            "stage": result.stage,
            "detail": result.detail,
            #: Порт реально проверялся (TCP/TLS), а не «проба не дошла».
            "probed": measured,
            "verdict": probe_service.probe_verdict(node),
            #: Таблица по портам и причина, если пробу не удалось поставить.
            "ports": [item.as_dict() for item in detailed.ports],
            "probe_error": detailed.error,
            #: Часть портов не пускает, но подключиться есть куда.
            "degraded": degraded,
            "closed_ports": detailed.closed_ports,
            "open_ports": detailed.open_ports,
        }
        fingerprint = f"node:{node.code}:probe"
        if result.ok and not degraded:
            node.last_probe_ok = True
            node.last_probe_ms = int(result.ms or 0)
            await resolve_by_fingerprint(session, fingerprint, note="порт снова пускает клиента")
        elif result.ok and degraded:
            # Подключиться можно, но не всеми профилями из подписки. Это не
            # «нода упала» и не «всё хорошо»: алерт предупреждающий, с портом
            # в заголовке — ровно то, что нужно открыть в фаерволе.
            node.last_probe_ok = True
            node.last_probe_ms = int(result.ms or 0)
            closed = ", ".join(str(port) for port in detailed.closed_ports)
            opened = ", ".join(str(port) for port in detailed.open_ports)
            await raise_alert(
                session,
                "node_probe_degraded",
                f"Нода «{label}»: часть портов закрыта ({closed})",
                severity=SEV_WARN,
                message=(
                    f"подключение работает через порты {opened}; профили с портом {closed} "
                    f"у клиентов не откроются — проверь фаервол и инбаунды"
                ),
                fingerprint=fingerprint,
                node_id=node.id,
                reopen_ack=False,
            )
        elif measured:
            node.last_probe_ok = False
            node.last_probe_ms = 0
            # Сообщение называет КОНКРЕТНЫЙ порт: «порт 8443 не пускает» — это
            # действие («открой фаервол на 8443»), а «порт не пускает» — тема
            # для размышления.
            closed = ", ".join(str(port) for port in detailed.closed_ports)
            title = (
                f"Нода «{label}»: порт {closed} не пускает клиента"
                if closed
                else f"Нода «{label}»: порт не пускает клиента"
            )
            await raise_alert(
                session,
                "node_probe_failed",
                title,
                severity=SEV_ERR,
                message=result.detail or "TCP-проба не прошла",
                fingerprint=fingerprint,
                node_id=node.id,
                reopen_ack=False,
            )
        else:
            # Проба не дошла до порта: утверждать про порт нечего. Раньше здесь
            # рождался ложный «порт не пускает клиента» (err + Telegram), и
            # оператор шёл проверять фаервол вместо настроек панели.
            node.last_probe_ok = False
            node.last_probe_ms = 0
            await resolve_by_fingerprint(
                session, fingerprint, note=f"проба не выполнена: {result.detail}"
            )
        results.append(entry)
    return results
