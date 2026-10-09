"""Аудит локаций: какая не работает и **почему** — по слоям, с доказательствами.

Зачем. На вопрос «NL не работает» сейчас отвечают четыре разных экрана, и
каждый видит только свой слой:

* ``/status`` показывает панель и последний замер порта, но **не видит** ни
  расхождения ``inbound_ids``, ни пустого ``sub_base``, ни отсутствия профиля
  клиента на инбаунде локации, ни дубля панели — а именно эти четыре причины и
  дают «не работает» при зелёной статусной странице;
* алерт «порт не пускает клиента» рождался и тогда, когда проба вообще не дошла
  до порта (панель не отдала инбаунды) — оператор шёл чинить фаервол;
* админка, дашборд и Telegram показывают следствие (красный бейдж, алерт), но
  не причину целиком;
* экран локаций в боте молча теряет страну, если панель не отдала инбаунды, а
  сервис подписок при сбое выдачи пишет только в лог.

Инструмент проходит по слоям клиентского пути и печатает по каждому
**доказательство**, а не догадку:

1. **панель** — отвечает ли API (``check_health``), сколько попыток, за сколько
   секунд; при отказе — точный текст ошибки;
2. **инбаунды и ID** — что настроено в боте и что реально в панели. Сверку не
   дублируем: берём готовый отчёт :mod:`app.tools.check_nodes` — тот же код, что
   у бота и админки, иначе диагнозы разъедутся;
3. **порт клиента** — TCP-проба по **каждому** TCP-инбаунду (``порт → ok/ошибка/мс``),
   а не «первый успешный»: мёртвый запасной порт видно сразу;
4. **сервис подписок** — задан ли адрес (``sub_base``), отвечает ли он, отдаёт ли
   конфиги (``panel.get_configs``) и какой HOST стоит в этих конфигах;
5. **дубль панели** — одна панель под двумя локациями: «страна» на самом деле
   чужая, и настоящую никто не проверяет.

Вердикт по локации — один из шести, в порядке «как далеко доходит клиент»:

========================  ==================================================
``панель не отвечает``    API не ответил ни на одну попытку: списка инбаундов
                          нет, порт не проверялся. Это **не** «локация
                          мертва»: порт может пускать, и уже выданные
                          конфиги работают.
``нечего проверять``      ни адреса для клиента, ни сервиса подписок, ни
                          сверки ID (заглушка панели, пустая запись) —
                          судить не о чем.
``порт не пускает``       панель ответила, TCP-инбаунды есть, но **ни один**
                          порт не принял соединение: клиент не подключится.
``конфиг не выдаётся``    порт пускает, но адреса подписок нет / сервис
                          молчит / конфигов нет: локация молча выпадает из
                          подписки клиента.
``настройка разошлась``   панель и порт в порядке, но ``inbound_ids`` не
                          совпадают с панелью: новые клиенты не создаются.
``работает``              все слои сошлись.
========================  ==================================================

Запуск на сервере (там боевой ``.env`` и ноды в БД)::

    .venv/bin/python -m app.tools.location_audit
    .venv/bin/python -m app.tools.location_audit --repeat 3 --interval 5
    .venv/bin/python -m app.tools.location_audit --json
    .venv/bin/python -m app.tools.location_audit --dry-run

Почему ``--repeat``: панель и порт умеют отвечать через раз, и однократная
проверка врёт в обе стороны. Инструмент печатает долю успехов по каждому слою
(``3/3``; ``1/3`` — «через раз») и не хоронит локацию из-за одной неудачной
попытки. Повторяются сетевые слои — панель и порты; сверка ID берётся из одного
прогона :mod:`app.tools.check_nodes`, чтобы диагноз совпадал с админкой.

Код возврата 1, если хотя бы одна локация не «работает»: годится для дежурной
проверки в скрипте. Вывод ``--json`` можно целиком переслать в чат — UUID
клиентов в конфигах маскируются, пароли из адресов вырезаются.

Как проверить. Локально (на заглушке панели)::

    .venv/bin/python -m pytest tests/test_location_audit.py -q
    .venv/bin/python -m app.tools.location_audit --dry-run

На сервере — ``--repeat 3 --interval 5 --json``; как читать вывод и что делать
по каждому вердикту — ``docs/РАЗБОР-ЛОКАЦИЯ-НЕ-РАБОТАЕТ.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from app.panels.base import panel_label, parse_inbound_ids

#: Вердикты локации. Строки — часть интерфейса: их ищут глазами в отчёте и
#: грепом в логах, поэтому не сокращаем их и не переводим «на лету».
VERDICT_OK = "работает"
VERDICT_PANEL_DOWN = "панель не отвечает"
VERDICT_PORT_CLOSED = "порт не пускает"
VERDICT_NO_CONFIGS = "конфиг не выдаётся"
VERDICT_IDS_MISMATCH = "настройка разошлась"
VERDICT_NOTHING = "нечего проверять"

#: Значки вердиктов: 🔴 — клиент не подключится, 🟠 — подключится, но что-то
#: сломано (или сказать нечего про панель), ⚪ — данных нет.
VERDICT_MARKS = {
    VERDICT_OK: "✅",
    VERDICT_PANEL_DOWN: "🟠",
    VERDICT_PORT_CLOSED: "🔴",
    VERDICT_NO_CONFIGS: "🔴",
    VERDICT_IDS_MISMATCH: "🟠",
    VERDICT_NOTHING: "⚪",
}

#: Сколько клиентов панели пробовать, прежде чем признать, что конфигов нет.
#: Один клиент может быть без профиля на этой локации (выдан до её появления),
#: и «локация не выдаётся» по нему было бы ложным.
CLIENTS_TO_TRY = 3

#: Заглушка вместо subId для проверки живости сервиса подписок: клиентов на
#: новой локации может не быть вовсе, а ответ сервиса (обычно 404) доказывает,
#: что адрес рабочий и подписка вообще отдаётся.
SUB_PLACEHOLDER = "0" * 16

#: Сколько текстов ошибок показывать по слою: панель может отвечать разным на
#: каждую попытку, но десять строк никто не читает.
ERRORS_SHOWN = 3

#: Локальные адреса: по ним видно только сам сервис, но не клиента.
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


# --------------------------------------------------------------- мелкие утилиты
def _short(text: str, limit: int = 240) -> str:
    """Обрезать длинный текст ошибки: отчёт должен читаться с телефона."""
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _safe_url(url: str) -> str:
    """Адрес без логина/пароля: отчёт уходит в чат, а в URL бывают креды."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    if "@" not in parts.netloc:
        return raw
    return parts._replace(netloc=parts.netloc.rsplit("@", 1)[-1]).geturl()


