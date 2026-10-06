"""Тесты алертов панели и автопроверки нод.

Смысл: центр алертов — это то, на что смотрит модератор вместо чата. Если
проверка нод перестанет писать алерты (или начнёт плодить дубли), проблемы
снова будут видны только тому, кто случайно прочитал Telegram.
"""

from __future__ import annotations

from app.db.models import Node
from app.panels.base import PanelClient, PanelError, PanelUser, UserSpec
from app.services import alerts as alerts_service
from app.services.alerts import (
    SEV_ERR,
    SEV_WARN,
    STATUS_ACK,
    STATUS_OPEN,
    STATUS_RESOLVED,
)

#: Минимальная панель для проверок: умеет быть сломанной или без инбаундов.
class StubPanel(PanelClient):
    name = "stub"
    location_title = "🇩🇪 Германия"

    def __init__(self, *, healthy: bool = True, inbounds: int = 1, error: str = "") -> None:
        self.healthy = healthy
        self.inbounds = inbounds
        self.error = error
        self.health_calls = 0

    async def health(self) -> bool:
        self.health_calls += 1
        if self.error:
            raise PanelError(self.error)
        return self.healthy

    async def list_inbounds(self):  # noqa: ANN201 - тестовая заглушка
        return [object()] * self.inbounds

    async def create_user(self, spec: UserSpec) -> PanelUser:  # pragma: no cover - не нужен
        raise NotImplementedError

    async def update_user(self, uuid: str, **kwargs) -> PanelUser:  # pragma: no cover
        raise NotImplementedError

    async def delete_user(self, uuid: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def get_user(self, uuid: str) -> PanelUser | None:  # pragma: no cover
        return None

    async def get_configs(self, uuid: str) -> list[str]:  # pragma: no cover
        return []


async def _node(session, code: str = "de") -> Node:
    node = Node(code=code, title="🇩🇪 Германия", panel_type="fake")
    session.add(node)
    await session.commit()
    return node


# ------------------------------------------------------------------ алерты
async def test_same_problem_does_not_create_duplicates(session):
    first = await alerts_service.raise_alert(session, "node_down", "Нода не отвечает", fingerprint="node:de")
    second = await alerts_service.raise_alert(session, "node_down", "Нода не отвечает", fingerprint="node:de")
    await session.commit()

    assert first.id == second.id
    assert second.repeat_count == 2
    assert int(await alerts_service.count_alerts(session, "open")) == 1


async def test_resolved_problem_can_be_raised_again(session):
    """После закрытия та же проблема поднимается заново, а не молчит."""
    first = await alerts_service.raise_alert(session, "node_down", "Нода не отвечает", fingerprint="node:de")
    await alerts_service.resolve_by_fingerprint(session, "node:de", note="нода ответила")
    await session.commit()
    assert first.status == STATUS_RESOLVED

    again = await alerts_service.raise_alert(session, "node_down", "Нода не отвечает", fingerprint="node:de")
    await session.commit()
    assert again.id != first.id
    assert again.status == STATUS_OPEN
    assert int(await alerts_service.count_alerts(session, "open")) == 1


async def test_summary_counts_only_open_and_warns(session):
    await alerts_service.raise_alert(session, "node_down", "Критично", fingerprint="a", severity=SEV_ERR)
    await alerts_service.raise_alert(session, "payment_unmatched", "Внимание", fingerprint="b", severity=SEV_WARN)
    info = await alerts_service.raise_alert(session, "sharing_suspect", "Инфо", fingerprint="c")
    await alerts_service.resolve_alert(session, info, by="тест")
    acked = await alerts_service.raise_alert(session, "panel_error", "Взяли в работу", fingerprint="d")
    await alerts_service.ack_alert(session, acked, by="тест")
    await session.commit()

    summary = await alerts_service.summary(session)
    assert summary.open_err == 1
    assert summary.open_warn == 1
    assert summary.open_info == 0  # закрытый инфо-алерт не считается
    assert summary.ack == 1
    assert summary.attention == 2  # критичные + внимание
    assert acked.status == STATUS_ACK


async def test_resolve_keeps_author_and_note(session):
    alert = await alerts_service.raise_alert(session, "node_down", "Нода", fingerprint="node:de")
    await alerts_service.resolve_alert(session, alert, by="модератор", note="сервер перезагрузили")
    await session.commit()

    assert alert.status == STATUS_RESOLVED
    assert alert.resolved_by == "модератор"
    assert alert.resolve_note == "сервер перезагрузили"
    assert alert.resolved_at is not None


# ------------------------------------------------------------- проверка нод
async def test_healthy_node_raises_nothing_and_writes_check_time(session):
    node = await _node(session)
    results = await alerts_service.check_nodes(session, [(node, StubPanel())])
    await session.commit()

    assert results[0]["ok"] is True
    assert node.last_check_ok is True
    assert node.last_check_at is not None
    assert int(await alerts_service.count_alerts(session, "open")) == 0


async def test_down_node_raises_critical_alert(session):
    node = await _node(session)
    results = await alerts_service.check_nodes(session, [(node, StubPanel(error="ConnectTimeout"))])
    await session.commit()

    assert results[0]["ok"] is False
    assert node.last_check_ok is False

    opened = await alerts_service.list_alerts(session, status="open")
    assert len(opened) == 1
    assert opened[0].kind == "node_down"
    assert opened[0].severity == SEV_ERR
    assert opened[0].fingerprint == "node:de"
    assert opened[0].node_id == node.id
    assert "ConnectTimeout" in (opened[0].message or "")


async def test_repeated_failure_grows_counter_not_rows(session):
    node = await _node(session)
    for _ in range(3):
        await alerts_service.check_nodes(session, [(node, StubPanel(healthy=False))])
    await session.commit()

    opened = await alerts_service.list_alerts(session, status="open")
    assert len(opened) == 1
    assert opened[0].repeat_count == 3


async def test_recovery_closes_alert_automatically(session):
    node = await _node(session)
    await alerts_service.check_nodes(session, [(node, StubPanel(healthy=False))])
    await session.commit()
    assert int(await alerts_service.count_alerts(session, "open")) == 1

    await alerts_service.check_nodes(session, [(node, StubPanel(healthy=True))])
    await session.commit()

    assert int(await alerts_service.count_alerts(session, "open")) == 0
    assert int(await alerts_service.count_alerts(session, "resolved")) == 1


async def test_panel_without_inbounds_is_warning(session):
    """Панель отвечает, но инбаундов нет: доступ по подписке не соберётся."""
    result = await alerts_service.check_nodes(session, [(None, StubPanel(inbounds=0))])
    await session.commit()

    assert result[0]["ok"] is True
    opened = await alerts_service.list_alerts(session, status="open")
    assert len(opened) == 1
    assert opened[0].kind == "node_degraded"
    assert opened[0].severity == SEV_WARN


async def test_primary_panel_alert_has_its_own_fingerprint(session):
    await alerts_service.check_nodes(session, [(None, StubPanel(healthy=False))])
    await session.commit()
    opened = await alerts_service.list_alerts(session, status="open")
    assert opened[0].fingerprint == "node:primary"


# ------------------------------------------------- планировщик пишет алерты
async def test_scheduled_health_job_fills_alert_center(session, bot, monkeypatch):
    """Фоновая проверка нод должна наполнять центр алертов, а не только Telegram."""
    from app import main as app_main
    from app.panels.registry import registry
    from app.services import notifications

    sent: list[str] = []

    async def fake_notify(bot_, text, **kwargs):  # noqa: ANN001
        sent.append(text)

    monkeypatch.setattr(notifications, "notify_admins", fake_notify)
    monkeypatch.setattr(registry, "primary", lambda: StubPanel(healthy=False))
    # состояние между вызовами сбрасываем, чтобы сработало уведомление о смене
    app_main.job_node_health.__dict__.pop("state", None)

    await app_main.job_node_health(bot)
    await session.commit()

    opened = await alerts_service.list_alerts(session, status="open")
    assert any(alert.kind == "node_down" for alert in opened)
    assert sent, "админам должно уйти уведомление о падении ноды"


__all__ = ["StubPanel"]
