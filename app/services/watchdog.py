"""Суточный контроль клиентов панели: кто сидит и сколько качает.

Зачем это нужно. Однажды с сервера бесплатно скачали **473 ГБ**: клиент был
создан руками, без срока действия и без лимита трафика, и панель об этом
никак не сообщала. Лимит в пробном доступе от такого не защищает — защищает
только регулярная сверка списка клиентов.

Как работает: раз в сутки забираем клиентов панели, сравниваем с прошлым
снимком (файл ``WATCH_SNAPSHOT_FILE``) и считаем суточный трафик по разнице.
В отчёт попадают:

* **тяжёлые** — больше ``WATCH_DAILY_GB`` за сутки;
* **вечные** — включённые клиенты без срока действия;
* **новые** — появились с прошлой проверки;
* **исчезнувшие** — были в снимке, но пропали из панели.

Отчёт уходит админам в Telegram; если аномалий нет — приходит короткая сводка.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings
from app.panels.base import PanelClient, PanelError

logger = logging.getLogger(__name__)

GIB = 1024**3


@dataclass
class WatchedClient:
    """Клиент панели глазами аудита."""

    email: str
    enabled: bool
    used_bytes: int = 0
    day_bytes: int = 0
    unlimited: bool = False
    never_expires: bool = False
    last_online: datetime | None = None

    @property
    def used_gb(self) -> float:
        return self.used_bytes / GIB

    @property
    def day_gb(self) -> float:
        return self.day_bytes / GIB

    @property
    def is_perpetual(self) -> bool:
        """Включён, без срока действия и без лимита — «вечный» бесплатный доступ."""
        return self.enabled and self.never_expires


@dataclass
class WatchReport:
    """Итог суточной проверки."""

    checked_at: datetime
    total: int
    clients: list[WatchedClient] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    gone: list[str] = field(default_factory=list)
    daily_gb_limit: int = 0

    @property
    def ignored(self) -> set[str]:
        """Свои клиенты (домашние устройства) — по ним тревог не поднимаем."""
        return {email.strip() for email in get_settings().watch_ignore.split(",") if email.strip()}

    @property
    def heavy(self) -> list[WatchedClient]:
        limit = self.daily_gb_limit * GIB
        skip = self.ignored
        return sorted(
            (c for c in self.clients if c.day_bytes >= limit and limit > 0 and c.email not in skip),
            key=lambda c: c.day_bytes,
            reverse=True,
        )

    @property
    def perpetual(self) -> list[WatchedClient]:
        skip = self.ignored
        return sorted(
            (c for c in self.clients if c.is_perpetual and c.email not in skip),
            key=lambda c: c.used_gb,
            reverse=True,
        )

    @property
    def day_total_bytes(self) -> int:
        return sum(c.day_bytes for c in self.clients)

    @property
    def alert_count(self) -> int:
        """Сколько всего замечаний попадёт в отчёт."""
        return len(self.heavy) + len(self.perpetual) + len(self.new)


# ------------------------------------------------------------------ снимок
def _snapshot_path() -> Path:
    return Path(get_settings().watch_snapshot_file)


def load_snapshot() -> dict[str, int]:
    """Прошлый снимок: ``email → байт``. Битый файл не должен ронять задачу."""
    path = _snapshot_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Снимок контроля клиентов повреждён (%s), считаем пустым", exc)
        return {}
    clients = data.get("clients")
    if not isinstance(clients, dict):
        return {}
    return {str(email): int(value or 0) for email, value in clients.items()}


def save_snapshot(clients: dict[str, int]) -> None:
    path = _snapshot_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "clients": clients,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:  # диск/права — не повод терять отчёт
        logger.warning("Не удалось сохранить снимок контроля клиентов: %s", exc)


# ------------------------------------------------------------------ проверка
def _last_online(user) -> datetime | None:
    """Когда клиент последний раз выходил на связь (панель отдаёт мс)."""
    raw = getattr(user, "raw", None) or {}
    stamp = int(raw.get("last_online_ms") or 0)
    if stamp <= 0:
        return None
    return datetime.fromtimestamp(stamp / 1000, tz=timezone.utc)


async def run_watch(panel: PanelClient | None = None) -> WatchReport | None:
    """Собрать отчёт по клиентам панели.

    ``None`` возвращаем, если панель недоступна или не отдала список клиентов:
    пустой отчёт выглядел бы как «всё чисто», а это было бы враньём.
    """
    if panel is None:
        from app.panels.registry import registry

        panel = registry.primary()

    settings = get_settings()
    try:
        users = await panel.list_users()
    except PanelError as exc:
        logger.error("Контроль клиентов: панель недоступна — %s", exc)
        return None

    if not users:
        logger.warning("Контроль клиентов: панель вернула пустой список, отчёт пропущен")
        return None

    previous = load_snapshot()
    clients: list[WatchedClient] = []
    for user in users:
        used = int(user.used_bytes or 0)
        was = previous.get(user.email)
        # Нового клиента в прошлом снимке нет: суточный расход неизвестен,
        # поэтому показываем 0, а сам факт появления попадает в отчёт.
        clients.append(
            WatchedClient(
                email=user.email,
                enabled=bool(user.enabled),
                used_bytes=used,
                day_bytes=max(0, used - was) if was is not None else 0,
                unlimited=not user.traffic_limit_bytes,
                never_expires=user.expires_at is None,
                last_online=_last_online(user),
            )
        )

    emails = {c.email for c in clients}
    report = WatchReport(
        checked_at=datetime.now(timezone.utc),
        total=len(clients),
        clients=clients,
        new=sorted(emails - set(previous) - {e.strip() for e in settings.watch_ignore.split(",") if e.strip()}),
        gone=sorted(set(previous) - emails),
        daily_gb_limit=int(settings.watch_daily_gb),
    )
    save_snapshot({c.email: c.used_bytes for c in clients})
    return report


# ------------------------------------------------------------------ текст
def format_report(report: WatchReport) -> str:
    """Короткий отчёт для Telegram: цифры сверху, аномалии — списком."""
    lines = [
        "🛡 <b>Контроль клиентов</b>",
        f"Проверено: {report.checked_at.astimezone().strftime('%d.%m.%Y %H:%M')}",
        "",
        f"Клиентов в панели: <b>{report.total}</b>",
        f"Трафик за сутки: <b>{report.day_total_bytes / GIB:.2f} ГБ</b>",
    ]

    if report.heavy:
        lines += ["", f"⚠️ <b>Качают больше {report.daily_gb_limit} ГБ за сутки:</b>"]
        lines += [f"• <code>{c.email}</code> — {c.day_gb:.1f} ГБ (всего {c.used_gb:.1f} ГБ)" for c in report.heavy]

    if report.perpetual:
        lines += ["", "🔓 <b>Без срока действия</b> (доступ не закроется сам):"]
        for c in report.perpetual:
            limit = "без лимита трафика" if c.unlimited else "лимит есть"
            lines.append(f"• <code>{c.email}</code> — всего {c.used_gb:.1f} ГБ, {limit}")

    if report.new:
        lines += ["", "🆕 Появились с прошлой проверки: " + ", ".join(f"<code>{e}</code>" for e in report.new)]

    if report.gone:
        lines += ["", "🗑 Пропали из панели: " + ", ".join(f"<code>{e}</code>" for e in report.gone)]

    if not (report.heavy or report.perpetual or report.new or report.gone):
        lines += ["", "✅ Аномалий нет"]

    return "\n".join(lines)
