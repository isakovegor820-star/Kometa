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
from app.bot.middlewares import (
    ChannelGateMiddleware,
    DbSessionMiddleware,
    ThrottlingMiddleware,
    UserMiddleware,
)
from app.config import get_settings
from app.db.models import User
from app.db.session import SessionMaker, init_db
from app.panels.base import PanelError
from app.panels.registry import registry
from app.payments.base import PaymentError, PaymentStatus
from app.payments.registry import payments
from app.services import notifications, orders, subscriptions, watchdog
from app.web.sub import build_app

settings = get_settings()
logger = logging.getLogger("kometa")


# --------------------------------------------------------------- фоновые задачи
async def job_expire_subscriptions(bot: Bot) -> None:
    async with SessionMaker() as session:
        try:
            changed = await subscriptions.disable_expired(
                session, await subscriptions.all_user_panels(session)
            )
        except PanelError as exc:
            logger.error("Не удалось отключить истёкшие подписки: %s", exc)
            return
        await session.commit()
        if changed:
            sent = await notifications.notify_expired(bot, session, changed)
            logger.info("Отключено подписок: %s, уведомлено: %s", len(changed), sent)


async def job_watch_clients(bot: Bot) -> None:
    """Суточный контроль клиентов: аномалии трафика и «вечные» доступы."""
    if not settings.watch_enabled:
        return
    report = await watchdog.run_watch(registry.primary())
    if report is None:
        logger.warning("Контроль клиентов: отчёт не собран (панель молчит)")
        return
    logger.info("Контроль клиентов: клиентов %s, аномалий %s", report.total, report.alert_count)
    await notifications.notify_admins(bot, watchdog.format_report(report))


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


async def job_autopay(bot: Bot) -> None:
    """Автоподтверждение переводов по выписке банка."""
    from app.services import autopay

    if not settings.autopay_enabled:
        return
    async with SessionMaker() as session:
        try:
            result = await autopay.reconcile(session, registry.primary(), bot)
        except Exception as exc:  # noqa: BLE001 - фоновая задача не должна падать молча
            logger.exception("Автоплатёж упал: %s", exc)
            return
        await session.commit()
        if result.fetched or result.confirmed or result.errors:
            logger.info("Автоплатёж: %s", result.as_text())


async def job_node_health(bot: Bot) -> None:
    """Следим за всеми панелями (странами) и сообщаем о смене состояния.

    Проверяем не только основную панель: если упала нода Токио, клиенты Дальнего
    Востока остаются без локации, и об этом нужно узнать сразу, а не от них.

    Проверка идёт через сервис алертов: он пишет ``last_check_at``/``last_check_ok``
    у нод и заводит (или закрывает) алерты в панели. Telegram остаётся для
    мгновенного сигнала, но проблема больше не теряется, если сообщение
    прочитали и забыли.
    """
    from app.db.models import Node
    from app.services import alerts as alerts_service

    state: dict[str, bool] = job_node_health.__dict__.setdefault("state", {})
    async with SessionMaker() as session:
        pairs: list[tuple[Node | None, object]] = [(None, registry.primary())]
        nodes = list(
            (
                await session.scalars(
                    select(Node).where(Node.is_active.is_(True)).order_by(Node.priority, Node.id)
                )
            ).all()
        )
        pairs.extend((node, registry.for_node(node)) for node in nodes)
        try:
            results = await alerts_service.check_nodes(session, pairs)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - фоновая задача не должна падать молча
            logger.exception("Проверка нод упала: %s", exc)
            await session.rollback()
            return
        await session.commit()

    for entry in results:
        label = entry["title"]
        ok = bool(entry["ok"])
        # «Готова» — не то же, что «ответила»: панель может отвечать и при этом
        # не мочь выдать конфиг (разошлись ID инбаундов). Раньше такое состояние
        # не давало сообщения вообще, а бейдж в админке горел зелёным.
        ready = bool(entry.get("ready", ok))
        previous = state.get(label)
        # Сообщаем о смене состояния, а также если проблема обнаружена на первой
        # проверке после запуска: молчать о мёртвой ноде только потому, что бот
        # перезапустился минуту назад, — плохая идея.
        if (previous is not None and previous != ready) or (previous is None and not ready):
            if ready:
                text = f"✅ Нода <b>{label}</b> снова отвечает."
            elif ok:
                text = (
                    f"⚠️ Нода <b>{label}</b>: панель отвечает с ошибкой — "
                    f"{entry['error']}"
                )
            else:
                text = f"⚠️ Нода <b>{label}</b> недоступна — проверь сервер и панель."
            await notifications.notify_admins(bot, text)
        state[label] = ready


async def job_node_probe(bot: Bot) -> None:
    """Проверить ноды «глазами клиента»: TCP-порт и задержка.

    Панель может отвечать, а порт инбаунда — нет: это не видно в
    ``job_node_health``, зато видно клиенту. Поэтому отдельная проба: она
    пишет пинг для админки и страницы подключения и поднимает алерт
    ``node_probe_failed``, когда дозвониться не удалось.
    """
    from app.db.models import Node
    from app.services import alerts as alerts_service

    state: dict[str, bool] = job_node_probe.__dict__.setdefault("state", {})
    async with SessionMaker() as session:
        nodes = list(
            (
                await session.scalars(
                    select(Node).where(Node.is_active.is_(True)).order_by(Node.priority, Node.id)
                )
            ).all()
        )
        pairs: list[tuple[Node | None, object]] = [(node, registry.for_node(node)) for node in nodes]
        try:
            results = await alerts_service.check_probes(session, pairs)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - фоновая задача не должна падать молча
            logger.exception("Проба нод упала: %s", exc)
            await session.rollback()
            return
        await session.commit()

    for entry in results:
        label = entry["title"]
        verdict = entry.get("verdict", "ok" if entry["ok"] else "unavailable")
        previous = state.get(label)
        if verdict != previous:
            if verdict == "ok" and previous is not None:
                text = (
                    f"✅ Нода <b>{label}</b> снова пускает клиента"
                    f" (задержка {entry['ms']} мс)."
                )
                await notifications.notify_admins(bot, text)
            elif verdict == "port":
                # Порт реально проверялся и не пустил — это авария.
                await notifications.notify_admins(
                    bot,
                    f"⚠️ Нода <b>{label}</b>: порт не пускает клиента. {entry['detail']}",
                )
            # «Проба не состоялась» (panel/config) сюда не попадает: причина —
            # панель или настройка, о ней сообщает проверка нод (job_node_health).
            # Иначе на одну причину уходило бы два разных сообщения.
        state[label] = verdict