def _url_host(url: str) -> str:
    """Хост из адреса: «http://1.2.3.4:2096/sub/» → «1.2.3.4»."""
    try:
        return str(urlsplit((url or "").strip()).hostname or "")
    except ValueError:
        return ""


def _authority_host(authority: str) -> str:
    """Хост из «host:port» (IPv6 в скобках тоже)."""
    value = (authority or "").strip()
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    return value.rsplit(":", 1)[0] if ":" in value else value


def _config_host(config: str) -> str:
    """«host:port» из готовой строки конфига: ``vless://uuid@HOST:PORT?…#имя``."""
    body = (config or "").split("://", 1)[-1]
    if "@" not in body:
        return ""
    after = body.split("@", 1)[1]
    return after.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip()


def _config_hosts(configs: list[str]) -> list[str]:
    """Уникальные адреса из конфигов — то, куда реально пойдёт клиент."""
    result: list[str] = []
    for config in configs:
        host = _config_host(config)
        if host and host not in result:
            result.append(host)
    return result


def _mask_config(config: str) -> str:
    """Строка конфига без UUID клиента: ``vless://…@150.241.106.75:443``.

    UUID — это доступ: по нему клиента можно подключить к нашей ноде. В отчёте
    он не нужен ни оператору, ни чату, поэтому оставляем только схему и адрес.
    """
    scheme = (config or "").split("://", 1)[0]
    host = _config_host(config)
    if not scheme or not host:
        return ""
    return f"{scheme}://…@{host}"


def _is_local_host(host: str) -> bool:
    """Локальный адрес сервиса, а не адрес клиента: проба порта ничего не скажет."""
    return (host or "").strip().lower() in LOCAL_HOSTS


def _configs_word(count: int) -> str:
    """«1 конфиг / 2 конфига / 5 конфигов»: отчёт читает человек, а не парсер."""
    if count % 10 == 1 and count % 100 != 11:
        return "конфиг"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "конфига"
    return "конфигов"


def _inbound_row(inbound: object) -> tuple[int, str, str, int, str]:
    """Строка таблицы инбаундов: ID, имя, протокол, порт, транспорт."""
    return (
        int(getattr(inbound, "id", 0) or 0),
        str(getattr(inbound, "remark", "") or ""),
        str(getattr(inbound, "protocol", "") or ""),
        int(getattr(inbound, "port", 0) or 0),
        str(getattr(inbound, "network", "") or ""),
    )


def _inbound_text(row: tuple[int, str, str, int, str]) -> str:
    inbound_id, remark, protocol, port, network = row
    label = f"«{remark}» " if remark else ""
    return f"{inbound_id} {label}{protocol}:{port}{' ' + network if network else ''}"


def _edit_hint(code: str) -> str:
    """Куда идти править локацию: у ноды — карточка в админке, у основной — .env.

    Без этого подсказка «/admin/nodes?edit=primary» ведёт в никуда: основной
    панели в таблице нод нет, её настройки живут в ``.env``.
    """
    if code == "primary":
        return "`.env` основной панели (PANEL_URL, PANEL_INBOUND_IDS, PANEL_SUB_BASE)"
    return f"/admin/nodes?edit={code}"


# ------------------------------------------------------------------- датаклассы
@dataclass(slots=True)
class AttemptStat:
    """Сколько раз слой проверили и сколько раз он ответил.

    Нужен ради «через раз»: панель может ответить один раз из трёх, и
    однократная проверка соврёт в любую сторону. Доля успехов печатается рядом
    с состоянием слоя, а локация не хоронится из-за одной неудачной попытки.
    """

    attempts: int = 0
    successes: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def add(self, ok: bool, *, error: str = "", seconds: float = 0.0) -> None:
        self.attempts += 1
        self.successes += 1 if ok else 0
        self.seconds += max(0.0, seconds)
        clean = _short(error)
        if clean and clean not in self.errors:
            self.errors.append(clean)

    @property
    def ok(self) -> bool:
        """Слой хоть раз ответил: этого достаточно, чтобы проверять дальше."""
        return self.successes > 0

    @property
    def flaky(self) -> bool:
        """Отвечает через раз — самый неприятный режим: вранье в обе стороны."""
        return 0 < self.successes < self.attempts

    @property
    def rate(self) -> str:
        return f"{self.successes}/{self.attempts}"

    def as_dict(self) -> dict:
        return {
            "attempts": self.attempts,
            "successes": self.successes,
            "rate": self.rate,
            "seconds": round(self.seconds, 2),
            "errors": list(self.errors),
        }


