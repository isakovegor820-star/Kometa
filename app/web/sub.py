"""Веб-слой: ссылка-подписка /sub/<token>.

Зачем он нужен:
  * пользователь получает ОДНУ постоянную ссылку на все локации;
  * при смене панели или добавлении ноды ссылка не меняется;
  * мы управляем метаданными профиля (имя, интервал обновления, остаток трафика).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import get_settings
from app.db.models import Subscription
from app.panels.base import PanelError, panel_label
from app.panels.registry import registry
from app.services import subscriptions as subs_service
from app.web.ratelimit import RateLimiter, SubCache, limit_for_path, public_client_ip

logger = logging.getLogger(__name__)
settings = get_settings()


def _profile_title(value: str) -> str:
    """Значение заголовка ``profile-title`` (имя профиля в приложении).

    Клиенты понимают и обычный текст, и формат ``base64:<строка>``. Кириллицу
    надёжнее отдавать в base64: часть клиентов не декодирует заголовок в UTF-8
    и показывает вместо имени адрес сервера.
    """
    title = (value or "").strip()
    if not title:
        return "Kometa"
    if title.isascii():
        return title
    return "base64:" + base64.b64encode(title.encode("utf-8")).decode()


#: Срок дальше этого числа лет считаем «бессрочным»: клиентам отдаём expire=0,
#: иначе приложение показывает дату из далёкого будущего вместо «бессрочно».
FOREVER_YEARS = 10


def _expire_timestamp(expires_at: datetime | None) -> int:
    """Метка окончания подписки для заголовка ``subscription-userinfo``.

    ``0`` в этом заголовке означает «не истекает». Бессрочные подписки храним с
    далёкой датой (чтобы их не трогали фоновые задачи истечения), а клиентам
    всё равно отдаём ``0`` — иначе Happ показывает «Истекает: 01.01.2099».
    """
    if expires_at is None:
        return 0
    moment = expires_at.replace(tzinfo=timezone.utc)
    if moment - datetime.now(timezone.utc) > timedelta(days=365 * FOREVER_YEARS):
        return 0
    return int(moment.timestamp())


def _rename(configs: list[str], title: str) -> list[str]:
    """Подменить служебное имя локации на человеческое («🇯🇵 Япония»)."""
    try:
        from app.web.subscription_format import rename_locations
    except ImportError:  # pragma: no cover - модуль форматов необязателен
        return configs
    return rename_locations(configs, title)


@dataclass(slots=True)
class PanelData:
    """Данные подписки, собранные у панелей (кэшируются на несколько секунд)."""

    configs: list[str] = field(default_factory=list)
    used_bytes: int = 0
    renamed_per_panel: bool = False
    channel_urls: dict[str, str] = field(default_factory=dict)


async def collect_panel_data(session: AsyncSession, sub: Subscription) -> PanelData:
    """Собрать конфиги со всех панелей **параллельно** и с таймаутом на каждую.

    Почему параллельно: раньше панели опрашивались по очереди, и две
    недоступные локации означали два полных таймаута ожидания — клиент видел
    зависшую ссылку, а вебхук-запросы копились. Теперь время ответа — самая
    медленная живая панель, а недоступная отсекается таймаутом
    ``PANEL_TIMEOUT_SECONDS`` и не мешает остальным.

    Возвращаем именно данные, а не готовый HTTP-ответ: формат (base64, Clash,
    sing-box) выбирается по User-Agent уже после, поэтому один кэш обслуживает
    и Happ, и v2rayNG.
    """
    data = PanelData()
    if not sub.panel_user_uuid:
        return data

    from app.web.subscription_format import channel_mark

    uuid = sub.panel_user_uuid
    timeout = max(1, int(settings.panel_timeout_seconds))
    pairs = await registry.all_panels_with_nodes(session)

    async def from_panel(node, panel) -> tuple[list[str], int, bool, dict[str, str]]:  # noqa: ANN001
        """Опрос одной панели. Ошибка этой панели не должна ломать остальные."""
        configs: list[str] = []
        used = 0
        renamed = False
        urls: dict[str, str] = {}
        try:
            panel_configs = await asyncio.wait_for(panel.get_configs(uuid), timeout=timeout)
        except (PanelError, asyncio.TimeoutError) as exc:
            logger.warning("Панель %s не отдала конфиги: %s", panel_label(panel), exc)
            return configs, used, renamed, urls
        except Exception as exc:  # noqa: BLE001 - чужая панель может ответить чем угодно
            logger.warning("Панель %s: неожиданная ошибка конфигов: %s", panel_label(panel), exc)
            return configs, used, renamed, urls

        # Имя локации у каждой страны своё: у основной панели — из LOCATION_TITLE,
        # у ноды — её название из админки («🇯🇵 Япония»).
        title = getattr(panel, "location_title", "") or ""
        # Канал локации: обычная, резервная или CDN. Метка в имени — единственный
        # способ передать канал в base64-список (Happ/v2RayTun групп не умеют),
        # а у канала может быть свой test-URL: под ограничениями общий адрес
        # замера недоступен.
        channel = (
            (getattr(node, "channel", "main") or "main").strip().lower() if node is not None else "main"
        )
        mark = channel_mark(channel)
        if mark:
            title = f"{title or panel_label(panel)}{mark}"
            custom_url = str(getattr(node, "test_url", "") or "").strip()
            if custom_url:
                urls.setdefault(channel, custom_url)
        if title:
            panel_configs, renamed = _rename(panel_configs, title), True
        configs.extend(panel_configs)

        try:
            panel_user = await asyncio.wait_for(panel.get_user(uuid), timeout=timeout)
            if panel_user is not None:
                used = panel_user.used_bytes or 0
        except (PanelError, asyncio.TimeoutError) as exc:
            logger.warning("Панель %s не отдала статистику: %s", panel_label(panel), exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Панель %s: неожиданная ошибка статистики: %s", panel_label(panel), exc)
        return configs, used, renamed, urls

    results = await asyncio.gather(*(from_panel(node, panel) for node, panel in pairs))
    for configs, used, renamed, urls in results:
        data.configs.extend(configs)
        data.used_bytes += used
        data.renamed_per_panel = data.renamed_per_panel or renamed
        for channel, url in urls.items():
            data.channel_urls.setdefault(channel, url)
    return data


def _test_url() -> str:
    """URL для замера задержки в клиенте (Clash ``url-test``, sing-box ``urltest``).

    Свой ``/ping`` вместо внешнего ``generate_204`` нужен из-за ограничений
    связи: если тест-URL недоступен, клиент помечает мёртвыми **все** профили,
    включая живые, и клиент видит «нет пинга» вместо рабочего подключения.
    Поэтому по умолчанию берём наш адрес, а внешний оставляем запасным
    вариантом для локальной разработки.
    """
    from app.web.subscription_format import DEFAULT_TEST_URL

    configured = (settings.subscription_test_url or "").strip()
    if configured:
        return configured

    base = (settings.public_base_url or "").strip().rstrip("/")
    if base.startswith("https://") or (base.startswith("http://") and "127.0.0.1" not in base and "localhost" not in base):
        return f"{base}/ping"
    return DEFAULT_TEST_URL


def _locations_html(nodes: list[object]) -> str:
    """Список локаций с задержкой пробы для страницы подключения.

    Пинг здесь — задержка **от сервиса до ноды**, а не пинг телефона (его
    клиент считает сам по test-URL). Подписываем это словами, чтобы цифра
    не создавала ложных ожиданий.
    """
    from app.services.probe import (
        PROBE_NOT_CONFIGURED,
        PROBE_OK,
        PROBE_PORT_FAILED,
        PROBE_UNKNOWN,
        probe_verdict,
    )
    from app.web.subscription_format import CHANNELS

    #: Человеческие имена каналов: обычные локации без пометки.
    CHANNEL_TITLES = {code: title for code, (_mark, title) in CHANNELS.items()}

    if not nodes:
        return ""

    rows: list[str] = []
    for node in nodes:
        title = str(getattr(node, "title", "") or getattr(node, "code", "") or "").strip()
        channel = str(getattr(node, "channel", "main") or "main").strip().lower()
        label = CHANNEL_TITLES.get(channel, "") if channel != "main" else ""
        # Порт проверялся только на шагах tcp/tls: если проба не состоялась,
        # красный «не отвечает» — выдумка, честнее «нет данных».
        verdict = probe_verdict(node)
        ms = int(getattr(node, "last_probe_ms", 0) or 0)
        if verdict == PROBE_OK:
            dot, ping = "🟢", (f"{ms} мс" if ms else "замер без цифры")
        elif verdict == PROBE_PORT_FAILED:
            dot, ping = "🔴", "не отвечает"
        elif verdict == PROBE_UNKNOWN:
            dot, ping = "⚪", "нет замера"
        elif verdict == PROBE_NOT_CONFIGURED:
            dot, ping = "⚪", "проба не настроена"
        else:  # PROBE_UNAVAILABLE — проба не состоялась
            dot, ping = "⚪", "нет данных пробы"
        suffix = f" · {label}" if label else ""
        rows.append(
            f"<div class='loc'><span>{dot} {title}{suffix}</span>"
            f"<span class='muted small'>{ping}</span></div>"
        )
    return (
        "<p class='muted small' style='margin:16px 0 6px'>"
        "Локации и задержка (замер с сервера, не с телефона):</p>" + "".join(rows)
    )


def _subscription_headers(sub: Subscription, used_bytes: int = 0) -> dict[str, str]:
    total = sub.traffic_limit_gb * 1024**3 if sub.traffic_limit_gb else 0
    expire_ts = _expire_timestamp(sub.expires_at)
    return {
        "profile-title": _profile_title(settings.subscription_title),
        "profile-update-interval": "12",
        "profile-web-page-url": settings.public_base_url,
        "subscription-userinfo": f"upload=0; download={used_bytes}; total={total}; expire={expire_ts}",
        "cache-control": "no-store",
    }


#: Кэш состояния сервиса: проверять панели на каждый запрос нельзя
_STATUS_CACHE: dict[str, object] = {"at": 0.0, "value": None}
STATUS_CACHE_SECONDS = 60


async def _service_status() -> dict:
    """Собрать состояние сервиса (с кэшем на минуту).

    Показываем **два независимых слоя**, потому что это разные правды:

    * ``panel`` — отвечает ли панель управления (авторизация, список инбаундов);
    * ``probe`` — пускает ли порт клиента (TCP-соединение «глазами клиента»).

    Раньше в ответе было только первое, а подпись обещала второе: страница
    писала «доступна», когда панель отвечала, хотя порт для клиента мог быть
    закрыт. И наоборот: панель недоступна (например 3x-ui закрыт по IP), а
    локация работает — и страница говорила «недоступна» про живую локацию.
    Ровно это противоречие владелец видел как «везде сообщает, что Нидерланды
    не работают».
    """
    now = time.time()
    cached = _STATUS_CACHE.get("value")
    if cached is not None and now - float(_STATUS_CACHE["at"]) < STATUS_CACHE_SECONDS:
        return cached  # type: ignore[return-value]

    from app.config import get_settings
    from app.db.session import SessionMaker
    from app.panels.registry import registry
    from app.services import probe as probe_service

    settings = get_settings()
    nodes: list[dict] = []
    async with SessionMaker() as session:
        for node, panel in await registry.all_panels_with_nodes(session):
            title = (
                (node.title if node is not None else "")
                or getattr(panel, "location_title", "")
                or panel_label(panel)
            )
            entry: dict = {"title": title, "ok": False, "panel": False}
            try:
                entry["panel"] = await panel.health()
            except Exception as exc:  # noqa: BLE001 - панель может быть недоступна
                logger.warning("Статус: панель %s не ответила: %s", panel_label(panel), exc)
            entry["ok"] = bool(entry["panel"])

            if node is not None:
                # Порт важнее панели для ответа на вопрос «подключусь ли я».
                state, text = probe_service.probe_state(node)
                entry["probe"] = state
                entry["probe_text"] = text
                entry["probe_ms"] = int(node.last_probe_ms or 0)
                entry["probe_at"] = (
                    node.last_probe_at.strftime("%d.%m %H:%M") if node.last_probe_at else ""
                )
                entry["ports"] = [
                    {"port": item.get("port"), "ok": bool(item.get("ok"))}
                    for item in probe_service.probe_ports(node)
                ]
                # Клиенту отвечает порт, а не панель: если замер есть, решает он.
                # Панель нужна как запасной признак — когда пробы не было вовсе
                # (свежая установка), иначе все локации выглядели бы мёртвыми.
                if state == probe_service.PROBE_PORT_FAILED:
                    entry["ok"] = False
                elif state == probe_service.PROBE_OK:
                    entry["ok"] = True
                elif state == probe_service.PROBE_NOT_CONFIGURED:
                    # UDP-канал (например AmneziaWG): TCP-проба неприменима, и
                    # «не настроена» — это не «сломана». Верим панели.
                    entry["ok"] = bool(entry["panel"])
                else:
                    entry["ok"] = bool(entry["panel"])
                entry["ready"] = entry["ok"] and state == probe_service.PROBE_OK
            else:
                # Основная панель из .env: пробы к ней нет, порт не измеряли.
                # Это НЕ «готово»: панель отвечает — значит можно выдать конфиг,
                # но пускает ли порт клиента, мы не проверяли. Пометка «ready»
                # остаётся ложной, иначе страница снова начнёт утверждать
                # «всё работает» без единого замера.
                entry["probe"] = "unknown"
                entry["probe_text"] = "проба не настроена"
                entry["ready"] = False
            nodes.append(entry)

    # Пустая выдача — не «всё хорошо»: значит панелей нет вовсе.
    ready_nodes = [node for node in nodes if node.get("ready")]
    value = {
        "ok": bool(nodes) and bool(ready_nodes) and all(node["ok"] for node in nodes),
        "subscriptions_available": bool(nodes) and any(node["panel"] for node in nodes),
        "payments": {
            "stars": settings.stars_enabled,
            "manual": bool(settings.manual_payment_details),
            "crypto": bool(settings.cryptobot_token),
        },
        "nodes": nodes,
        "checked_at": datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M"),
    }
    _STATUS_CACHE["at"] = now
    _STATUS_CACHE["value"] = value
    return value


async def build_app(bot: "Bot | None" = None) -> FastAPI:
    """Собрать веб-приложение: публичная ссылка-подписка + админ-панель.

    :param bot: экземпляр бота — нужен админке, чтобы писать пользователям
        (подтверждение оплаты, начисление дней, рассылка).
    """
    from fastapi.staticfiles import StaticFiles

    from app.web.admin import router as admin_router
    from app.web.payments import router as payments_router
    from app.web.templating import STATIC_DIR

    # openapi_url=None обязателен: docs_url и redoc_url были закрыты, а схема
    # осталась — публичный /openapi.json отдавал 49 КБ с 68 путями админки,
    # включая /admin/orders/{id}/refund, /admin/export/users.csv и
    # /admin/team/{id}/password, с именами полей форм. Это бесплатная разведка
    # для того, кто ищет админку (находка 09.10.2026).
    app = FastAPI(
        title="Kometa subscription service",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.bot = bot
    # Лимиты и кэш живут в состоянии приложения: и боевой процесс, и каждый
    # тестовый экземпляр получают свои счётчики (иначе тесты влияли бы друг на
    # друга, а «тридцать запросов подряд» ловили бы чужой трафик).
    app.state.rate_limiter = RateLimiter()
    app.state.sub_cache = SubCache()

    @app.middleware("http")
    async def public_rate_limit(request: Request, call_next):  # noqa: ANN001, ANN202
        """Ограничить поток запросов с одного IP на публичных адресах.

        Ответ 429 — человеческий: сколько ждать и почему. Без лимита тридцать
        запросов к /sub подряд — это тридцать походов в панели от каждого, кто
        узнал токен, а вебхуки читают тело целиком до проверки подписи.
        """
        if not settings.rate_limit_enabled:
            return await call_next(request)
        info = limit_for_path(request.url.path, settings)
        if info is None:
            return await call_next(request)
        group, limit = info
        key = f"{group}:{public_client_ip(request)}"
        decision = app.state.rate_limiter.check(
            key, limit=limit, window_seconds=int(settings.rate_limit_window_seconds)
        )
        if not decision.allowed:
            logger.warning("Лимит запросов: %s исчерпан (порог %s)", key, limit)
            return JSONResponse(
                {
                    "detail": (
                        f"Слишком много запросов с этого адреса: не больше {limit} за "
                        f"{int(settings.rate_limit_window_seconds)} секунд. "
                        f"Повтори через {decision.retry_after} с."
                    ),
                    "retry_after": decision.retry_after,
                },
                status_code=429,
                headers={"Retry-After": str(decision.retry_after)},
            )
        return await call_next(request)

    @app.middleware("http")
    async def panel_ajax_flag(request: Request, call_next):  # noqa: ANN001, ANN202
        """Пометить запросы панели, пришедшие из JS (заголовок X-Panel-Ajax).

        Нужно, чтобы действия отвечали JSON-ом (тост + обновление списка), а не
        редиректом с сообщением в cookie. Флаг живёт в contextvar и читается
        flash_redirect, поэтому маршруты об этом ничего не знают.
        """
        from app.web.admin import common as admin_common

        admin_common.set_ajax(request.headers.get(admin_common.AJAX_HEADER) == "1")
        return await call_next(request)

    # Статика панели (CSS/JS) отдаётся только вместе с /admin: никаких CDN,
    # панель обязана работать на localhost без интернета.
    app.mount("/admin/static", StaticFiles(directory=str(STATIC_DIR)), name="admin-static")
    app.include_router(admin_router)
    app.include_router(payments_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ping", status_code=204)
    async def ping() -> Response:
        """Пустой 204 для замера задержки в клиентах.

        Именно этот адрес приложения используют как test-URL (Clash ждёт 204):
        по нему клиент показывает пинг и выбирает живую локацию. Внешние
        ``generate_204`` под ограничениями связи недоступны, и тогда клиент
        считает мёртвыми все профили сразу.
        """
        return Response(status_code=204)

    @app.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request) -> Response:
        """Публичная страница состояния сервиса.

        Зачем: во время сбоев клиент должен уметь проверить,
        работает ли сервис, не заходя в Telegram. Показываем только страны и
        состояние — никаких адресов нод и токенов.
        """
        snapshot = await _service_status()
        if request.query_params.get("format") == "json":
            return JSONResponse(snapshot)

        def _row(node: dict) -> str:
            """Строка локации: панель и порт — отдельными словами.

            Клиенту важно второе («подключусь ли»), но починить можно только
            зная первое: панель — это вход оператора, порт — путь клиента.
            Одинаковые подписи на два разных состояния и рождали путаницу.
            """
            ok = bool(node.get("ok"))
            probe = node.get("probe", "unknown")
            ports = node.get("ports") or []
            # Про локацию без замера нельзя сказать ни «работает», ни «сломалась»:
            # серый кружок и слово «без замера» — честный ответ. Иначе панель из
            # .env, к которой пробы не ставятся, всегда висела зелёной.
            unknown = probe in ("unknown", "unavailable", "not_configured")
            details: list[str] = []
            details.append("панель отвечает" if node.get("panel") else "панель не отвечает")
            details.append(node.get("probe_text") or "порт не проверяли")
            if ports:
                details.append(
                    "порты: "
                    + ", ".join(f"{item['port']} {'✓' if item['ok'] else '✗'}" for item in ports)
                )
            # Цифру задержки отдельно не добавляем: она уже внутри подписи пробы
            # («порт открыт, 42 мс»), иначе строка читалась как «42 мс · 42 мс».
            note = ""
            if probe == "port" and node.get("panel"):
                # Самое дорогое противоречие: панель жива, порт для клиента закрыт.
                note = (
                    "<div class='small warn'>Панель отвечает, но порт для подключения "
                    "закрыт — напишите в поддержку.</div>"
                )
            elif not node.get("panel") and probe == "ok":
                # Обратный случай: панель закрыта, а клиенты подключаются.
                note = (
                    "<div class='small muted'>Панель управления сейчас не отвечает, "
                    "но подключение работает.</div>"
                )
            elif unknown:
                note = "<div class='small muted'>Замер порта ещё не проходил.</div>"
            measured_at = node.get("probe_at") or ""
            if unknown:
                dot, verdict = "unknown", "без замера"
            else:
                dot, verdict = ("ok" if ok else "bad"), ("работает" if ok else "есть проблема")
            return (
                f"<li><span class='dot {dot}'></span>"
                f"<b>{node['title']}</b> — {verdict}"
                f"<div class='small muted'>{' · '.join(details)}"
                f"{' · замер ' + measured_at if measured_at else ''}</div>{note}</li>"
            )

        rows = "".join(_row(node) for node in snapshot["nodes"]) or (
            "<li class='muted'>Ноды ещё не настроены</li>"
        )

        overall_ok = snapshot["ok"]
        # Три состояния, а не два: «всё работает» нельзя показывать, когда
        # замеров ещё не было — это утверждение, которого мы не проверяли.
        # «Проверяем» — только когда проблем нет, но и подтверждения нет.
        broken = [node for node in snapshot["nodes"] if not node.get("ok")]
        measured = [node for node in snapshot["nodes"] if node.get("probe") == "ok"]
        if overall_ok and measured:
            badge_text, badge_bg, badge_fg = "Всё работает", "#16301f", "#a7e6c1"
        elif broken:
            badge_text, badge_bg, badge_fg = "Есть проблемы", "#33191c", "#f3b6b6"
        else:
            badge_text, badge_bg, badge_fg = "Проверяем", "#2a2f3f", "#c9cfdd"

        html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Статус сервиса Kometa</title>
<style>
 body{{margin:0;background:#0f1117;color:#e8eaf0;font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
 main{{max-width:520px;margin:10vh auto;padding:24px}}
 .card{{background:#171a23;border:1px solid #2a2f3f;border-radius:14px;padding:22px}}
 h1{{font-size:20px;margin:0 0 14px}}
 .badge{{display:inline-block;padding:4px 12px;border-radius:999px;font-size:14px;
        background:{badge_bg};color:{badge_fg}}}
 ul{{list-style:none;padding:0;margin:16px 0 0}}
 li{{padding:8px 0;border-bottom:1px solid #2a2f3f}}
 li:last-child{{border:none}}
 .dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:9px}}
 .dot.ok{{background:#45c07d}} .dot.bad{{background:#ef6b6b}}
 .dot.unknown{{background:#5b6478}}
 .muted{{color:#98a0b3}} .small{{font-size:13px}} .warn{{color:#f3b6b6}}
 footer{{margin-top:16px;font-size:13px}}
 a{{color:#6ea8fe;text-decoration:none}}
</style></head>
<body><main><div class="card">
 <h1>🛰 Kometa — состояние сервиса</h1>
 <span class="badge">{badge_text}</span>
 <ul>{rows}</ul>
 <p class="muted small" style="margin-top:16px">
   «Порт открыт» — замер с нашего сервера до локации, а не скорость вашего
   интернета: её показывает приложение при подключении.</p>
 <p class="muted" style="margin-top:8px">Обновлено: {snapshot['checked_at']} UTC</p>
 <footer class="muted">
   Страница обновляется автоматически — её можно открыть в любой момент.<br>
   Вопросы и помощь: {settings.support_contact or 'поддержка в боте'}.
   <a href="?format=json">JSON</a>
 </footer>
</div></main></body></html>"""
        return HTMLResponse(html)

    @app.get("/connect/{token}", response_class=HTMLResponse)
    async def connect_page(token: str, request: Request, app: str = "") -> Response:
        """Страница подключения: одно нажатие — профиль уже в приложении.

        Зачем отдельная страница, а не кнопка с диплинком: Telegram запрещает
        нестандартные схемы (``happ://``, ``v2rayng://``, ``hiddify://``) в
        inline-кнопках — он отвечает «Unsupported URL protocol» и клавиатура
        не отправляется вообще. Поэтому кнопка в боте ведёт сюда (обычный
        https), а страница уже открывает приложение по схеме. Бонус: здесь
        видно ссылку целиком, если приложение не установлено.
        """
        from urllib.parse import quote

        from app.db.session import SessionMaker

        async with SessionMaker() as session:  # type: AsyncSession
            sub = await subs_service.get_subscription_by_token(session, token)
            if sub is None:
                raise HTTPException(status_code=404, detail="subscription not found")
            if sub.status == "blocked":
                raise HTTPException(status_code=403, detail="subscription blocked")

            from sqlalchemy import select

            from app.db.models import Node

            # Локации с замером пробы: клиент видит, что живое, а не гадает,
            # почему в приложении «нет пинга».
            nodes = list(
                (
                    await session.scalars(
                        select(Node).where(Node.is_active.is_(True)).order_by(Node.priority, Node.id)
                    )
                ).all()
            )

        sub_url = subs_service.subscription_link(token)
        title = (settings.subscription_title or "Kometa").strip() or "Kometa"
        fragment = quote(title, safe="")
        encoded = quote(sub_url, safe="")

        apps = [
            ("happ", "🟢 Happ", f"happ://add/{sub_url}#{fragment}"),
            ("v2rayng", "🔵 v2rayNG", f"v2rayng://install-sub/?url={encoded}%23{fragment}"),
            ("hiddify", "🟣 Hiddify", f"hiddify://import/{sub_url}#{fragment}"),
        ]
        chosen = (app or "").strip().lower()
        cards = "".join(
            f"<a class='btn{' primary' if key == chosen else ''}' href=\"{url}\" "
            f"id='btn-{key}'>{label}</a>"
            for key, label, url in apps
        )
        locations = _locations_html(nodes)
        # Автопопытка открыть приложение: пользователь уже нажал кнопку в
        # Telegram, поэтому браузер обычно разрешает переход по схеме.
        auto = next((url for key, _, url in apps if key == chosen), "")
        auto_script = (
            f"<script>setTimeout(function(){{location.href='{auto}';}}, 400);</script>" if auto else ""
        )

        html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Kometa — подключение</title>