# ---------------------------------------------------------------------- запуск
def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )


def _uvicorn_kwargs() -> dict[str, object]:
    """Параметры запуска веб-слоя, включая TLS, если заданы сертификаты.

    Клиенты (Happ, v2rayNG) отказываются добавлять подписку по http, поэтому
    на боевом сервере веб-слой слушает https с сертификатом Let's Encrypt.
    """
    kwargs: dict[str, object] = {
        "host": settings.web_host,
        "port": settings.web_port,
        "log_level": settings.log_level.lower(),
        "access_log": False,
    }
    if settings.web_ssl_cert and settings.web_ssl_key:
        kwargs["ssl_certfile"] = settings.web_ssl_cert
        kwargs["ssl_keyfile"] = settings.web_ssl_key
    return kwargs


async def run_web(bot: Bot) -> None:
    app = await build_app(bot)
    config = uvicorn.Config(app, **_uvicorn_kwargs())
    server = uvicorn.Server(config)
    scheme = "https" if (settings.web_ssl_cert and settings.web_ssl_key) else "http"
    logger.info("Подписки: %s://%s:%s/sub/<token>", scheme, settings.web_host, settings.web_port)
    logger.info(
        "Админ-панель: %s://%s:%s/admin %s",
        scheme,
        settings.web_host,
        settings.web_port,
        "" if settings.admin_panel_password else "(ВЫКЛЮЧЕНА: задай ADMIN_PANEL_PASSWORD)",
    )
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
    logger.info("Способы оплаты: %s", ", ".join(p.code for p in payments.available()) or "нет")

    dispatcher = Dispatcher()
    # pre_checkout_query обязателен для оплаты в Stars — ему тоже нужны
    # сессия БД и наш пользователь, но гейт подписки на него не ставим:
    # подтверждение оплаты не должно зависеть от подписки на канал.
    for observer in (dispatcher.message, dispatcher.callback_query):
        observer.middleware(ThrottlingMiddleware())
        observer.middleware(DbSessionMiddleware())
        observer.middleware(UserMiddleware())
        observer.middleware(ChannelGateMiddleware())
    dispatcher.pre_checkout_query.middleware(ThrottlingMiddleware())
    dispatcher.pre_checkout_query.middleware(DbSessionMiddleware())
    dispatcher.pre_checkout_query.middleware(UserMiddleware())
    dispatcher.include_router(build_router())

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(job_expire_subscriptions, "interval", minutes=10, args=[bot], id="expire_subs")
    scheduler.add_job(job_reminders, "interval", hours=1, args=[bot], id="reminders")
    scheduler.add_job(job_expire_orders, "interval", minutes=5, id="expire_orders")
    scheduler.add_job(job_check_crypto, "interval", minutes=2, args=[bot], id="check_crypto")
    scheduler.add_job(
        job_autopay,
        "interval",
        minutes=settings.autopay_interval_minutes,
        args=[bot],
        id="autopay",
    )
    scheduler.add_job(job_node_health, "interval", minutes=5, args=[bot], id="node_health")
    # Проба «глазами клиента»: TCP-порт и задержка. Отдельно от проверки
    # панели — панель отвечает, а порт может не пускать.
    scheduler.add_job(job_node_probe, "interval", minutes=5, args=[bot], id="node_probe")
    # Сторож аномалий: раз в сутки (09:00 МСК) сверяем клиентов и трафик.
    scheduler.add_job(
        job_watch_clients,
        "cron",
        hour=6,
        minute=0,
        args=[bot],
        id="watch_clients",
    )
    scheduler.start()

    web_task = asyncio.create_task(run_web(bot))

    me = await bot.get_me()
    logger.info("Бот запущен: @%s", me.username)
    await notifications.notify_admins(bot, f"🚀 <b>Kometa запущена</b>\nБот: @{me.username}")

    # Самопроверка гейта подписки: «включил гейт, а бота в канал админом не
    # добавил» — самая частая ошибка настройки, и узнать о ней лучше сразу.
    if settings.channel_gate_enabled:
        from app.services import channel_gate

        gate_ok, gate_detail = await channel_gate.admin_check(bot)
        logger.info("Гейт подписки: %s", gate_detail)
        if not gate_ok:
            await notifications.notify_admins(
                bot,
                "⚠️ <b>Гейт подписки на канал не работает</b>\n\n"
                f"{gate_detail}\n\n"
                "Что сделать: добавь бота в канал <b>администратором</b> "
                "(право «Добавлять участников» не нужно, достаточно админки) "
                "и проверь CHANNEL_ID. Пока проверка не работает, бот ведёт себя "
                "по CHANNEL_GATE_FAIL_OPEN.",
            )

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