@dataclass(slots=True)
class PortReport:
    """Один TCP-инбаунд: пускает ли клиента именно этот порт."""

    port: int
    remark: str = ""
    protocol: str = ""
    stat: AttemptStat = field(default_factory=AttemptStat)
    ms: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.stat.ok

    def as_text(self) -> str:
        label = f" «{self.remark}»" if self.remark else ""
        proto = f" {self.protocol}" if self.protocol else ""
        if not self.ok:
            return f"  порт {self.port}{label}{proto}: ❌ не пускает {self.stat.rate} · {self.detail or 'нет ответа'}"
        state = f"✅ открыт {self.stat.rate}"
        if self.stat.flaky:
            state = f"⚠️ открыт через раз {self.stat.rate}"
        ms = f" · {self.ms} мс" if self.ms else ""
        return f"  порт {self.port}{label}{proto}: {state}{ms}"

    def as_dict(self) -> dict:
        return {
            "port": self.port,
            "remark": self.remark,
            "protocol": self.protocol,
            "ok": self.ok,
            "ms": self.ms,
            "detail": self.detail,
            **self.stat.as_dict(),
        }


@dataclass(slots=True)
class PortsResult:
    """Итог слоя «порт клиента»: строки отчёта и что узнали про инбаунды."""

    reports: list[PortReport] = field(default_factory=list)
    note: str = ""
    inbounds: list[tuple[int, str, str, int, str]] = field(default_factory=list)


@dataclass(slots=True)
class SubReport:
    """Публичный сервис подписок: адрес, доступность и что внутри конфигов."""

    base: str = ""
    configured: bool = False
    checked: bool = False
    skipped: bool = False
    reachable: bool = False
    http_status: int = 0
    configs: int = 0
    clients_checked: int = 0
    hosts: list[str] = field(default_factory=list)
    sample: str = ""
    error: str = ""
    errors: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def ok(self) -> bool:
        """Конфиги реально выдаются: адрес задан, сервис ответил, строки есть."""
        return self.configured and self.checked and self.reachable and not self.error and self.configs > 0

    @property
    def failed(self) -> bool:
        """Клиент не получит локацию: адреса нет, сервис молчит или строк нет.

        Отдельно от :attr:`ok`: «нет клиентов, чтобы проверить выдачу» — это не
        поломка (новая локация), а честное «не проверено».
        """
        if self.skipped:
            return False
        if not self.configured or self.error:
            return True
        return bool(self.clients_checked) and self.configs == 0

    def as_text(self) -> str:
        if self.skipped:
            return self.note
        if not self.configured:
            return f"❌ {self.error or 'адрес сервиса подписок не задан'}"
        where = f"{self.base} · HTTP {self.http_status}" if self.reachable else f"{self.base} · ❌ не отвечает"
        parts = [where]
        if self.configs:
            parts.append(f"{self.configs} {_configs_word(self.configs)}")
        if self.hosts:
            parts.append("host в конфигах: " + ", ".join(self.hosts))
        elif self.sample:
            parts.append(f"образец: {self.sample}")
        if self.error:
            parts.append(f"❌ {self.error}")
        if self.note:
            parts.append(f"ℹ️ {self.note}")
        return " · ".join(parts)

    def as_dict(self) -> dict:
        return {
            "base": self.base,
            "configured": self.configured,
            "checked": self.checked,
            "skipped": self.skipped,
            "reachable": self.reachable,
            "http_status": self.http_status,
            "configs": self.configs,
            "clients_checked": self.clients_checked,
            "hosts": list(self.hosts),
            "sample": self.sample,
            "ok": self.ok,
            "failed": self.failed,
            "error": self.error,
            "errors": list(self.errors),
            "note": self.note,
        }


