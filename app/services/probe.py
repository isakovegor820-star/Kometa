"""Проба ноды «глазами клиента»: TCP-соединение и замер задержки.

Зачем отдельный слой, если есть проверка панели: панель отвечает по своему
API-порту, а клиент идёт на порт инбаунда. Между ними может быть закрытый
порт, сгоревшая подсеть, фильтр у оператора — и панель при этом здорова.
Проба отвечает на вопрос «дозвонится ли клиент» и даёт цифру задержки,
которую видно в админке и на странице подключения.

Важно про цифру: это задержка **от сервиса до ноды**, а не пинг клиента в
приложении (его считает сам клиент по ``SUBSCRIPTION_TEST_URL``). Показываем
её как «проверка с сервера», чтобы не обещать клиентский пинг.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from app.config import get_settings

#: Транспорты, доступные по TCP. UDP-профили (AmneziaWG, Hysteria2, TUIC)
#: так не проверить: открытый порт ничего не говорит о работоспособности.
TCP_NETWORKS: tuple[str, ...] = ("tcp", "raw", "ws", "xhttp", "grpc", "httpupgrade", "splithttp")

#: Шаги, на которых порт реально проверялся (TCP-соединение и рукопожатие).
#: Всё остальное («panel», «config») — проба не состоялась: сказать про порт
#: нечего, и делать вывод «порт не пускает клиента» нельзя.
MEASURED_STAGES: tuple[str, ...] = ("tcp", "tls")

#: Шаг успешной пробы: и TCP-соединение, и рукопожатие пройдены. Пишем его
#: явно, потому что интерфейсы (карточка ноды в админке) показывают состояние
#: по шагу: пустая строка при ``last_probe_ok=true`` выглядела в шаблоне как
#: «проба не выполнена» — зелёная нода с подписью «не проверяли».
OK_STAGE = "ok"

#: Шаг «поставить пробу нечем»: у ноды не заполнен host или нет TCP-инбаундов
#: (например канал только на AmneziaWG). Это не авария порта.
CONFIG_STAGE = "config"

#: Что известно о порте ноды — единый словарь состояний для интерфейсов.
PROBE_OK = "ok"
PROBE_PORT_FAILED = "port"
PROBE_UNAVAILABLE = "unavailable"
PROBE_NOT_CONFIGURED = "not_configured"
PROBE_UNKNOWN = "unknown"

#: Человеческие подписи состояний: один словарь на бота, админку и публичную
#: страницу. Раньше каждый интерфейс писал своё («не отвечает», «нет данных»,
#: «порт не пускает»), и одинаковые состояния выглядели как разные.
PROBE_TITLES: dict[str, str] = {
    PROBE_OK: "порт открыт",
    PROBE_PORT_FAILED: "порт не пускает",
    PROBE_UNAVAILABLE: "проба не выполнена",
    PROBE_NOT_CONFIGURED: "проба не настроена",
    PROBE_UNKNOWN: "пробы не было",
}


def probe_verdict(node: object) -> str:
    """Что известно о порте ноды по последней пробе.

    Одно место вместо разбора текста ошибки в шаблонах и отчётах: и таймаут
    TCP, и «панель не отдала инбаунды» пишут текст, но означают разное —
    первое «порт не пускает», второе «проба не состоялась».

    Успех проверяем ДО шага: ``last_probe_ok`` мог прийти из старой записи,
    где шаг не сохранялся, а успешную пробу нельзя превращать в «не выполнена».
    """
    if getattr(node, "last_probe_at", None) is None:
        return PROBE_UNKNOWN
    if bool(getattr(node, "last_probe_ok", False)):
        return PROBE_OK
    stage = str(getattr(node, "last_probe_stage", "") or "")
    if stage in MEASURED_STAGES:
        return PROBE_PORT_FAILED
    if stage == CONFIG_STAGE:
        return PROBE_NOT_CONFIGURED
    return PROBE_UNAVAILABLE


def probes_enabled(node: object) -> bool:
    """Идут ли пробы вообще: выключенные пробы нельзя читать как «всё хорошо».

    Настройка ``NODE_PROBE_ENABLED=false`` выключает пробы целиком. В этом
    случае «пробы не было» означает не «мы не успели», а «мы не проверяем» —
    и честная подпись другая.
    """
    try:
        from app.config import get_settings

        return bool(get_settings().node_probe_enabled)
    except Exception:  # noqa: BLE001 - настройки не должны ронять отрисовку
        return True


def probe_state(node: object) -> tuple[str, str]:
    """``(состояние, человеческая подпись)`` для карточек нод.

    Один источник для админки, публичной страницы и бота. Подпись объясняет,
    что делать, а не только констатирует: «порт 8443 не пускает» лучше, чем
    «недоступна», потому что называет предмет разговора с хостингом.

    Пустая подпись означает «проба ещё не запускалась»: это не диагноз, и
    показывать «всё работает» в этом состоянии нельзя.
    """
    verdict = probe_verdict(node)
    if verdict == PROBE_OK:
        ms = int(getattr(node, "last_probe_ms", 0) or 0)
        return verdict, f"порт открыт{f', {ms} мс' if ms else ''}"
    if verdict == PROBE_PORT_FAILED:
        closed = [item["port"] for item in probe_ports(node) if not item.get("ok")]
        opened = [item["port"] for item in probe_ports(node) if item.get("ok")]
        if closed and opened:
            return verdict, f"порт {', '.join(str(p) for p in closed)} не пускает (открыт {', '.join(str(p) for p in opened)})"
        if closed:
            return verdict, f"порт {', '.join(str(p) for p in closed)} не пускает"
        return verdict, "порт не пускает"
    if verdict == PROBE_NOT_CONFIGURED:
        detail = str(getattr(node, "last_probe_error", "") or "")
        return verdict, f"проба не настроена{': ' + detail if detail else ''}"
    if verdict == PROBE_UNKNOWN:
        if not probes_enabled(node):
            return verdict, "пробы выключены настройкой"
        return verdict, "проба ещё не запускалась"
    detail = str(getattr(node, "last_probe_error", "") or "")
    return verdict, f"проба не состоялась{': ' + detail if detail else ''}"


def probe_ports(node: object) -> list[dict]:
    """Замер по каждому порту из последней пробы (пусто — замеров нет).

    Разбираем JSON молча: это диагностическая строка, и «сломанный JSON» не
    повод не показать карточку ноды. Старые записи (до появления колонки)
    дают пустой список — интерфейсы обязаны это переживать.
    """
    import json

    raw = str(getattr(node, "last_probe_ports", "") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict) and "port" in item]


@dataclass(slots=True)
class PortResult:
    """Итог пробы одного порта: порт, состояние и задержка.

    Нужен потому, что «первый успешный из списка» скрывал половину картины:
    у ноды мог быть открыт 8443 и закрыт 443, клиент пробовал оба (в подписке
    оба), а в админке горело «ок» — и вопрос «почему у меня не работает» не
    имел ответа. Теперь проба отчитывается по каждому порту отдельно.
    """

    port: int
    label: str
    ok: bool
    ms: int = 0
    stage: str = ""
    detail: str = ""

    def as_text(self) -> str:
        state = "открыт" if self.ok else (self.detail or "не ответил")
        suffix = f" {self.ms} мс" if self.ok and self.ms else ""
        return f"{self.port}: {state}{suffix}"

    def as_dict(self) -> dict:
        return {
            "port": self.port,
            "label": self.label,
            "ok": self.ok,
            "ms": self.ms,
            "stage": self.stage,
            "detail": self.detail,
        }


@dataclass(slots=True)
class PanelProbe:
    """Полный итог пробы панели: лучший результат и таблица по портам.

    :param best: результат, по которому принимается решение (первый успешный,
        иначе последний неудачный — как раньше).
    :param ports: что вышло на каждом TCP-порту: доказательство для оператора.
    :param targets: сколько целей было найдено (0 — проба не состоялась).
    :param error: почему пробу не удалось поставить (панель/настройка).
        Пусто — значит пробу поставить удалось, и таблица ``ports`` заполнена.
    """

    best: ProbeResult
    ports: list[PortResult] = field(default_factory=list)
    targets: int = 0
    error: str = ""

    @property
    def open_ports(self) -> list[int]:
        return [item.port for item in self.ports if item.ok]

    @property
    def closed_ports(self) -> list[int]:
        return [item.port for item in self.ports if not item.ok]

    def as_dict(self) -> dict:
        return {
            "ok": self.best.ok,
            "ms": self.best.ms,
            "stage": self.best.stage,
            "detail": self.best.detail,
            "targets": self.targets,
            "error": self.error,
            "ports": [item.as_dict() for item in self.ports],
        }

    def as_text(self) -> str:
        if not self.ports:
            return self.error or "пробу не удалось поставить"
        return " · ".join(item.as_text() for item in self.ports)


@dataclass(slots=True)
class ProbeResult:
    """Итог пробы: успех, задержка и на каком шаге остановились."""

    ok: bool
    ms: int = 0
    stage: str = ""
    detail: str = ""

    @property
    def measured(self) -> bool:
        """Порт проверялся по-настоящему (TCP/TLS), а не «не дошли».

        Нужно вызывающему коду, чтобы не превращать «панель не отдала
        инбаунды» в диагноз «порт закрыт».
        """
        return self.stage in MEASURED_STAGES


@dataclass(slots=True)
class ProbeTarget:
    """Куда стучимся: адрес ноды и порт TCP-инбаунда."""

    host: str
    port: int
    label: str = ""
    extra: dict[str, str] = field(default_factory=dict)


def probe_targets(inbounds: list[object], host: str) -> list[ProbeTarget]:
    """Собрать цели для пробы из инбаундов панели.

    Берём только TCP-транспорты: UDP-порт открыт, но это не значит, что
    клиент подключится. Реальность и TLS проверяются отдельным ручным
    инструментом (``app.tools.ping_node``), потому что для рукопожатия нужен
    SNI инбаунда, а абстракция панели его не отдаёт.
    """
    clean_host = (host or "").strip()
    if not clean_host:
        return []

    targets: list[ProbeTarget] = []
    seen: set[int] = set()
    for inbound in inbounds:
        port = int(getattr(inbound, "port", 0) or 0)
        network = str(getattr(inbound, "network", "") or "").lower()
        protocol = str(getattr(inbound, "protocol", "") or "").lower()
        if port <= 0 or port in seen:
            continue
        if network not in TCP_NETWORKS:
            continue
        if protocol in {"wireguard", "amneziawg"}:
            continue
        seen.add(port)
        label = str(getattr(inbound, "remark", "") or "") or f"{clean_host}:{port}"
        targets.append(ProbeTarget(host=clean_host, port=port, label=label))
    return targets


async def probe_endpoint(
    host: str,
    port: int,
    *,
    sni: str = "",
    timeout: float | None = None,
) -> ProbeResult:
    """Проверить адрес: TCP-соединение и, если задан SNI, TLS-рукопожатие.

    :param host: адрес ноды.
    :param port: порт инбаунда.
    :param sni: домен для рукопожатия (пусто — ограничиваемся TCP).
    :param timeout: таймаут в секундах; пусто — из настроек.
    """
    settings = get_settings()
    limit = float(timeout if timeout is not None else settings.node_probe_timeout)
    limit = max(limit, 0.5)

    started = time.perf_counter()
    writer: asyncio.StreamWriter | None = None
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=limit)
    except (asyncio.TimeoutError, TimeoutError):
        return ProbeResult(False, stage="tcp", detail=f"таймаут TCP ({limit:.1f} с)")
    except OSError as exc:
        return ProbeResult(False, stage="tcp", detail=f"TCP: {exc}")

    tcp_ms = int((time.perf_counter() - started) * 1000)
    try:
        if sni:
            started_tls = time.perf_counter()
            try:
                await asyncio.wait_for(writer.start_tls(_client_ssl_context(), server_hostname=sni), timeout=limit)
            except (asyncio.TimeoutError, TimeoutError):
                return ProbeResult(False, ms=tcp_ms, stage="tls", detail=f"таймаут рукопожатия ({sni})")
            except Exception as exc:  # noqa: BLE001 - рукопожатие может упасть чем угодно
                # Порт открыт, но рукопожатие не прошло: для маскировки это
                # тревожный признак — фильтр пропускает TCP, но рвёт сессию.
                return ProbeResult(False, ms=tcp_ms, stage="tls", detail=f"TLS: {exc}")
            tls_ms = int((time.perf_counter() - started_tls) * 1000)
            return ProbeResult(True, ms=tls_ms, stage="tls", detail=f"TLS {sni}")
        return ProbeResult(True, ms=tcp_ms, stage="tcp", detail="TCP открыт")
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - закрытие не должно ломать пробу
                pass


def _client_ssl_context():  # noqa: ANN202 - тип из ssl, импортируем лениво
    import ssl

    context = ssl.create_default_context()
    # Reality отдаёт сертификат маскировочного домена, у WS за CDN бывает
    # самоподписанный на источнике. Нам важен факт рукопожатия и задержка,
    # а не цепочка доверия: доверие проверяет клиент при подключении.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


async def probe_panel(panel, host: str) -> ProbeResult:
    """Проверить ноду через её панель: взять TCP-инбаунды и постучаться.

    Возвращаем первый успешный результат. Если не ответил ни один порт —
    последний результат с ошибкой (по нему и поднимается алерт).

    Если список инбаундов получить не удалось, проба вообще не состоялась:
    ``stage="panel"`` и в тексте прямо сказано, что порт не проверялся —
    иначе эту ошибку легко принять за закрытый порт.

    Тонкая обёртка над :func:`probe_panel_detailed`: вызывающим, которым нужен
    только вердикт, не нужно знать про таблицу портов. Кому нужны
    доказательства (алерты, админка) — берут подробный вариант.
    """
    return (await probe_panel_detailed(panel, host)).best


async def probe_panel_detailed(panel, host: str) -> PanelProbe:
    """Проверить ноду и вернуть **все** замеры, а не только лучший.

    Зачем подробный вариант: в подписке у клиента не один порт, а несколько
    (TCP 443 для Reality, WS/XHTTP за CDN, 8443 для добора). Проба «первый
    успешный» отвечала «ок», когда открыт хотя бы один, и молчала о том, что
    второй закрыт. Диагностика «у меня не работает» начинается именно с этого
    места, поэтому доказательства сохраняем все.
    """
    if not (host or "").strip():
        return PanelProbe(
            best=ProbeResult(False, stage=CONFIG_STAGE, detail="у ноды не заполнен host"),
            error="у ноды не заполнен host",
        )

    try:
        inbounds = await panel.list_inbounds()
    except Exception as exc:  # noqa: BLE001 - чужая панель отвечает чем угодно
        detail = f"панель не отдала инбаунды, порт не проверялся: {exc}"
        return PanelProbe(
            best=ProbeResult(False, stage="panel", detail=detail),
            error=detail,
        )

    targets = probe_targets(list(inbounds or []), host)
    if not targets:
        return PanelProbe(
            best=ProbeResult(False, stage=CONFIG_STAGE, detail="нет TCP-инбаундов для пробы"),
            error="нет TCP-инбаундов для пробы",
        )

    best = ProbeResult(False, stage=CONFIG_STAGE, detail="нет целей для пробы")
    ports: list[PortResult] = []
    for target in targets:
        result = await probe_endpoint(target.host, target.port)
        ports.append(
            PortResult(
                port=target.port,
                label=target.label,
                ok=result.ok,
                ms=result.ms,
                stage=result.stage,
                detail=result.detail,
            )
        )
        if result.ok:
            best = ProbeResult(
                True,
                ms=result.ms,
                stage=result.stage,
                detail=f"{target.label}: {result.detail}",
            )
            continue
        if not best.ok:
            best = ProbeResult(
                False,
                ms=result.ms,
                stage=result.stage,
                detail=f"{target.label}: {result.detail}",
            )

    # Успешная проба помечается шагом «ok»: интерфейсы показывают состояние по
    # шагу, и пустое значение при ok выглядело как «пробу не выполняли».
    if best.ok:
        best = ProbeResult(True, ms=best.ms, stage=OK_STAGE, detail=best.detail)
    return PanelProbe(best=best, ports=ports, targets=len(targets))
