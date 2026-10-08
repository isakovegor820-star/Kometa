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

#: Шаг «поставить пробу нечем»: у ноды не заполнен host или нет TCP-инбаундов
#: (например канал только на AmneziaWG). Это не авария порта.
CONFIG_STAGE = "config"

#: Что известно о порте ноды — единый словарь состояний для интерфейсов.
PROBE_OK = "ok"
PROBE_PORT_FAILED = "port"
PROBE_UNAVAILABLE = "unavailable"
PROBE_NOT_CONFIGURED = "not_configured"
PROBE_UNKNOWN = "unknown"


def probe_verdict(node: object) -> str:
    """Что известно о порте ноды по последней пробе.

    Одно место вместо разбора текста ошибки в шаблонах и отчётах: и таймаут
    TCP, и «панель не отдала инбаунды» пишут текст, но означают разное —
    первое «порт не пускает», второе «проба не состоялась».
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
    """
    if not (host or "").strip():
        return ProbeResult(False, stage="config", detail="у ноды не заполнен host")

    try:
        inbounds = await panel.list_inbounds()
    except Exception as exc:  # noqa: BLE001 - чужая панель отвечает чем угодно
        return ProbeResult(False, stage="panel", detail=f"панель не отдала инбаунды, порт не проверялся: {exc}")

    targets = probe_targets(list(inbounds or []), host)
    if not targets:
        return ProbeResult(False, stage="config", detail="нет TCP-инбаундов для пробы")

    last = ProbeResult(False, stage="config", detail="нет целей для пробы")
    for target in targets:
        result = await probe_endpoint(target.host, target.port)
        if result.ok:
            return ProbeResult(
                True,
                ms=result.ms,
                stage=result.stage,
                detail=f"{target.label}: {result.detail}",
            )
        last = ProbeResult(
            False,
            ms=result.ms,
            stage=result.stage,
            detail=f"{target.label}: {result.detail}",
        )
    return last