@dataclass(slots=True)
class LocationReport:
    """Одна локация: доказательства по слоям, вердикт и что делать."""

    code: str
    title: str = ""
    host: str = ""
    host_source: str = ""
    panel_kind: str = ""
    panel_url: str = ""
    configured_ids: list[int] = field(default_factory=list)
    actual_ids: list[int] = field(default_factory=list)
    missing_ids: list[int] = field(default_factory=list)
    extra_ids: list[int] = field(default_factory=list)
    inbounds: list[tuple[int, str, str, int, str]] = field(default_factory=list)
    id_note: str = ""
    #: Сверка ID вообще применима к этой панели (у заглушки — нет).
    ids_comparable: bool = False
    #: Сверка выполнена: панель ответила и список инбаундов получен.
    ids_checked: bool = False
    panel: AttemptStat = field(default_factory=AttemptStat)
    ports: list[PortReport] = field(default_factory=list)
    ports_note: str = ""
    sub: SubReport = field(default_factory=SubReport)
    verdict: str = ""
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    dry_run: bool = False

    def headline(self) -> str:
        where = self.host or "хост не задан"
        if self.host and self.host_source:
            where += f" ({self.host_source})"
        head = f"{self.title or self.code} ({self.code}) · {where}"
        if self.panel_url:
            head += f" · панель {_safe_url(self.panel_url)}"
        if self.panel_kind:
            head += f" [{self.panel_kind}]"
        return head

    def as_text(self) -> str:
        lines = [self.headline()]
        if self.dry_run:
            return "\n".join(lines + self._plan_lines())

        if self.panel.attempts:
            mark = VERDICT_MARKS[VERDICT_OK] if self.panel.ok else "❌"
            state = "отвечает" if self.panel.ok else "не отвечает"
            if self.panel.flaky:
                state = "отвечает через раз"
            lines.append(f"  панель:     {mark} {state} {self.panel.rate} · {self.panel.seconds:.1f} с")
            for error in self.panel.errors[:ERRORS_SHOWN]:
                lines.append(f"              └ {error}")

        if self.inbounds:
            lines.append("  инбаунды:   " + " · ".join(_inbound_text(row) for row in self.inbounds))
        elif self.panel.ok:
            lines.append("  инбаунды:   пусто")
        else:
            lines.append("  инбаунды:   не получены — панель не ответила")

        configured = ", ".join(str(item) for item in self.configured_ids) or "— (все инбаунды)"
        if self.ids_checked:
            actual = ", ".join(str(item) for item in self.actual_ids) or "пусто"
            if self.missing_ids:
                state = "❌ нет настроенных ID: " + ", ".join(str(item) for item in self.missing_ids)
            else:
                state = "✅ сходятся"
            lines.append(f"  ID:         настроено {configured} · в панели {actual} · {state}")
        else:
            lines.append(f"  ID:         настроено {configured} · {self.id_note or 'сверка не выполнялась'}")

        for port in self.ports:
            lines.append(port.as_text())
        if self.ports_note:
            lines.append(f"  порты:      {self.ports_note}")
        lines.append(f"  подписка:   {self.sub.as_text()}")

        lines.append(f"  вердикт:    {VERDICT_MARKS.get(self.verdict, '')} {self.verdict}".rstrip())
        for reason in self.reasons:
            lines.append(f"              → {reason}")
        for warning in self.warnings:
            lines.append(f"              ⚠️ {warning}")
        for action in self.actions:
            lines.append(f"              что делать: {action}")
        return "\n".join(lines)

    def _plan_lines(self) -> list[str]:
        """Строки ``--dry-run``: что будет проверено, без единого запроса в сеть."""
        kind = self.panel_kind or "—"
        lines = [
            "  dry-run:    сеть не трогали — ниже то, что будет проверено",
            f"  панель:     {_safe_url(self.panel_url) or '— адрес не задан'} [{kind}]",
            f"  порты:      TCP-инбаунды на {self.host or '— (host не задан)'}",
            f"  подписка:   {self.sub.base or '— адрес сервиса подписок не задан'}",
        ]
        configured = ", ".join(str(item) for item in self.configured_ids) or "— (все инбаунды)"
        lines.append(f"  ID:         настроено {configured}; сверка с панелью — в обычном прогоне")
        return lines

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "title": self.title,
            "host": self.host,
            "host_source": self.host_source,
            "panel_kind": self.panel_kind,
            "panel_url": _safe_url(self.panel_url),
            "verdict": self.verdict,
            "configured_ids": list(self.configured_ids),
            "actual_ids": list(self.actual_ids),
            "missing_ids": list(self.missing_ids),
            "extra_ids": list(self.extra_ids),
            "ids_comparable": self.ids_comparable,
            "ids_checked": self.ids_checked,
            "id_note": self.id_note,
            "panel": self.panel.as_dict(),
            "inbounds": [
                {"id": item[0], "remark": item[1], "protocol": item[2], "port": item[3], "network": item[4]}
                for item in self.inbounds
            ],
            "ports": [port.as_dict() for port in self.ports],
            "ports_note": self.ports_note,
            "subscription": self.sub.as_dict(),
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "actions": list(self.actions),
        }


@dataclass(slots=True)
class Report:
    """Итог аудита: локации, предупреждения о дублях и общий вердикт."""

    locations: list[LocationReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    repeat: int = 1
    dry_run: bool = False
    checked_at: str = ""

    @property
    def broken(self) -> list[LocationReport]:
        return [item for item in self.locations if item.verdict != VERDICT_OK]

    @property
    def ok(self) -> bool:
        if self.dry_run:
            return True  # проверок не было — и проблем не нашли
        return bool(self.locations) and not self.broken

    def summary_text(self) -> str:
        """Итог и что делать: одна строка на причину, без пересказа отчёта."""
        if self.dry_run:
            return "Итог: проверок не было (--dry-run) — запусти без флага, чтобы проверить локации."
        if not self.locations:
            return "Итог: локаций нет — добавь ноду в /admin/nodes и включи её."
        if not self.broken:
            return f"Итог: ✅ все локации работают ({len(self.locations)})"
        if all(item.verdict == VERDICT_NOTHING for item in self.broken):
            # «Нечего проверять» — это не поломка, а отсутствие данных: писать
            # «не работают» было бы таким же враньём, как зелёное «работает».
            return (
                f"Итог: ⚪ подтвердить нечем — {len(self.broken)} из {len(self.locations)} локаций "
                "без адреса клиента и сервиса подписок (см. строки выше)"
            )
        head = f"Итог: ⚠️ не работают {len(self.broken)} из {len(self.locations)} — " + " · ".join(
            f"{item.code}: {item.verdict}" for item in self.broken
        )
        steps = [f"{item.code}: {action}" for item in self.broken for action in item.actions]
        if steps:
            head += "\nЧто делать:\n" + "\n".join(f"— {step}" for step in steps)
        return head

    def as_text(self) -> str:
        head = f"🩺 Аудит локаций · {self.checked_at}"
        head += " · dry-run (сеть не трогали)" if self.dry_run else f" · попыток на слой: {self.repeat}"
        blocks = [item.as_text() for item in self.locations] or ["локаций нет"]
        parts = [head, "", "\n\n".join(blocks)]
        if self.warnings:
            parts.append("\n".join(f"⚠️ {item}" for item in self.warnings))
        parts.append(self.summary_text())
        return "\n".join(parts)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at,
            "repeat": self.repeat,
            "dry_run": self.dry_run,
            "summary": self.summary_text(),
            "locations": [item.as_dict() for item in self.locations],
            "warnings": list(self.warnings),
        }


