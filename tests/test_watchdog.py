"""Тесты суточного контроля клиентов: аномалии трафика и «вечные» доступы.

Смысл: однажды с сервера бесплатно скачали 473 ГБ через клиента, созданного
руками, без срока и лимита. Панель об этом молчала — значит, нужен сторож.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.panels.base import UserSpec
from app.services import watchdog


@pytest.fixture
def snapshot_file(tmp_path, monkeypatch):
    """Снимок кладём во временный файл, а не в data/."""
    path = tmp_path / "watch.json"
    monkeypatch.setattr(get_settings(), "watch_snapshot_file", str(path))
    return path


async def add_client(panel, email: str, *, gb: float = 1.0, days: int = 30):
    """Создать клиента в фейковой панели и «накрутить» ему трафик.

    ``days=0`` — бессрочный доступ (в панели это ``expiryTime=0``).
    """
    user = await panel.create_user(UserSpec(email=email, days=days, traffic_gb=0, devices=0))
    user.used_bytes = int(gb * watchdog.GIB)
    if days == 0:
        user.expires_at = None
    return user


async def test_watch_reports_nothing_on_first_run(snapshot_file, panel):
    """Первый запуск: суточной разницы ещё нет, но снимок уже сохранён."""
    await add_client(panel, "alice", gb=5)

    report = await watchdog.run_watch(panel)

    assert report is not None
    assert report.total == 1
    assert report.heavy == []
    assert snapshot_file.exists()
    assert "alice" in snapshot_file.read_text(encoding="utf-8")


async def test_watch_flags_heavy_daily_traffic(snapshot_file, monkeypatch, panel):
    """Клиент, который за сутки скачал больше лимита, попадает в тревоги."""
    monkeypatch.setattr(get_settings(), "watch_daily_gb", 100)
    user = await add_client(panel, "alice", gb=1)
    await watchdog.run_watch(panel)  # снимок «на начало суток»

    user.used_bytes = int(250 * watchdog.GIB)
    report = await watchdog.run_watch(panel)

    assert [c.email for c in report.heavy] == ["alice"]
    assert report.heavy[0].day_gb == pytest.approx(249, abs=1)
    assert report.alert_count == 1


async def test_watch_flags_perpetual_access(snapshot_file, panel):
    """Клиент без срока действия — «вечный» доступ, его видно в отчёте."""
    await add_client(panel, "forever", gb=473, days=0)

    report = await watchdog.run_watch(panel)

    assert [c.email for c in report.perpetual] == ["forever"]
    assert "Без срока действия" in watchdog.format_report(report)


async def test_watch_marks_new_and_gone_clients(snapshot_file, panel):
    """Появление и исчезновение клиентов тоже важно: так находят «левых»."""
    alice = await add_client(panel, "alice", gb=1)
    await watchdog.run_watch(panel)

    await panel.delete_user(alice.uuid)
    await add_client(panel, "bob", gb=1)
    report = await watchdog.run_watch(panel)

    assert report.new == ["bob"]
    assert report.gone == ["alice"]


async def test_watch_skips_report_when_panel_is_empty(snapshot_file, panel):
    """Пустой список клиентов — это не «всё чисто», а «нет данных»."""
    report = await watchdog.run_watch(panel)

    assert report is None
    assert not snapshot_file.exists()


async def test_report_text_is_quiet_when_everything_is_fine(snapshot_file, panel):
    await add_client(panel, "alice", gb=2)
    await watchdog.run_watch(panel)  # первый прогон: alice попадёт в «новые»

    report = await watchdog.run_watch(panel)
    text = watchdog.format_report(report)

    assert "Контроль клиентов" in text
    assert "Аномалий нет" in text
    assert "alice" not in text  # здоровых клиентов не перечисляем


async def test_watch_ignores_own_devices(snapshot_file, monkeypatch, panel):
    """Свои устройства не должны шуметь в отчёте — их исключаем по списку."""
    monkeypatch.setattr(get_settings(), "watch_ignore", "my-laptop, my-phone")
    await add_client(panel, "my-laptop", gb=500, days=0)
    await add_client(panel, "stranger", gb=500, days=0)

    report = await watchdog.run_watch(panel)
    text = watchdog.format_report(report)

    assert [c.email for c in report.perpetual] == ["stranger"]
    assert "my-laptop" not in text
    assert "stranger" in text
