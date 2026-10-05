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
from datetime import datetime, timezone

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


def _subscription_headers(sub: Subscription, used_bytes: int = 0) -> dict[str, str]:
    total = sub.traffic_limit_gb * 1024**3 if sub.traffic_limit_gb else 0
    expire_ts = int(sub.expires_at.replace(tzinfo=timezone.utc).timestamp()) if sub.expires_at else 0
    return {
        "profile-title": "Kometa",
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
    from app.web.admin import router as admin_router
    from app.web.payments import router as payments_router

    app = FastAPI(title="Kometa subscription service", docs_url=None, redoc_url=None)
    app.state.bot = bot
    app.include_router(admin_router)
    app.include_router(payments_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request) -> Response:
        """Публичная страница состояния сервиса.

        Зачем: во время сбоев и ограничений клиент должен уметь проверить,
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
   Если мобильный интернет не работает, попробуй Wi-Fi — при ограничениях у операторов
   проводной интернет обычно продолжает работать.<br>
   Вопросы — в поддержку из бота. <a href="?format=json">JSON</a>
 </footer>
</div></main></body></html>"""
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
            for panel in await registry.all_panels(session):
                if not sub.panel_user_uuid:
                    continue
                try:
                    configs.extend(await panel.get_configs(sub.panel_user_uuid))
                    panel_user = await panel.get_user(sub.panel_user_uuid)
                    if panel_user is not None:
                        used_bytes += panel_user.used_bytes or 0
                except PanelError as exc:
                    logger.warning("Панель %s не отдала конфиги: %s", panel.name, exc)

            if not configs:
                raise HTTPException(status_code=503, detail="no configs available")

        body = "\n".join(configs)
        if request.query_params.get("format") == "plain":
            return PlainTextResponse(body, headers=_subscription_headers(sub, used_bytes))

        encoded = base64.b64encode(body.encode()).decode()
        return Response(
            content=encoded,
            media_type="text/plain; charset=utf-8",
            headers=_subscription_headers(sub, used_bytes),
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