# -------------------------------------------------------------------- проверки
async def _health_once(panel) -> tuple[bool, str, float]:  # noqa: ANN001 - панель из реестра
    """Одна проверка панели: ``(отвечает, текст ошибки, секунды)``.

    ``check_health`` умеет отдавать причину (таймаут, 401, «нет маршрута»), а
    ``health`` — только ``bool``; для чужих панелей без ``check_health``
    оставляем запасной путь, как это делает проверка нод.
    """
    started = time.perf_counter()
    try:
        checker = getattr(panel, "check_health", None)
        if callable(checker):
            ok, error = await checker()
        else:  # pragma: no cover - панель без check_health
            ok, error = bool(await panel.health()), ""
    except Exception as exc:  # noqa: BLE001 - чужая панель отвечает чем угодно
        ok, error = False, str(exc)
    return bool(ok), str(error or ""), time.perf_counter() - started


async def _list_all_inbounds(panel) -> list:  # noqa: ANN001 - панель из реестра
    """Сырой список инбаундов: фильтрованный падает как раз при расхождении ID."""
    lister = getattr(panel, "list_all_inbounds", None) or panel.list_inbounds
    return list(await lister())


async def _check_panel(panel, *, repeat: int, interval: float) -> AttemptStat:
    """Слой 1: отвечает ли панель — ``repeat`` попыток подряд с замером времени."""
    stat = AttemptStat()
    for attempt in range(1, repeat + 1):
        if attempt > 1 and interval > 0:
            await asyncio.sleep(interval)
        ok, error, seconds = await _health_once(panel)
        stat.add(ok, error=error, seconds=seconds)
    return stat


async def _check_ports(
    panel,  # noqa: ANN001 - панель из реестра
    host: str,
    *,
    repeat: int,
    interval: float,
    timeout: float | None,
) -> PortsResult:
    """Слой 3: постучаться в каждый TCP-инбаунд панели.

    Проверяем **все** порты, а не «первый успешный» (так делает фоновая проба):
    мёртвый запасной порт — это тоже «локация работает не полностью», и по
    одному удачному порту его не увидеть. Список инбаундов обновляем каждую
    попытку: панель может отдать его не с первого раза.
    """
    from app.services import probe as probe_service

    result = PortsResult()
    if not host:
        result.note = "не проверялся: у локации не задан хост — клиенту стучаться некуда"
        return result
    if _is_local_host(host):
        result.note = (
            f"не проверялся: {host} — локальный адрес сервиса, а не адрес клиента; "
            "заполни host ноды (или PANEL_SUB_BASE у основной панели)"
        )
        return result

    reports: dict[int, PortReport] = {}
    last_error = ""
    for attempt in range(1, repeat + 1):
        if attempt > 1 and interval > 0:
            await asyncio.sleep(interval)
        try:
            inbounds = list(await _list_all_inbounds(panel))
        except Exception as exc:  # noqa: BLE001 - панель отвечает чем угодно
            last_error = str(exc)
            continue
        if not result.inbounds:
            result.inbounds = [_inbound_row(inbound) for inbound in inbounds]
        protocols = {
            int(getattr(inbound, "port", 0) or 0): str(getattr(inbound, "protocol", "") or "")
            for inbound in inbounds
        }
        for target in probe_service.probe_targets(inbounds, host):
            item = reports.get(target.port)
            if item is None:
                item = PortReport(
                    port=target.port,
                    remark=target.label,
                    protocol=protocols.get(target.port, ""),
                )
                reports[target.port] = item
            outcome = await probe_service.probe_endpoint(target.host, target.port, timeout=timeout)
            item.stat.add(outcome.ok, error="" if outcome.ok else (outcome.detail or outcome.stage))
            if outcome.ok:
                item.ms = int(outcome.ms or 0)
                item.detail = outcome.detail
            elif not item.detail:
                item.detail = outcome.detail or outcome.stage

    result.reports = list(reports.values())
    if not result.reports:
        if last_error:
            result.note = f"не проверялся: панель не отдала список инбаундов ({_short(last_error)})"
        elif result.inbounds:
            result.note = "не проверялся: TCP-инбаундов в панели нет (только UDP — TCP-проба неприменима)"
        else:
            result.note = "не проверялся: инбаундов в панели нет"
    return result


def _sub_base(node, panel) -> str:  # noqa: ANN001 - нода или None
    """Адрес сервиса подписок локации: у ноды — из БД, у основной — из настроек."""
    if node is not None:
        return str(getattr(node, "subscription_base", "") or "")
    return str(getattr(panel, "sub_base", "") or "")


async def _probe_sub_service(base: str, timeout: float | None = None) -> tuple[bool, int, str]:
    """Жив ли сервис подписок: ответ на запрос несуществующего клиента.

    Отдельная проба нужна потому, что ``panel.get_configs`` требует настоящего
    клиента, а на новой локации клиентов может не быть вовсе. Любой ответ
    сервиса (обычно 404) доказывает, что адрес рабочий и подписка отдаётся;
    отказ соединения или таймаут — что не отдастся никому.
    """
    import httpx

    url = f"{(base or '').rstrip('/')}/{SUB_PLACEHOLDER}"
    try:
        async with httpx.AsyncClient(
            timeout=float(timeout or 5.0), follow_redirects=False, verify=False
        ) as client:  # noqa: S501 - свой сертификат: важен ответ, а не цепочка доверия
            response = await client.get(url)
    except Exception as exc:  # noqa: BLE001 - сеть отвечает чем угодно
        return False, 0, _short(f"сервис подписок не ответил ({url}): {exc}")
    return True, int(response.status_code), ""


