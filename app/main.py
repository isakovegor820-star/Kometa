"""Точка входа: бот (long polling), фоновые задачи и веб-слой ссылки-подписки.

Запуск:  python -m app.main
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.bot.handlers import build_router
from app.bot.handlers.buy import finalize_order
from app.bot.middlewares import DbSessionMiddleware, ThrottlingMiddleware, UserMiddleware
from app.config import get_settings
from app.db.models import User
from app.db.session import SessionMaker, init_db
from app.panels.base import PanelError
from app.panels.registry import registry
from app.payments.base import PaymentError, PaymentStatus
from app.payments.registry import payments
from app.services import notifications, orders, subscriptions
from app.web.sub import build_app

settings = get_settings()
logger = logging.getLogger("kometa")


# --------------------------------------------------------------- фоновые задачи
async def job_expire_subscriptions(bot: Bot) -> None:
    async with SessionMaker() as session:
        try:
            changed = await subscriptions.disable_expired(session, registry.primary())
        except PanelError as exc:
            logger.error("Не удалось отключить истёкшие подписки: %s", exc)
            return
        await session.commit()
        if changed:
            sent = await notifications.notify_expired(bot, session, changed)
            logger.info("Отключено подписок: %s, уведомлено: %s", len(changed), sent)


async def job_reminders(bot: Bot) -> None:
    async with SessionMaker() as session:
        for days in (3, 1):
            sent = await notifications.notify_expiring(bot, session, days)
            if sent:
                logger.info("Напоминаний за %s дн.: %s", days, sent)
        await session.commit()


async def job_expire_orders() -> None:
    async with SessionMaker() as session:
        expired = await orders.expire_stale_orders(session)
        await session.commit()
        if expired:
            logger.info("Закрыто просроченных заказов: %s", len(expired))


async def job_check_crypto(bot: Bot) -> None:
    """Автоматически подтверждает крипто-платежи, не дожидаясь кнопки."""
    provider = payments.get("crypto")
    if provider is None:
        return
    async with SessionMaker() as session:
        pending = [o for o in await orders.pending_orders(session) if o.provider == "crypto"]
        for order in pending:
            try:
                check = await provider.check_payment(order.external_id or "")
            except PaymentError as exc:
                logger.warning("Проверка крипто-счёта %s не удалась: %s", order.external_id, exc)
                continue
            if check.status is not PaymentStatus.PAID:
                continue
            user = await session.get(User, order.user_id)
            if user is None:
                continue
            await finalize_order(session, order, bot, user)
            await session.commit()


async def job_node_health(bot: Bot) -> None:
    """Следим за панелями и сообщаем админам об изменении состояния."""
    state: dict[str, bool] = job_node_health.__dict__.setdefault("state", {})
    for panel in [registry.primary()]:
        try:
            ok = await panel.health()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Панель %s: ошибка проверки — %s", panel.name, exc)
            ok = False
        previous = state.get(panel.name)
        if previous is not None and previous != ok:
            text = (
                f"✅ Нода <b>{panel.name}</b> снова отвечает."
                if ok
                else f"⚠️ Нода <b>{panel.name}</b> недоступна — проверь сервер и панель."
            )
            await notifications.notify_admins(bot, text)
        state[panel.name] = ok


# ---------------------------------------------------------------------- запуск
def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )


async def run_web() -> None:
    app = await build_app()
    config = uvicorn.Config(
        app,
        host=settings.web_host,
        port=settings.web_port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    server = uvicorn.Server(config)
    logger.info("Веб-слой подписок: http://%s:%s/sub/<token>", settings.web_host, settings.web_port)
    await server.serve()


async def main() -> None:
    setup_logging()

    if not settings.bot_token:
        logger.error("BOT_TOKEN не задан. Заполни .env (см. .env.example) и запусти снова.")
        sys.exit(1)

    await init_db()
    logger.info("База готова: %s", settings.resolved_db_url)

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    payments.init(bot)

    dispatcher = Dispatcher()
    for observer in (dispatcher.message, dispatcher.callback_query):
        observer.middleware(ThrottlingMiddleware())
        observer.middleware(DbSessionMiddleware())
        observer.middleware(UserMiddleware())
    dispatcher.include_router(build_router())

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(job_expire_subscriptions, "interval", minutes=10, args=[bot], id="expire_subs")
    scheduler.add_job(job_reminders, "interval", hours=1, args=[bot], id="reminders")
    scheduler.add_job(job_expire_orders, "interval", minutes=5, id="expire_orders")
    scheduler.add_job(job_check_crypto, "interval", minutes=2, args=[bot], id="check_crypto")
    scheduler.add_job(job_node_health, "interval", minutes=5, args=[bot], id="node_health")
    scheduler.start()

    web_task = asyncio.create_task(run_web())

    me = await bot.get_me()
    logger.info("Бот запущен: @%s", me.username)
    await notifications.notify_admins(bot, f"🚀 <b>Kometa запущена</b>\nБот: @{me.username}")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        web_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await web_task
        await payments.close()
        await registry.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлено")