<style>
 body{{margin:0;background:#0f1117;color:#e8eaf0;font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
 main{{max-width:520px;margin:6vh auto;padding:20px}}
 .card{{background:#171a23;border:1px solid #2a2f3f;border-radius:14px;padding:22px}}
 h1{{font-size:20px;margin:0 0 6px}}
 .btn{{display:block;padding:14px 16px;margin:10px 0;border-radius:12px;text-align:center;
      background:#1e2330;border:1px solid #2f3547;color:#e8eaf0;text-decoration:none;font-weight:600}}
 .btn.primary{{background:#1b3a5c;border-color:#2f6ea8}}
 input{{width:100%;box-sizing:border-box;padding:12px;border-radius:10px;border:1px solid #2f3547;
       background:#12151d;color:#cfd6e6;font-size:14px;margin:8px 0}}
 .copy{{width:100%;padding:12px;border-radius:10px;border:0;background:#2f6ea8;color:#fff;font-weight:600;font-size:15px}}
 .muted{{color:#98a0b3}} .small{{font-size:13px}}
 .ok{{background:#16301f!important;border-color:#2c6b45!important}}
 .loc{{display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-bottom:1px solid #232838}}
 .loc:last-child{{border-bottom:0}}
</style></head>
<body><main><div class="card">
 <h1>🔌 Подключение Kometa</h1>
 <p class="muted small">Нажми название приложения — профиль добавится сам, останется включить VPN.</p>
 {cards}
 {locations}
 <p class="muted small" style="margin-top:18px">Если приложение не открылось — скопируй ссылку и вставь её в клиенте вручную:</p>
 <input id="sub" value="{sub_url}" readonly onclick="this.select()">
 <button class="copy" id="copy" onclick="copySub()">📋 Скопировать ссылку</button>
 <p class="muted small" style="margin-top:14px">Ссылка постоянная: при продлении её менять не нужно.
  Приложение скачать: Happ, v2rayNG, Hiddify — любое на выбор.</p>
</div></main>
<script>
function copySub(){{
  var el=document.getElementById('sub');
  el.select(); el.setSelectionRange(0, 99999);
  var done=false;
  try{{ done=document.execCommand('copy'); }}catch(e){{}}
  if(navigator.clipboard && !done){{ navigator.clipboard.writeText(el.value); done=true; }}
  var b=document.getElementById('copy');
  if(done){{ b.textContent='✅ Скопировано'; b.className='copy ok'; }}
}}
</script>{auto_script}</body></html>"""
        return HTMLResponse(html)

    @app.get("/sub/{token}")
    async def get_subscription(token: str, request: Request) -> Response:
        from app.db.session import SessionMaker

        async with SessionMaker() as session:  # type: AsyncSession
            sub = await subs_service.get_subscription_by_token(session, token)
            if sub is None:
                raise HTTPException(status_code=404, detail="subscription not found")
            if sub.status == "blocked":
                raise HTTPException(status_code=403, detail="subscription blocked")

            # Короткий кэш данных панелей: приложения дёргают ссылку при каждом
            # открытии и обновлении профиля, а панель на каждый запрос получает
            # два HTTP-вызова. Внутри окна кэша панели не опрашиваются вовсе;
            # срок подписки при этом берётся из БД, поэтому продление видно сразу.
            data = app.state.sub_cache.get(token)
            if data is None:
                data = await collect_panel_data(session, sub)
                app.state.sub_cache.set(token, data)

            if not data.configs:
                raise HTTPException(status_code=503, detail="no configs available")

            configs = list(data.configs)
            used_bytes = data.used_bytes
            renamed_per_panel = data.renamed_per_panel
            channel_urls = dict(data.channel_urls)

        # Имена локаций: панель отдаёт служебные («DE-REALITY-firefox-u123-10GB📊»),
        # в приложении это выглядит мусором — подменяем на человеческое имя страны.
        if settings.location_title and not renamed_per_panel:
            # Запасной путь: у панелей нет своих имён (одна страна, старые настройки).
            configs = _rename(configs, settings.location_title)

        # Режим TCP-only: когда провайдер пропускает только TCP 80/443/22,
        # UDP-профили в подписке бессмысленны — клиент долбится в мёртвый профиль
        # и решает, что сервис сломался. Выкидываем их из ВСЕХ форматов сразу,
        # иначе base64-список и sing-box разошлись бы по составу.
        # Включается флагом SUBSCRIPTION_TCP_ONLY (см. .env.example).
        if settings.subscription_tcp_only:
            from app.web.subscription_format import is_udp_link

            before = len(configs)
            configs = [link for link in configs if not is_udp_link(link)]
            if len(configs) < before:
                logger.info(
                    "Режим TCP-only: убрано UDP-профилей — %d из %d",
                    before - len(configs),
                    before,
                )
        if not configs:
            raise HTTPException(status_code=503, detail="no configs available")

        body = "\n".join(configs)
        headers = _subscription_headers(sub, used_bytes)

        # Формат выбираем по User-Agent приложения: Clash и sing-box умеют
        # группу авто-выбора локации, остальным отдаём привычный base64-список.
        try:
            from app.web.subscription_format import (
                build_clash_yaml,
                build_singbox_json,
                detect_client_format,
            )
        except ImportError:  # pragma: no cover - модуль форматов необязателен
            build_clash_yaml = build_singbox_json = None  # type: ignore[assignment]
            detect_client_format = lambda *_: "base64"  # noqa: E731

        requested = request.query_params.get("format")
        if requested == "plain":
            return PlainTextResponse(body, headers=headers)

        client_format = detect_client_format(request.headers.get("user-agent"), requested)
        test_url = _test_url()
        if client_format == "clash" and build_clash_yaml is not None:
            return Response(
                content=build_clash_yaml(configs, title="Kometa", test_url=test_url, test_urls=channel_urls),
                media_type="text/yaml; charset=utf-8",
                headers=headers,
            )
        if client_format == "singbox" and build_singbox_json is not None:
            return Response(
                content=build_singbox_json(configs, title="Kometa", test_url=test_url, test_urls=channel_urls),
                media_type="application/json; charset=utf-8",
                headers=headers,
            )

        encoded = base64.b64encode(body.encode()).decode()
        return Response(
            content=encoded,
            media_type="text/plain; charset=utf-8",
            headers=headers,
        )

    @app.get("/sub/{token}/info")
    async def subscription_info(token: str) -> dict[str, object]:
        from app.db.session import SessionMaker

        async with SessionMaker() as session:
            sub = await subs_service.get_subscription_by_token(session, token)
            if sub is None:
                raise HTTPException(status_code=404, detail="subscription not found")
            return {
                "status": sub.status,
                "expires_at": sub.expires_at.isoformat() if sub.expires_at else None,
                "days_left": sub.days_left,
                "devices_limit": sub.devices_limit,
                "traffic_limit_gb": sub.traffic_limit_gb,
            }

    return app