async def _check_sub(
    panel,  # noqa: ANN001 - панель из реестра
    *,
    node,  # noqa: ANN001 - нода или None
    kind: str,
    panel_ok: bool,
    timeout: float | None,
) -> SubReport:
    """Слой 4: отдаёт ли локация конфиги клиенту и с каким адресом внутри.

    Заглушку панели не проверяем вовсе: у ``fake`` адрес подписок локальный, и
    «сервис не отвечает» было бы выдумкой — ровно как со сверкой ID в
    :mod:`app.tools.check_nodes`.
    """
    item = SubReport()
    if kind != "xui":
        item.skipped = True
        item.note = (
            f"панель типа «{kind or '—'}» (заглушка) — сервис подписок не проверяется; "
            "в проде PANEL_TYPE=xui"
        )
        return item

    base = _sub_base(node, panel)
    item.base = _safe_url(base)
    if not base:
        item.error = "не задан адрес сервиса подписок (sub_base): конфиги этой локации собрать нечем"
        return item

    item.configured = True
    item.checked = True
    reachable, status, error = await _probe_sub_service(base, timeout)
    item.reachable = reachable
    item.http_status = status
    if error:
        item.error = error
        return item

    if not panel_ok:
        item.note = "конфиги не проверялись: панель не ответила (сервис подписок при этом отвечает)"
        return item

    try:
        users = [user for user in await panel.list_users() if str(getattr(user, "uuid", "") or "")]
    except Exception as exc:  # noqa: BLE001 - панель может не уметь список клиентов
        users = []
        item.errors.append(_short(f"список клиентов панели не получен: {exc}"))
    if not users:
        item.note = (
            f"клиентов на локации нет — выдачу проверить нечем; сервис подписок ответил HTTP {status}, "
            "адрес рабочий"
        )
        return item

    for user in users[:CLIENTS_TO_TRY]:
        item.clients_checked += 1
        try:
            configs = [str(config) for config in await panel.get_configs(str(user.uuid))]
        except Exception as exc:  # noqa: BLE001 - PanelError и ошибки чужих панелей
            item.errors.append(_short(str(exc)))
            continue
        item.configs = len(configs)
        item.hosts = _config_hosts(configs)
        item.sample = _mask_config(configs[0]) if configs else ""
        break

    if item.configs == 0:
        item.error = (
            f"конфиги не получены ни у одного из {item.clients_checked} клиентов локации: "
            f"{item.errors[-1] if item.errors else 'сервис вернул пусто'}"
        )
    return item


def _resolve_host(node, sub_base: str, *, kind: str) -> tuple[str, str]:  # noqa: ANN001 - нода или None
    """Адрес, по которому стучится клиент, и откуда он взят.

    У ноды это ``host``. У основной панели отдельного поля нет: берём хост из
    адреса сервиса подписок (``PANEL_SUB_BASE``) — подписка отдаётся с того же
    адреса, куда клиент подключается. Пусто — порт проверить нечем, и об этом
    честно пишем, а не выдумываем «не пускает».

    У заглушки панели адрес подписок локальный (``FakePanel`` придумывает его
    сам), поэтому брать оттуда host нельзя: получилась бы проверка порта на
    самом сервисе.
    """
    if node is not None and str(getattr(node, "host", "") or "").strip():
        return str(node.host).strip(), "nodes.host"
    if kind != "xui":
        return "", ""
    host = _url_host(sub_base)
    if host:
        return host, "sub_base"
    return "", ""


