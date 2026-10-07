"""Алерты панели: проблемы видны в интерфейсе, а не только в Telegram.

Зачем: сообщение в чат легко пропустить, а «нода недоступна» или «пришло
199.13 ₽, заказ не найден» — это работа, которую нельзя терять. Алерты
дедуплицируются по отпечатку: пока открыт алерт ``node:de``, повторные
проверки увеличивают счётчик, а не создают сотню строк.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Alert, Node
from app.panels.base import PanelClient, PanelError

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
) -> Alert:
    """Поднять алерт. Повтор с тем же отпечатком не плодит строк, а обновляет её."""
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
        alert.status = STATUS_OPEN if alert.status == STATUS_ACK else alert.status
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
async def check_nodes(session: AsyncSession, panels: list[tuple[Node | None, PanelClient]]) -> list[dict]:
    """Проверить доступность нод и завести/закрыть алерты.

    Пишем ``Node.last_check_at``/``last_check_ok`` — на дашборде видно, когда
    ноду проверяли в последний раз, и не нужно гадать, свежие ли данные.

    :param panels: пары (нода из БД или None для основной панели, клиент панели).
    """
    now = _now()
    results: list[dict] = []
    for node, panel in panels:
        label = node.title if node is not None else (panel.location_title or "Основная панель")
        code = node.code if node is not None else "primary"
        entry = {"code": code, "title": label, "ok": False, "error": "", "inbounds": 0}
        try:
            entry["ok"] = await panel.health()
            if entry["ok"]:
                inbounds = await panel.list_inbounds()
                entry["inbounds"] = len(inbounds)
                if not inbounds:
                    entry["error"] = "панель отвечает, но инбаундов нет — клиенты не получат конфиг"
        except PanelError as exc:
            entry["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - чужая панель может ответить чем угодно
            entry["error"] = str(exc)

        if node is not None:
            node.last_check_at = now
            node.last_check_ok = bool(entry["ok"])

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
            )
        elif entry["error"]:
            await raise_alert(
                session,
                "node_degraded",
                f"Нода «{label}»: нет инбаундов",
                severity=SEV_WARN,
                message=entry["error"],
                fingerprint=f"{fingerprint}:inbounds",
                node_id=node.id if node is not None else None,
            )
            await resolve_by_fingerprint(session, fingerprint)
        else:
            await resolve_by_fingerprint(session, fingerprint, note="нода снова отвечает")
            await resolve_by_fingerprint(session, f"{fingerprint}:inbounds")

        results.append(entry)
    return results


async def check_probes(session: AsyncSession, panels: list[tuple[Node | None, PanelClient]]) -> list[dict]:
    """Проверить ноды «глазами клиента» и завести алерт, если порт не пускает.

    Панель может отвечать по API, а порт инбаунда — нет (закрыли в фаерволе,
    сгорела подсеть, оператор режет адрес). Это ровно тот случай, когда
    ``node_down`` молчит, а клиенты уже не подключаются.

    Заодно пишем задержку: она видна в админке и на странице подключения.
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
        result = await probe_service.probe_panel(panel, node.host)
        node.last_probe_at = now
        node.last_probe_ok = bool(result.ok)
        node.last_probe_ms = int(result.ms or 0) if result.ok else 0

        entry = {
            "code": node.code,
            "title": label,
            "ok": bool(result.ok),
            "ms": node.last_probe_ms,
            "stage": result.stage,
            "detail": result.detail,
        }
        fingerprint = f"node:{node.code}:probe"
        if result.ok:
            await resolve_by_fingerprint(session, fingerprint, note="порт снова пускает клиента")
        else:
            await raise_alert(
                session,
                "node_probe_failed",
                f"Нода «{label}»: порт не пускает клиента",
                severity=SEV_ERR,
                message=result.detail or "TCP-проба не прошла",
                fingerprint=fingerprint,
                node_id=node.id,
            )
        results.append(entry)
    return results
