"""Веб-слой: ссылка-подписка /sub/<token>.

Зачем он нужен:
  * пользователь получает ОДНУ постоянную ссылку на все локации;
  * при смене панели или добавлении ноды ссылка не меняется;
  * мы управляем метаданными профиля (имя, интервал обновления, остаток трафика).
"""

from __future__ import annotations

import base64
import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import get_settings
from app.db.models import Subscription
from app.panels.base import PanelError
from app.panels.registry import registry
from app.services import subscriptions as subs_service

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
    """Собрать состояние сервиса (с кэшем на минуту)."""
    now = time.time()
    cached = _STATUS_CACHE.get("value")
    if cached is not None and now - float(_STATUS_CACHE["at"]) < STATUS_CACHE_SECONDS:
        return cached  # type: ignore[return-value]

    from app.config import get_settings
    from app.db.session import SessionMaker
    from app.panels.registry import registry

    settings = get_settings()
    nodes: list[dict] = []
    async with SessionMaker() as session:
        for panel in await registry.all_panels(session):
            entry = {"title": panel.name, "ok": False}
            try:
                entry["ok"] = await panel.health()
            except Exception as exc:  # noqa: BLE001 - панель может быть недоступна
                logger.warning("Статус: панель %s не ответила: %s", panel.name, exc)
            nodes.append(entry)

    # Источник подписок доступен, если жива хотя бы одна панель
    value = {
        "ok": bool(nodes) and any(node["ok"] for node in nodes),
        "subscriptions_available": bool(nodes) and any(node["ok"] for node in nodes),
        "payments": {
            "stars": settings.stars_enabled,
            "manual": bool(settings.manual_payment_details),
            "crypto": bool(settings.cryptobot_token),
            "wata": bool(settings.wata_token),
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

    app = FastAPI(title="Kometa subscription service", docs_url=None, redoc_url=None)
    app.state.bot = bot
    # Статика панели (CSS/JS) отдаётся только вместе с /admin: никаких CDN,
    # панель обязана работать на localhost без интернета.
    app.mount("/admin/static", StaticFiles(directory=str(STATIC_DIR)), name="admin-static")
    app.include_router(admin_router)
    app.include_router(payments_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

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

        rows = "".join(
            f"<li><span class='dot {'ok' if node['ok'] else 'bad'}'></span>"
            f"{node['title']} — {'доступна' if node['ok'] else 'недоступна'}</li>"
            for node in snapshot["nodes"]
        ) or "<li class='muted'>Ноды ещё не настроены</li>"

        overall_ok = snapshot["ok"]
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
        background:{'#16301f' if overall_ok else '#33191c'};color:{'#a7e6c1' if overall_ok else '#f3b6b6'}}}
 ul{{list-style:none;padding:0;margin:16px 0 0}}
 li{{padding:6px 0;border-bottom:1px solid #2a2f3f}}
 li:last-child{{border:none}}
 .dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:9px}}
 .dot.ok{{background:#45c07d}} .dot.bad{{background:#ef6b6b}}
 .muted{{color:#98a0b3}}
 footer{{margin-top:16px;font-size:13px}}
 a{{color:#6ea8fe;text-decoration:none}}
</style></head>
<body><main><div class="card">
 <h1>🛰 Kometa — состояние сервиса</h1>
 <span class="badge">{'Всё работает' if overall_ok else 'Есть проблемы'}</span>
 <ul>{rows}</ul>
 <p class="muted" style="margin-top:16px">Обновлено: {snapshot['checked_at']} UTC</p>
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
</style></head>
<body><main><div class="card">
 <h1>🔌 Подключение Kometa</h1>
 <p class="muted small">Нажми название приложения — профиль добавится сам, останется включить VPN.</p>
 {cards}
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

            configs: list[str] = []
            used_bytes = 0
            renamed_per_panel = False
            for panel in await registry.all_panels(session):
                if not sub.panel_user_uuid:
                    continue
                try:
                    panel_configs = await panel.get_configs(sub.panel_user_uuid)
                except PanelError as exc:
                    logger.warning("Панель %s не отдала конфиги: %s", panel.name, exc)
                    continue

                # Имя локации у каждой страны своё: у основной панели — из
                # LOCATION_TITLE, у ноды — её название из админки («🇯🇵 Япония»).
                title = getattr(panel, "location_title", "") or ""
                if title:
                    panel_configs, renamed_per_panel = _rename(panel_configs, title), True
                configs.extend(panel_configs)

                try:
                    panel_user = await panel.get_user(sub.panel_user_uuid)
                    if panel_user is not None:
                        used_bytes += panel_user.used_bytes or 0
                except PanelError as exc:
                    logger.warning("Панель %s не отдала статистику: %s", panel.name, exc)

            if not configs:
                raise HTTPException(status_code=503, detail="no configs available")

        # Имена локаций: панель отдаёт служебные («DE-REALITY-firefox-u123-10GB📊»),
        # в приложении это выглядит мусором — подменяем на человеческое имя страны.
        if settings.location_title and not renamed_per_panel:
            # Запасной путь: у панелей нет своих имён (одна страна, старые настройки).
            configs = _rename(configs, settings.location_title)

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
        if client_format == "clash" and build_clash_yaml is not None:
            return Response(
                content=build_clash_yaml(configs, title="Kometa"),
                media_type="text/yaml; charset=utf-8",
                headers=headers,
            )
        if client_format == "singbox" and build_singbox_json is not None:
            return Response(
                content=build_singbox_json(configs, title="Kometa"),
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