def _evaluate(item: LocationReport) -> None:
    """Проставить вердикт, доказательства и действие.

    Порядок — по клиентскому пути: сначала «дошёл ли клиент до порта», потом
    «получил ли конфиг», и только потом настройки. Разошедшиеся ID при закрытом
    порте — не то, что нужно чинить первым.
    """
    item.reasons.clear()
    item.warnings.clear()
    item.actions.clear()

    # Предупреждения не зависят от вердикта: «через раз» и «часть портов мертва»
    # важно видеть и тогда, когда локация в остальном работает.
    if item.panel.attempts > 1 and item.panel.flaky:
        item.warnings.append(
            f"панель отвечает через раз ({item.panel.rate}) — однократная проверка соврёт в любую сторону"
        )
    if item.ids_checked and item.extra_ids:
        item.warnings.append(
            "в панели есть инбаунды вне настроек: "
            + ", ".join(str(value) for value in item.extra_ids)
            + " — их локации клиенты не получат"
        )
    dead = [port for port in item.ports if not port.ok]
    if item.ports and dead and len(dead) < len(item.ports):
        item.warnings.append(
            "часть портов не пускает: "
            + ", ".join(str(port.port) for port in dead)
            + " — клиент может подключиться на остальных, но запасной канал мёртв"
        )
    flaky_ports = [port for port in item.ports if port.stat.flaky and port.ok]
    if flaky_ports:
        item.warnings.append(
            "порт отвечает через раз: "
            + ", ".join(f"{port.port} ({port.stat.rate})" for port in flaky_ports)
        )
    if item.sub.hosts and item.host:
        wrong = [value for value in item.sub.hosts if _authority_host(value) != item.host]
        if wrong:
            item.warnings.append(
                "в конфигах адрес " + ", ".join(wrong) + f", а локация проверялась по {item.host} — "
                "клиент пойдёт не туда, куда смотрела проверка (сверь shareAddr инбаунда)"
            )

    if item.panel.attempts and not item.panel.ok:
        first = item.panel.errors[0] if item.panel.errors else "панель не ответила"
        item.verdict = VERDICT_PANEL_DOWN
        item.reasons.append(
            f"панель не ответила ни разу ({item.panel.rate} попыток за {item.panel.seconds:.1f} с): {first}"
        )
        if any(port.ok for port in item.ports):
            ports = ", ".join(str(port.port) for port in item.ports if port.ok)
            item.reasons.append(
                f"порт {ports} при этом клиента пускает — это НЕ «локация мертва»: "
                "уже выданные конфиги могут работать, перевыдавать ничего не нужно"
            )
        elif item.ports:
            item.reasons.append("и порты не пускают: причина не одна, начни с панели")
        else:
            item.reasons.append("список инбаундов получить не удалось — порт не проверялся")
        item.actions.append(
            "подними панель ноды (на её сервере — `systemctl status x-ui`, затем "
            f"`journalctl -u x-ui -n 50`); токен и адрес панели — {_edit_hint(item.code)}"
        )
        return

    if not item.host and not item.sub.configured and not item.ids_comparable:
        item.verdict = VERDICT_NOTHING
        item.reasons.append(
            "нет ни адреса для клиента (host), ни сервиса подписок, ни сверки ID "
            "(панель-заглушка) — судить о локации нечем"
        )
        item.actions.append(
            f"заполни адрес ноды и адрес сервиса подписок: {_edit_hint(item.code)} → запусти аудит снова"
        )
        return

    if item.ports and all(not port.ok for port in item.ports):
        item.verdict = VERDICT_PORT_CLOSED
        closed = ", ".join(str(port.port) for port in item.ports)
        item.reasons.append(
            f"панель ответила ({item.panel.rate}), но ни один TCP-порт не принял соединение: {closed}"
        )
        for port in item.ports[:ERRORS_SHOWN]:
            item.reasons.append(f"порт {port.port}: {port.detail or 'нет ответа'}")
        item.actions.append(
            f"проверь фаервол и порты ноды из сервиса: "
            f"`.venv/bin/python -m app.tools.ping_node --host {item.host} --port {item.ports[0].port}`"
        )
        if item.ids_checked and not item.missing_ids:
            item.actions.append(
                "конфиги у клиентов уже выданы — после починки порта перевыдавать ничего не нужно"
            )
        return

    if item.sub.failed:
        item.verdict = VERDICT_NO_CONFIGS
        if not item.sub.configured:
            item.reasons.append(
                "не задан адрес сервиса подписок (sub_base): локация молча выпадает из подписки клиента, "
                "ошибки при этом нигде не видно"
            )
            item.actions.append(
                f"впиши «Адрес сервиса подписок» (и адрес ноды) — {_edit_hint(item.code)} → сохранить"
            )
        else:
            item.reasons.append(f"сервис подписок {item.sub.base}: {item.sub.error}")
            if item.sub.clients_checked:
                item.reasons.append(
                    f"профиля нет ни у одного из {item.sub.clients_checked} проверенных клиентов локации"
                )
                item.actions.append(
                    "досоздай клиентов в инбаундах локации: "
                    "`.venv/bin/python -m app.tools.sync_inbounds --apply`"
                )
            else:
                item.actions.append(
                    f"проверь сервис подписок: `curl -sS {item.sub.base}<subId>` должен вернуть "
                    "base64-конфиги; на ноде это порт сервиса подписок 3x-ui (обычно 2096)"
                )
        return

    if item.ids_checked and item.missing_ids:
        item.verdict = VERDICT_IDS_MISMATCH
        item.reasons.append(
            "настроенные ID отсутствуют в панели: "
            + ", ".join(str(value) for value in item.missing_ids)
            + " — новые клиенты на локацию не создаются (действующие подписки не страдают)"
        )
        if item.actual_ids:
            actual = ",".join(str(value) for value in item.actual_ids)
            item.actions.append(
                f"впиши фактические ID «{actual}» — {_edit_hint(item.code)} → сохранить → «Проверить ноды»"
            )
        else:
            item.actions.append(
                f"в панели локации нет ни одного инбаунда — создай его в 3x-ui и впиши ID: {_edit_hint(item.code)}"
            )
        item.actions.append(
            "действующим клиентам досоздай профили на новых инбаундах: "
            "`.venv/bin/python -m app.tools.sync_inbounds --apply`"
        )
        return

    item.verdict = VERDICT_OK
    confirmed = [f"панель {item.panel.rate}"]
    working = [port for port in item.ports if port.ok]
    if working:
        confirmed.append("порты " + ", ".join(f"{port.port} ({port.ms} мс)" for port in working))
    if item.sub.configs:
        confirmed.append(f"{item.sub.configs} {_configs_word(item.sub.configs)}")
    if item.ids_checked:
        confirmed.append("ID сходятся")
    item.reasons.append("подтверждено: " + " · ".join(confirmed))
    if item.ports_note:
        item.reasons.append(item.ports_note)
    if item.sub.note:
        item.reasons.append(item.sub.note)


