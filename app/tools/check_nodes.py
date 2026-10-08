"""Диагностика инбаундов: что настроено в боте против того, что реально в панели.

Зачем: алерт «в панели не найдены инбаунды [3] (проверь inbound_ids)» сообщает
о расхождении, но не показывает, какие ID в панели есть. Из-за этого починка
начинается с похода в 3x-ui руками. Инструмент делает ту же работу, что бот, и
печатает расхождение построчно — вместе с тем, что вписать в админке.

Запуск на сервере (или локально с боевым ``.env``)::

    .venv/bin/python -m app.tools.check_nodes

Код возврата 1, если есть расхождения или панель не ответила — удобно в скриптах
и в дежурной проверке. Отдельно предупреждаем про **дубль адреса панели**: если
``panel_url`` ноды совпадает с ``PANEL_URL``, одна и та же панель опрашивается
дважды с разными ``inbound_ids``, и «основная» проверка зелёная, пока нода
ругается на несуществующий ID.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.panels.base import panel_label, parse_inbound_ids


@dataclass(slots=True)
class PanelReport:
    """Одна панель: что настроено, что в ней есть и чем это грозит."""

    code: str
    title: str
    url: str
    configured: list[int] = field(default_factory=list)
    actual: list[tuple[int, str, str, int]] = field(default_factory=list)
    ok: bool = False
    error: str = ""
    missing: list[int] = field(default_factory=list)
    extra: list[int] = field(default_factory=list)
    #: Пояснение, если сверка ID не имеет смысла (панель-заглушка).
    note: str = ""

    @property
    def healthy(self) -> bool:
        """Всё сходится: панель ответила, нужные ID на месте."""
        return self.ok and not self.error and not self.missing

    def as_text(self) -> str:
        head = f"{self.title} ({self.code})"
        if self.url:
            head += f" · {self.url}"
        lines = [head]
        if self.error:
            lines.append(f"  ⚠️ {self.error}")
        lines.append(f"  настроено: {', '.join(str(i) for i in self.configured) or '— (все инбаунды)'}")
        if self.actual:
            listed = ", ".join(f"{i} «{remark}» {proto}:{port}" for i, remark, proto, port in self.actual)
            lines.append(f"  в панели:  {listed}")
        elif not self.error:
            lines.append("  в панели:  пусто")
        if self.missing:
            lines.append(f"  ❌ нет настроенных ID: {', '.join(str(i) for i in self.missing)}")
            lines.append(
                f"     что делать: /admin/nodes?edit={self.code} → вписать "
                f"«{','.join(str(i) for i in (self.actual_ids or self.configured))}» → "
                "сохранить → «Проверить ноды»"
            )
        elif self.healthy:
            lines.append("  ✅ сходится")
        if self.extra:
            # Инбаунд есть в панели, но не входит в настройки: клиенты его не
            # получат. Это не ошибка (лишний порт мог остаться от экспериментов),
            # но молчать нельзя — иначе «сходится» читается как «всё включено».
            lines.append(
                "  ℹ️ в панели есть инбаунды вне настроек: "
                f"{', '.join(str(i) for i in self.extra)} — их локации клиенты не получат"
            )
        if self.note:
            lines.append(f"  ℹ️ {self.note}")
        return "\n".join(lines)

    @property
    def actual_ids(self) -> list[int]:
        """ID, которые реально есть в панели, — их и надо вписать в настройки."""
        return [item[0] for item in self.actual]

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "title": self.title,
            "url": self.url,
            "configured": self.configured,
            "actual": [
                {"id": i, "remark": remark, "protocol": proto, "port": port}
                for i, remark, proto, port in self.actual
            ],
            "ok": self.ok,
            "error": self.error,
            "missing": self.missing,
            "healthy": self.healthy,
            "note": self.note,
        }


@dataclass(slots=True)
class Report:
    """Итог: панели, предупреждения о дублях и общий вердикт."""

    panels: list[PanelReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(panel.healthy for panel in self.panels)

    def as_text(self) -> str:
        moment = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        head = "✅ Инбаунды сходятся" if self.ok else "⚠️ Есть расхождения в инбаундах"
        body = "\n\n".join(panel.as_text() for panel in self.panels) or "панелей нет"
        extra = "\n\n".join(f"⚠️ {item}" for item in self.warnings)
        return f"{head} · {moment}\n\n{body}" + (f"\n\n{extra}" if extra else "")

    def as_dict(self) -> dict:
        return {"ok": self.ok, "panels": [panel.as_dict() for panel in self.panels], "warnings": self.warnings}


async def collect(session=None) -> Report:  # noqa: ANN001 - AsyncSession, но тип не тянем в CLI
    """Собрать отчёт по всем панелям: основная + активные ноды.

    Панель спрашиваем **сырым** списком (``list_all_inbounds``): фильтрованный
    как раз и падает при расхождении, а нам нужны фактические ID.
    """
    from app.config import get_settings
    from app.db.session import SessionMaker, init_db
    from app.panels.registry import registry

    settings = get_settings()
    report = Report()

    if session is None:
        # Инструмент запускают руками на сервере: пусть сам дотянет колонки
        # (create_all + лёгкие миграции), иначе на старой базе будет «no such column».
        await init_db()
        async with SessionMaker() as own_session:
            return await collect(own_session)

    pairs = await registry.all_panels_with_nodes(session)
    urls: list[tuple[str, str]] = []
    nodes_seen = 0
    for node, panel in pairs:
        code = node.code if node is not None else "primary"
        if node is not None:
            nodes_seen += 1
        title = (node.title if node is not None else "") or panel_label(panel)
        url = (node.panel_url if node is not None else "") or str(getattr(panel, "base_url", "") or "")
        configured = parse_inbound_ids(node.inbound_ids) if node is not None else list(settings.inbound_id_list)
        # Заглушка панели (fake) игнорирует inbound_ids: сверять нечего, иначе
        # инструмент позовёт «чинить» рабочие настройки — как и на странице нод.
        kind = (node.panel_type if node is not None else settings.panel_type) or ""
        comparable = kind.strip().lower() == "xui"
        item = PanelReport(code=code, title=title, url=url, configured=configured)

        try:
            checker = getattr(panel, "check_health", None)
            if callable(checker):
                item.ok, item.error = await checker()
            else:  # pragma: no cover - сторонняя панель без check_health
                item.ok, item.error = bool(await panel.health()), ""
        except Exception as exc:  # noqa: BLE001 - панель отвечает чем угодно
            item.ok, item.error = False, str(exc)

        if item.ok:
            lister = getattr(panel, "list_all_inbounds", None) or panel.list_inbounds
            try:
                inbounds = list(await lister())
            except Exception as exc:  # noqa: BLE001 - панель отвечает чем угодно
                item.error = str(exc)
                inbounds = []
            item.actual = [
                (
                    int(getattr(inbound, "id", 0)),
                    str(getattr(inbound, "remark", "")),
                    str(getattr(inbound, "protocol", "")),
                    int(getattr(inbound, "port", 0) or 0),
                )
                for inbound in inbounds
            ]
            actual_ids = item.actual_ids
            if comparable:
                item.missing = [i for i in configured if i not in actual_ids]
                item.extra = [i for i in actual_ids if configured and i not in configured]
            elif configured:
                item.note = (
                    f"панель типа «{kind}» не применяет inbound_ids — сверка ID пропущена "
                    "(в продакшене ноды должны быть типа xui)"
                )
        elif configured and not comparable:
            item.note = f"панель типа «{kind}»: сверка ID не выполняется"

        report.panels.append(item)
        if url:
            urls.append((code, url.rstrip("/")))

    # Дубль адреса: одна и та же панель под двумя записями (F18 в ревью).
    seen: dict[str, str] = {}
    for code, url in urls:
        if url in seen:
            report.warnings.append(
                f"адрес {url} указан дважды: «{seen[url]}» и «{code}» — одна панель "
                "опрашивается с разными inbound_ids, локация попадёт в подписку дважды"
            )
        else:
            seen[url] = code

    if nodes_seen == 0:
        # Иначе «✅ сходятся» на пустой базе читается как «ноды в порядке»,
        # хотя сравнивать было нечего: есть только основная панель.
        report.warnings.append(
            "активных нод нет — сравнить нечего; смотри /admin/nodes и включи ноду"
        )

    return report


def main() -> int:  # pragma: no cover - ручной запуск
    report = asyncio.run(collect())
    if "--json" in sys.argv:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.as_text())
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    raise SystemExit(main())
