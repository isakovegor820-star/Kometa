"""Точка входа: бот (long polling), фоновые задачи и веб-слой ссылки-подписки.

Запуск:  python -m app.main
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from datetime import datetime, timezone

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import ErrorEvent
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
from app.db.session import SessionMaker, init_db
from app.panels.base import PanelError
from app.panels.registry import registry
from app.payments.base import PaymentError, PaymentStatus
from app.payments.registry import payments, platega_provider
from app.services import (
    digest,
    lifecycle,
    notifications,
    notify_bot,
    orders,
    subscriptions,
    watchdog,
)
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


async def job_daily_digest(bot: Bot) -> None:
    """Сводка за сутки: деньги, люди, инфраструктура.

    Одно сообщение в конце дня вместо десяти: сколько заработали, сколько
    пришло людей, что сломалось и что требует руки. Считается по базе, а не
    по памяти процесса, — после перезапуска цифры те же.
    """
    try:
        async with SessionMaker() as session:
            text = await digest.build_daily(session)
    except Exception as exc:  # noqa: BLE001 - сводка не важнее жизни бота
        logger.exception("Сводка за сутки не собралась: %s", exc)
        return
    sent = await notifications.notify_admins(bot, text)
    logger.info("Сводка за сутки отправлена: %s получателям", sent)


async def on_update_error(event: ErrorEvent) -> None:
    """Ошибка при обработке апдейта — сообщаем команде, а не только в лог.

    Клиент в этот момент видит «что-то пошло не так» и уходит. Раньше об этом
    узнавали из жалобы в поддержку; теперь — из сообщения с типом ошибки и
    id клиента, по которому можно посмотреть, что именно он делал.
    """
    logger.error("Ошибка обработки апдейта: %s", event.exception, exc_info=event.exception)
    user_id = None
    for source in ("message", "callback_query", "pre_checkout_query"):
        payload = getattr(event.update, source, None)
        author = getattr(payload, "from_user", None)
        if author is not None:
            user_id = author.id
            break
    await notifications.notify_error(
        notify_bot.customer_bot(),
        where="обработка апдейта",
        exc=event.exception,
        user_id=user_id,
    )


#: Больше этого числа сообщений за один прогон не отправляем: если сценарий
#: неожиданно захватил всю базу, лучше остановиться и разобраться, чем устроить
#: рассылку всем подряд (Telegram ограничивает ботов за массовые сообщения).
LIFECYCLE_MAX_PER_RUN = 150


async def job_lifecycle(bot: Bot) -> None:
    """Автосценарии: подсказка после триала, win-back, апселл, рефералка."""
    if not settings.lifecycle_enabled:
        return
    async with SessionMaker() as session:
        planned = await lifecycle.plan_sends(session)
        if len(planned) > LIFECYCLE_MAX_PER_RUN:
            logger.warning(
                "Автосценарии: под аудиторию попало %s человек — отправляю первые %s, "
                "остальные уйдут следующим прогоном",
                len(planned),
                LIFECYCLE_MAX_PER_RUN,
            )
            planned = planned[:LIFECYCLE_MAX_PER_RUN]
        sent = await lifecycle.run_lifecycle(bot, session, planned=planned)
        await session.commit()
        if sent:
            logger.info("Автосценарии: отправлено сообщений %s", sent)


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
            # Владельца заказа finalize_order определяет сам: получатель
            # доступа не должен зависеть от того, кто инициировал проверку.
            await finalize_order(session, order, bot)
            await session.commit()


async def _confirm_platega_orders(bot: Bot, *, statuses: tuple[str, ...]) -> int:
    """Спросить Platega про заказы в указанных статусах, выдать доступ оплаченным.

    Возвращает число выданных доступов.
    """
    granted = 0
    async with SessionMaker() as session:
        candidates = await orders.awaiting_payment(
            session, provider_prefix="platega", statuses=statuses
        )
        for order in candidates:
            provider = platega_provider(order.provider)
            if provider is None or not order.external_id:
                continue
            if order.external_id.startswith("ord-"):
                # Счёт у Platega не создавался (заказ до подключения оплаты) —
                # опрашивать нечего, а GET по чужому id вернёт ошибку.
                continue
            try:
                check = await provider.check_payment(order.external_id)
            except PaymentError as exc:
                logger.warning("Проверка счёта Platega %s не удалась: %s", order.external_id, exc)
                continue
            if check.status is not PaymentStatus.PAID:
                continue
            if order.status == "paid":
                continue

            logger.info("Platega: заказ #%s оплачен (подтверждено опросом)", order.id)
            # finalize_order выдаёт доступ сам, в том числе по заказу, который
            # успел закрыться: деньги пришли — человек не должен ждать админа.
            await finalize_order(session, order, bot)
            granted += 1
            await session.commit()
    return granted


async def job_check_platega(bot: Bot) -> None:
    """Быстрый опрос открытых счетов: клиент оплатил — доступ сразу.

    Вебхук быстрее, но он есть не всегда: пока Callback URL не вписан в кабинет
    Platega, опрос — единственный автоматический путь (``GET /transaction/{id}``).
    Поэтому спрашиваем каждые 30 секунд: клиент не должен сидеть перед экраном
    «оплата проходит» две минуты и тем более писать в поддержку.

    Закрытые заказы (клиент оплатил позже, чем истёк счёт) здесь не трогаем —
    ими занимается редкая задача ``job_check_platega_late``.
    """
    granted = await _confirm_platega_orders(bot, statuses=("pending",))
    if granted:
        logger.info("Platega: выдано доступов по опросу — %s", granted)


async def job_check_platega_late(bot: Bot) -> None:
    """Страховка: оплата по заказу, который успел закрыться.

    Счёт живёт 15 минут, заказ закрывается через 30 — если клиент платил с
    телефона и не вернулся в бот, оплата могла прийти уже по закрытому заказу.
    Раньше такой платёж ждал админа («проверь поступление и выдай вручную»),
    то есть человек, заплативший деньги, зависел от того, когда владелец
    посмотрит телефон. Теперь доступ выдаётся автоматически — заказ
    открывается заново, подписка продлевается, админам уходит обычное
    уведомление об оплате.

    Спрашиваем редко (раз в 15 минут) и только по заказам за сутки: платёжная
    ссылка столько не живёт, а дёргать провайдера без нужды незачем.
    """
    granted = await _confirm_platega_orders(bot, statuses=("canceled", "expired"))
    if granted:
        logger.info("Platega: выдано доступов по закрытым заказам — %s", granted)


async def startup_payment_check(bot: Bot) -> None:
    """Проверить счета сразу после старта: платёж мог прийти во время перезапуска.

    Иначе клиент, оплативший ровно в момент выката, ждал бы доступ до первого
    тика опроса. Ошибку только пишем в лог: проверка не важнее запуска бота.
    """
    try:
        await job_check_platega(bot)
    except Exception as exc:  # noqa: BLE001 - фоновая проверка не должна ронять старт
        logger.warning("Стартовая проверка платежей не удалась: %s", exc)


async def job_grant_paid(bot: Bot) -> None:
    """Довести до доступа оплаченные заказы, по которым выдача не подтверждена.

    Закрывает дыру «деньги приняты, доступа нет и не будет»: если панель не
    ответила в момент оплаты, заказ остаётся с пустым ``granted_at`` и попадает
    сюда. Функция сверяет факт (срок в панели) и либо отмечает выдачу
    выполненной, либо повторяет её — с алертом и ограничением числа попыток.
    """
    from app.services import orders as orders_service

    async with SessionMaker() as session:
        try:
            granted = await orders_service.grant_ungranted_orders(session, bot=bot)
        except Exception as exc:  # noqa: BLE001 - фоновая задача не должна падать молча
            logger.exception("Повторная выдача доступа упала: %s", exc)
            return
        await session.commit()
        if granted:
            logger.info("Повторная выдача доступа: заказы %s", granted)


async def job_retention(bot: Bot) -> None:
    """Ретенция персональных данных: чистка журналов и анонимизация молчунов.

    Сроки — решение владельца, зафиксированы в одном месте
    (``app/services/retention.py``) и совпадают с Политикой конфиденциальности:
    технические журналы — 30 дней, аккаунт без активности 12 месяцев —
    анонимизация, заказы и платежи — 4 года (их не трогаем).
    """
    from app.services import retention

    async with SessionMaker() as session:
        try:
            purged = await retention.purge_old_records(session)
            anonymized = await retention.anonymize_inactive_users(session)
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - фоновая задача не должна падать молча
            logger.exception("Ретенция упала: %s", exc)
            return

    if not purged.total and not anonymized:
        logger.info("Ретенция: чистить нечего")
        return
    logger.info(
        "Ретенция: удалено (%s), анонимизировано пользователей: %s",
        purged.as_text(),
        len(anonymized),
    )
    if bot is not None:
        await notifications.notify_admins(
            bot,
            "🧹 <b>Ретенция данных</b>\n"
            f"Удалено старше {retention.EVENTS_RETENTION_DAYS} дней — {purged.as_text()}.\n"
            f"Анонимизировано без активности {retention.INACTIVITY_MONTHS} мес.: {len(anonymized)}.\n"
            "Заказы и суммы сохранены (налоговый учёт, 4 года).",
        )


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
        if result.fetched or result.confirmed or result.errors or result.pending_grant:
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
    # Основной бот — он же отправитель писем клиентам: команды бота уведомлений
    # подтверждают заявку из служебного чата, а письмо клиенту должно прийти от
    # бота, которого клиент знает.
    notify_bot.set_customer_bot(bot)
    payments.init(bot)
    logger.info("Способы оплаты: %s", ", ".join(p.code for p in payments.available()) or "нет")

    # Бот уведомлений: отдельный токен для оперативных сообщений команде.
    await notify_bot.start()
    notify_task = await notify_bot.start_polling()

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
    dispatcher.errors.register(on_update_error)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(job_expire_subscriptions, "interval", minutes=10, args=[bot], id="expire_subs")
    scheduler.add_job(job_reminders, "interval", hours=1, args=[bot], id="reminders")
    scheduler.add_job(job_expire_orders, "interval", minutes=5, id="expire_orders")
    scheduler.add_job(job_check_crypto, "interval", minutes=2, args=[bot], id="check_crypto")
    # Platega: опрос — страховка от потерянного вебхука и основной путь там,
    # где публичного адреса для вебхука нет вообще. Открытые счета спрашиваем
    # каждые 30 секунд: клиент стоит с телефоном в руке и ждёт доступ, а не
    # «до двух минут». Закрытые (оплата пришла позже счёта) — раз в 15 минут.
    scheduler.add_job(job_check_platega, "interval", seconds=30, args=[bot], id="check_platega")
    scheduler.add_job(
        job_check_platega_late,
        "interval",
        minutes=15,
        args=[bot],
        id="check_platega_late",
    )
    scheduler.add_job(
        job_autopay,
        "interval",
        minutes=settings.autopay_interval_minutes,
        args=[bot],
        id="autopay",
    )
    # Повторная выдача доступа по оплаченным заказам: панель могла не ответить
    # в момент оплаты. Раз в 3 минуты — клиент не должен ждать человека.
    scheduler.add_job(job_grant_paid, "interval", minutes=3, args=[bot], id="grant_paid")
    # Ретенция: раз в сутки ночью (03:00 UTC = 06:00 МСК), когда никто не работает.
    # Задача чистит журналы старше 30 дней и анонимизирует тех, кто молчит 12 месяцев.
    scheduler.add_job(
        job_retention,
        "cron",
        hour=3,
        minute=0,
        args=[bot],
        id="retention",
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
    # Автосценарии в боте: подсказка после триала, возврат ушедших, апселл,
    # напоминание про рефералку. Раз в сутки, в 10:00 МСК — не ночью.
    scheduler.add_job(
        job_lifecycle,
        "cron",
        hour=7,
        minute=0,
        args=[bot],
        id="lifecycle",
    )
    # Сводка за сутки. Время в UTC: 18 = 21:00 МСК — день закрыт, но ещё не
    # ночь, и если что-то сломалось, у команды есть вечер, чтобы починить.
    # NOTIFY_DIGEST_HOUR_UTC=0 выключает сводку.
    if settings.notify_digest_hour_utc:
        scheduler.add_job(
            job_daily_digest,
            "cron",
            hour=settings.notify_digest_hour_utc,
            minute=0,
            args=[bot],
            id="daily_digest",
        )
    scheduler.start()

    web_task = asyncio.create_task(run_web(bot))
    # Счета проверяем сразу, не дожидаясь первого тика опроса.
    asyncio.create_task(startup_payment_check(bot), name="platega-startup-check")

    me = await bot.get_me()
    logger.info("Бот запущен: @%s", me.username)
    try:
        async with SessionMaker() as session:
            startup_text = await digest.build_startup(
                session,
                sales_bot=me.username or "",
                notify_username=notify_bot.username() or "основным ботом",
            )
    except Exception as exc:  # noqa: BLE001 - цифры не важнее запуска бота
        logger.warning("Не удалось собрать стартовую сводку: %s", exc)
        startup_text = f"🚀 <b>Kometa запущена</b>\nБот: @{me.username}"
    await notifications.notify_admins(bot, startup_text)

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
        if notify_task is not None:
            notify_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await notify_task
        web_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await web_task
        await payments.close()
        await registry.close()
        await notify_bot.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлено")