async def _check_location(
    node,  # noqa: ANN001 - нода из БД или None для основной панели
    panel,  # noqa: ANN001 - клиент панели из реестра
    settings,  # noqa: ANN001 - настройки приложения
    ids,  # noqa: ANN001 - готовый отчёт check_nodes по этой панели или None
    *,
    repeat: int,
    interval: float,
    timeout: float | None,
    dry_run: bool,
) -> LocationReport:
    """Пройти по слоям одной локации и собрать отчёт с доказательствами."""
    code = node.code if node is not None else "primary"
    kind = str((node.panel_type if node is not None else settings.panel_type) or "").strip().lower()
    sub_base = _sub_base(node, panel)
    host, host_source = _resolve_host(node, sub_base, kind=kind)
    if node is not None:
        panel_url = str(node.panel_url or "") or str(getattr(panel, "base_url", "") or "")
        configured = parse_inbound_ids(node.inbound_ids)
    else:
        panel_url = str(getattr(panel, "base_url", "") or "")
        configured = list(settings.inbound_id_list)

    item = LocationReport(
        code=code,
        title=str((node.title if node is not None else "") or panel_label(panel) or code),
        host=host,
        host_source=host_source,
        panel_kind=kind or "—",
        panel_url=panel_url,
        configured_ids=configured,
        ids_comparable=kind == "xui",
        dry_run=dry_run,
    )
    item.sub.base = _safe_url(sub_base) if kind == "xui" else ""
    if ids is not None:
        item.configured_ids = list(ids.configured)
        item.actual_ids = list(ids.actual_ids)
        item.missing_ids = list(ids.missing)
        item.extra_ids = list(ids.extra)
        item.ids_checked = bool(ids.ok and item.ids_comparable)
        if not item.ids_checked:
            item.id_note = ids.note or (
                "панель не отдала список инбаундов"
                if not ids.ok
                else f"панель типа «{kind or '—'}» не применяет inbound_ids — сверка пропущена"
            )
        item.inbounds = (
            [(inbound_id, remark, protocol, port, "") for inbound_id, remark, protocol, port in ids.actual]
            if ids.ok
            else []
        )
    elif dry_run:
        item.id_note = "сверка не выполнялась (--dry-run)"

    if dry_run:
        return item

    item.panel = await _check_panel(panel, repeat=repeat, interval=interval)
    ports = await _check_ports(panel, host, repeat=repeat, interval=interval, timeout=timeout)
    item.ports = ports.reports
    item.ports_note = ports.note
    if ports.inbounds:
        # Свежий список из слоя порта (с транспортом): он точнее для таблицы.
        item.inbounds = ports.inbounds
    item.sub = await _check_sub(panel, node=node, kind=kind, panel_ok=item.panel.ok, timeout=timeout)
    _evaluate(item)
    return item


async def collect(
    session=None,  # noqa: ANN001 - AsyncSession, но тип не тянем в CLI
    *,
    repeat: int = 1,
    interval: float = 0.0,
    timeout: float | None = None,
    dry_run: bool = False,
) -> Report:
    """Собрать отчёт по всем локациям: основная панель + активные ноды.

    Сверку ID не дублируем: берём готовый отчёт :func:`app.tools.check_nodes.collect`
    — тот же код, что у бота и админки. Иначе диагноз инструмента разошёлся бы с
    тем, что оператор видит на ``/admin/nodes``, и чинить пришлось бы два раза.
    """
    from app.config import get_settings
    from app.db.session import SessionMaker, init_db
    from app.panels.registry import registry
    from app.tools import check_nodes

    settings = get_settings()
    if session is None:
        # Инструмент запускают руками на сервере: пусть сам дотянет колонки,
        # иначе на старой базе будет «no such column».
        await init_db()
        async with SessionMaker() as own_session:
            return await collect(
                own_session, repeat=repeat, interval=interval, timeout=timeout, dry_run=dry_run
            )

    pairs = await registry.all_panels_with_nodes(session)
    #: В dry-run в сеть не ходим вовсе — значит и отчёт check_nodes не собираем.
    ids_report = None if dry_run else await check_nodes.collect(session)
    by_code = {panel.code: panel for panel in (ids_report.panels if ids_report is not None else [])}

    report = Report(
        repeat=repeat,
        dry_run=dry_run,
        checked_at=datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC"),
    )
    if ids_report is not None:
        report.warnings.extend(ids_report.warnings)
    if not dry_run and not settings.node_probe_enabled:
        # Пробы выключены настройкой: инструмент всё равно проверит порты руками,
        # но на экранах состояния порта не будет — и это надо сказать прямо,
        # иначе «нет данных пробы» читается как «мы не успели проверить».
        report.warnings.append(
            "фоновые пробы выключены (NODE_PROBE_ENABLED=false): админка, /status и бот не покажут "
            "состояние портов — этот прогон проверяет их вручную"
        )

    for node, panel in pairs:
        code = node.code if node is not None else "primary"
        report.locations.append(
            await _check_location(
                node,
                panel,
                settings,
                by_code.get(code),
                repeat=max(1, repeat),
                interval=max(0.0, interval),
                timeout=timeout,
                dry_run=dry_run,
            )
        )
    return report


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - ручной запуск
    parser = argparse.ArgumentParser(
        description="Аудит локаций: какая не работает и почему — по слоям, с доказательствами."
    )
    parser.add_argument("--repeat", type=int, default=1, help="сколько раз проверять панель и каждый порт")
    parser.add_argument("--interval", type=float, default=0.0, help="пауза между попытками, секунды")
    parser.add_argument("--timeout", type=float, default=None, help="таймаут одной сетевой проверки, секунды")
    parser.add_argument("--json", action="store_true", help="машинный вывод: можно переслать в чат целиком")
    parser.add_argument("--dry-run", action="store_true", help="показать, что будет проверяться, не трогая сеть")
    args = parser.parse_args(argv)

    report = asyncio.run(
        collect(
            repeat=max(1, args.repeat),
            interval=max(0.0, args.interval),
            timeout=args.timeout,
            dry_run=args.dry_run,
        )
    )
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.as_text())
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    raise SystemExit(main())
