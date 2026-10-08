"""Проверка готовности аварийного уровня: что настроено, а что нет.

Запуск на сервере (или локально с боевым ``.env``)::

    .venv/bin/python -m app.tools.emergency_check

Отчёт отвечает на вопросы, которые иначе выясняются в самый неподходящий
момент: есть ли вообще аварийные каналы, когда последний раз пробовали порты,
какой пинг у локаций и отвечает ли точка замера (``/ping``), по которой клиент
считает пинг. Код возврата 1, если чего-то не хватает — удобно в скриптах.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from app.db.models import Node
from app.services import probe as probe_service
from app.services.probe import probe_verdict

#: Через сколько минут считать замер пробы устаревшим. Задача пробы идёт
#: каждые 5 минут, так что 20 — это «три пропуска подряд».
PROBE_FRESH_MINUTES = 20

#: Человеческие имена каналов для отчёта.
CHANNEL_TITLES = {"main": "обычная", "reserve": "резерв", "cdn": "CDN"}


@dataclass(slots=True)
class Report:
    """Итог проверки: строки для человека и список того, что мешает.

    ``issues`` — то, без чего аварийный уровень не работает; ``optional`` —
    каналы, которые можно добавить (например дежурный DNS-туннель): без них
    отчёт остаётся «готов», но возможностей меньше.
    """

    ok: bool = False
    lines: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        head = "✅ Аварийный уровень готов" if self.ok else "⚠️ Аварийный уровень не готов"
        body = "\n".join(self.lines) if self.lines else "локаций нет"
        problems = (
            "\n\n<b>Что мешает</b>\n" + "\n".join(f"— {issue}" for issue in self.issues)
            if self.issues
            else ""
        )
        extra = (
            "\n\n<b>Можно добавить</b>\n" + "\n".join(f"— {item}" for item in self.optional)
            if self.optional
            else ""
        )
        return f"{head}\n\n{body}{problems}{extra}"


def assess(
    nodes: list[Node],
    *,
    ping_ok: bool,
    ping_url: str,
    probe_fresh_minutes: int = PROBE_FRESH_MINUTES,
    now: datetime | None = None,
    dns_domain: str = "",
) -> Report:
    """Оценить готовность по списку нод и результату проверки точки замера.

    Функция чистая: ни базы, ни сети — её и проверяют тесты.
    """
    moment = now or datetime.now(timezone.utc)
    active = [node for node in nodes if node.is_active]
    report = Report()

    if not dns_domain.strip():
        report.optional.append(
            "дежурный DNS-канал не настроен (DNS_TUNNEL_DOMAIN пуст): он выручает, "
            "когда не проходит вообще ничего, кроме DNS — см. docs/DNS-ТУННЕЛЬ-2026-10.md"
        )

    if not active:
        report.issues.append("нет ни одной активной локации: сначала нода, потом аварийный уровень")
        report.ok = False
        return report

    freshest: datetime | None = None
    alive = 0
    emergency = 0
    unprobed = 0
    not_configured = 0
    for node in sorted(
        active, key=lambda item: ((item.channel or "main"), item.priority or 100, item.id or 0)
    ):
        channel = (node.channel or "main").strip().lower() or "main"
        title = CHANNEL_TITLES.get(channel, channel)
        if channel != "main":
            emergency += 1

        if node.last_probe_at is not None:
            probed = node.last_probe_at
            if probed.tzinfo is None:
                probed = probed.replace(tzinfo=timezone.utc)
            freshest = max(freshest, probed) if freshest else probed

        # Текст ошибки у «порт не пускает» и «пробу не поставить» одинаковый по
        # форме, поэтому состояние берём из шага пробы, а не из строки.
        verdict = probe_verdict(node)
        reason = str(getattr(node, "last_probe_error", "") or "")
        if verdict == probe_service.PROBE_OK:
            alive += 1
            age = _age_text(node.last_probe_at, moment)
            report.lines.append(f"🟢 {node.title} · {title} · {node.last_probe_ms} мс{age}")
        elif verdict == probe_service.PROBE_UNKNOWN:
            report.lines.append(f"⚪ {node.title} · {title} · пробы ещё не было")
        elif verdict == probe_service.PROBE_PORT_FAILED:
            report.lines.append(f"🔴 {node.title} · {title} · порт не пускает")
        elif verdict == probe_service.PROBE_NOT_CONFIGURED:
            # UDP-only канал или не заполнен host: проба к порту неприменима.
            not_configured += 1
            report.lines.append(f"⚪ {node.title} · {title} · проба не настроена: {reason}")
        else:
            unprobed += 1
            report.lines.append(f"⚪ {node.title} · {title} · проба не выполнена: {reason}")

    if emergency == 0:
        report.issues.append(
            "нет ни одного аварийного канала: добавь ноду и поставь ей канал «резервная» или «CDN»"
        )
    if alive == 0:
        if not_configured == len(active):
            # Ни одна проба не применима: это не сеть нод и не настройка ID.
            report.issues.append(
                "пробы не умеют проверять эти каналы: у нод нет TCP-инбаундов (только UDP) "
                "или не заполнен host — проверь порты и firewall руками"
            )
        elif unprobed == len(active):
            report.issues.append(
                "ни одну пробу не удалось выполнить: смотри причину в строках выше "
                "(чаще всего панель не отдаёт инбаунды — сверь ID в /admin/nodes)"
            )
        else:
            report.issues.append("ни одна проба не проходит: проверь порты и firewall на нодах")
    if freshest is None:
        report.issues.append("пробы ни разу не запускались: подожди 5 минут после старта бота")
    elif moment - freshest > timedelta(minutes=probe_fresh_minutes):
        minutes = int((moment - freshest).total_seconds() // 60)
        report.issues.append(f"пробы устарели: последняя была {minutes} мин назад (задача идёт каждые 5 мин)")
    if not ping_ok:
        report.issues.append(
            f"точка замера {ping_url} не отвечает 204 — в приложении у клиента не будет пинга"
        )

    report.ok = not report.issues
    return report


def _age_text(probed_at: datetime | None, now: datetime) -> str:
    """«(2 мин назад)» — чтобы свежесть замера была видна без часов."""
    if probed_at is None:
        return ""
    moment = probed_at if probed_at.tzinfo else probed_at.replace(tzinfo=timezone.utc)
    minutes = max(0, int((now - moment).total_seconds() // 60))
    return f" ({minutes} мин назад)"


async def load_nodes() -> list[Node]:
    """Активные ноды из базы.

    Сначала прогоняем ``init_db``: инструмент запускают на сервере руками, и он
    должен сам дотянуть недостающие колонки (create_all + лёгкие миграции),
    иначе на старой базе падает с «no such column».
    """
    from sqlalchemy import select

    from app.db.session import SessionMaker, init_db

    await init_db()
    async with SessionMaker() as session:
        return list(
            (
                await session.scalars(
                    select(Node).where(Node.is_active.is_(True)).order_by(Node.priority, Node.id)
                )
            ).all()
        )


async def check_ping(url: str, timeout: float = 3.0) -> bool:
    """Отвечает ли точка замера: клиент ждёт 204 без тела."""
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:  # noqa: S501 - свой сертификат
            response = await client.get(url)
    except Exception:  # noqa: BLE001 - сеть может отказать как угодно
        return False
    return 200 <= response.status_code < 300


async def run() -> Report:
    """Собрать отчёт: ноды из базы плюс проверка точки замера."""
    from app.config import get_settings
    from app.web.sub import _test_url

    ping_url = _test_url()
    nodes = await load_nodes()
    ping_ok = await check_ping(ping_url)
    return assess(
        nodes,
        ping_ok=ping_ok,
        ping_url=ping_url,
        dns_domain=get_settings().dns_tunnel_domain,
    )


def main() -> int:  # pragma: no cover - ручной запуск
    report = asyncio.run(run())
    print(report.as_text().replace("<b>", "").replace("</b>", ""))
    print(f"\nточка замера: {_ping_url_for_display()}")
    return 0 if report.ok else 1


def _ping_url_for_display() -> str:  # pragma: no cover - ручной запуск
    from app.web.sub import _test_url

    return _test_url()


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    raise SystemExit(main())
