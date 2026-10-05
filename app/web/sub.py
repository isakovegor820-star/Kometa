"""Веб-слой: ссылка-подписка /sub/<token>.

Зачем он нужен:
  * пользователь получает ОДНУ постоянную ссылку на все локации;
  * при смене панели или добавлении ноды ссылка не меняется;
  * мы управляем метаданными профиля (имя, интервал обновления, остаток трафика).
"""

from __future__ import annotations

import base64
import logging
from datetime import timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
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
