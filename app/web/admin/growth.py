"""Рост: реферальная программа, промокоды, тарифы и рассылки.

Почему всё в одном модуле: это четыре стороны одного вопроса «откуда берутся
и как долго остаются клиенты». Рефералка приводит людей, промокод снимает
сомнение на первой оплате, тариф задаёт цену, рассылка возвращает тех, кто
уже ушёл. Владелец смотрит на них вместе, поэтому и страницы стоят рядом.

Правила, общие для модуля:
  * просмотр и действия разведены (``growth.view`` / ``growth.act``): поддержка
    видит цифры, но ничего не меняет;
  * деньги и цены — только владелец (``plans.manage``);
  * рассылка — отдельное право (``broadcast.send``), потому что ошибка в ней
    стоит дороже всего: сообщение уходит живым людям и его не отозвать;
  * каждое изменение пишется в журнал: видно, кто включил промокод и менял цену.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, or_, select

from app.config import get_settings
from app.db.models import (
    Broadcast,
    Plan,
    PromoCode,
    PromoRedemption,
    Referral,
    Subscription,
    User,
    utcnow,
)
from app.db.session import SessionMaker
from app.services import audit, notifications, promo as promo_service, referral as referral_service
from app.web import ui
from app.web.admin.common import flash_redirect, page, require

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()

#: Сколько строк показываем на странице роста без пагинации.
TOP_REFERRERS_LIMIT = 15
INVITATIONS_LIMIT = 50
CODES_LIMIT = 100
#: Сколько последних применений промокода показываем в строке списка.
REDEMPTIONS_PER_CODE = 3

#: Аудитории рассылки. Значение из формы обязано быть в этом списке: опечатка
#: или подделанный POST не должны превратиться в «отправить вообще всем».
AUDIENCES: tuple[tuple[str, str], ...] = (
    ("active", "С активной подпиской"),
    ("expired", "С истёкшей подпиской"),
    ("trials", "На пробном доступе"),
    ("all", "Все, кроме заблокированных"),
)
AUDIENCE_CODES = frozenset(code for code, _ in AUDIENCES)

#: Подписи статусов рассылки: ключ — значение Broadcast.status.
BROADCAST_STATUS: dict[str, tuple[str, str]] = {
    "running": ("Идёт", "info"),
    "done": ("Завершена", "ok"),
    "failed": ("Ошибка", "err"),
    "canceled": ("Остановлена", "warn"),
}
BROADCAST_HISTORY_LIMIT = 20

#: Пауза между сообщениями: Telegram ограничивает скорость, а очередь из
#: нескольких тысяч человек не должна превратиться в бан бота.
BROADCAST_PAUSE_SECONDS = 0.05
#: Текст сообщения в Telegram — до 4096 символов; режем с запасом.
BROADCAST_MAX_CHARS = 4000

#: Границы полей тарифа: (минимум, максимум). Одна таблица на валидацию
#: и на подсказки — так форма и проверка не разъезжаются.
PLAN_LIMITS: dict[str, tuple[int, int]] = {
    "days": (1, 3650),
    "price_rub": (0, 100_000),
    "price_stars": (0, 100_000),
    "devices_limit": (1, 50),
    "traffic_limit_gb": (0, 10_000),
    "sort_order": (-9999, 9999),
}

#: Фоновые задачи держим в множестве: пока на задачу никто не ссылается,
#: сборщик мусора вправе её убить — рассылка оборвалась бы на середине.
_TASKS: set[asyncio.Task] = set()


def _task_finished(task: asyncio.Task) -> None:
    """Убрать задачу из множества и показать в логе то, что она не поймала.

    Без этого исключение из фоновой задачи осело бы в «Task exception was
    never retrieved» — и владелец не узнал бы, почему рассылка не дошла.
    """
    _TASKS.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("Фоновая рассылка упала: %s", error)


# ------------------------------------------------------------------ помощники
def _int_field(
    form: Any,  # noqa: ANN401 - starlette FormData
    name: str,
    label: str,
    *,
    low: int,
    high: int,
    default: int = 0,
) -> tuple[int, str]:
    """Целое число из формы с границами. Возвращает (значение, текст ошибки).

    Пустое поле — это не ошибка: у промокода «0 активаций» значит «без лимита»,
    и владельцу не нужно вписывать ноль руками.
    """
    raw = str(form.get(name) or "").strip()
    if not raw:
        return default, ""
    try:
        value = int(raw)
    except ValueError:
        return default, f"{label}: нужно целое число"
    if not low <= value <= high:
        return default, f"{label}: допустимо от {low} до {high}"
    return value, ""


def _checkbox_flag(form: Any, name: str, *, default: bool) -> bool:  # noqa: ANN401
    """Флаг из формы.

    Отсутствие поля — это «не прислали» (так вызывают маршрут тесты и скрипты),
    и тогда работает значение по умолчанию. В форме чекбокс продублирован
    скрытым полем ``0``, поэтому снятая галочка приходит как явный ноль.
    """
    values = form.getlist(name)
    if not values:
        return default
    return str(values[-1]).strip().lower() in {"1", "on", "true", "yes", "да"}


def _actor(auth: Any) -> audit.Actor:  # noqa: ANN401 - Session
    return audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id)


# ------------------------------------------------------- рефералы и промокоды
async def _recent_invitations(db, limit: int = INVITATIONS_LIMIT) -> list[dict]:  # noqa: ANN001
    """Последние приглашения: кто привёл, кого и чем это закончилось."""
    rows = (
        await db.execute(
            select(Referral, User)
            .join(User, User.id == Referral.invited_id)
            .order_by(Referral.created_at.desc(), Referral.id.desc())
            .limit(limit)
        )
    ).all()
    if not rows:
        return []
    # Пригласивших подтягиваем одним запросом на список: иначе на 50 строк
    # получилось бы 50 запросов к базе.
    referrer_ids = {ref.referrer_id for ref, _ in rows}
    referrers = {user.id: user for user in await db.scalars(select(User).where(User.id.in_(referrer_ids)))}
    return [
        {"ref": ref, "invited": invited, "referrer": referrers.get(ref.referrer_id)}
        for ref, invited in rows
    ]


async def _redemptions_by_promo(db, promo_ids: list[int]) -> dict[int, list[dict]]:  # noqa: ANN001
    """Последние применения каждого промокода: кто и на сколько.

    Одним запросом на все коды страницы (``in_``) — список кодов длиной в сотню
    строк не должен превращаться в сотню запросов.
    """
    if not promo_ids:
        return {}
    rows = list(
        (
            await db.scalars(
                select(PromoRedemption)
                .where(PromoRedemption.promo_id.in_(promo_ids))
                .order_by(PromoRedemption.created_at.desc(), PromoRedemption.id.desc())
            )
        ).all()
    )
    if not rows:
        return {}
    users = {
        user.id: user
        for user in await db.scalars(select(User).where(User.id.in_({row.user_id for row in rows})))
    }
    grouped: dict[int, list[dict]] = {}
    for row in rows:
        bucket = grouped.setdefault(row.promo_id, [])
        if len(bucket) < REDEMPTIONS_PER_CODE:
            bucket.append({"row": row, "user": users.get(row.user_id)})
    return grouped


@router.get("/referrals", response_class=HTMLResponse)
async def referrals_page(request: Request):
    """Сводка по программе, промокоды (создание и список) и последние приглашения."""
    auth = await require(request, "growth.view")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        ref_stats = await referral_service.program_stats(db)
        code_stats = await promo_service.program_stats(db)
        tops = await referral_service.top_referrers(db, limit=TOP_REFERRERS_LIMIT)
        invitations = await _recent_invitations(db)
        codes = await promo_service.list_codes(db, limit=CODES_LIMIT)
        redemptions = await _redemptions_by_promo(db, [row.id for row in codes])

    return await page(
        request,
        "referrals.html",
        auth,
        title="Рефералы и промокоды",
        page="referrals",
        ref_stats=ref_stats,
        code_stats=code_stats,
        tops=tops,
        invitations=invitations,
        codes=codes,
        redemptions=redemptions,
        can_act=ui.can(auth.role, "growth.act"),
        bonus_referrer_days=settings.referral_bonus_days_referrer,
        bonus_invited_days=settings.referral_bonus_days_invited,
        discount_percent=settings.referral_discount_percent,
        discount_max_rub=settings.referral_discount_max_rub,
        rewards_per_month=settings.referral_max_rewards_per_month,
    )


@router.post("/referrals/promo")
async def promo_create(request: Request):
    """Создать промокод руками: акция, компенсация, договорённость с блогером."""
    auth = await require(request, "growth.act")
    if isinstance(auth, Response):
        return auth

    form = await request.form()
    code = promo_service.normalize(str(form.get("code") or ""))
    if not code:
        return flash_redirect("/admin/referrals", error="Укажи код промокода")
    if len(code) > 32:
        return flash_redirect("/admin/referrals", error="Код длиннее 32 символов — сократи")

    percent, error = _int_field(form, "percent", "Скидка, %", low=1, high=100, default=50)
    if error:
        return flash_redirect("/admin/referrals", error=error)
    uses, error = _int_field(form, "uses", "Активаций", low=0, high=100_000)
    if error:
        return flash_redirect("/admin/referrals", error=error)
    days, error = _int_field(form, "days", "Срок, дней", low=0, high=3650)
    if error:
        return flash_redirect("/admin/referrals", error=error)
    max_discount, error = _int_field(form, "max_discount", "Потолок скидки, ₽", low=0, high=100_000)
    if error:
        return flash_redirect("/admin/referrals", error=error)
    first_only = _checkbox_flag(form, "first_only", default=True)

    async with SessionMaker() as db:
        try:
            row = await promo_service.create_admin_code(
                db,
                code,
                percent=percent,
                uses_limit=uses,
                days=days,
                first_only=first_only,
                max_discount_rub=max_discount,
                note=f"создан в панели: {auth.name}",
            )
        except ValueError as exc:  # такой код уже есть или пустой
            return flash_redirect("/admin/referrals", error=f"Промокод не создан: {exc}")

        await audit.log_action(
            db,
            "admin.promo_created",
            actor=_actor(auth),
            payload={
                "code": row.code,
                "percent": percent,
                "uses_limit": uses,
                "days": days,
                "max_discount_rub": max_discount,
                "first_only": first_only,
            },
        )
        await db.commit()

    return flash_redirect("/admin/referrals", message=f"Промокод {row.code} создан")


@router.post("/referrals/promo/{promo_id}/toggle")
async def promo_toggle(promo_id: int, request: Request):
    """Включить или выключить промокод.

    Выключение не удаляет код: старые применения и суммы скидок должны
    остаться в отчёте, а сам код может понадобиться снова.
    """
    auth = await require(request, "growth.act")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        row = await db.get(PromoCode, promo_id)
        if row is None:
            return flash_redirect("/admin/referrals", error="Промокод не найден")

        row.is_active = not row.is_active
        code, state = row.code, row.is_active
        await audit.log_action(
            db,
            "admin.promo_toggled",
            actor=_actor(auth),
            payload={"promo_id": promo_id, "code": code, "is_active": state},
        )
        await db.commit()

    return flash_redirect(
        "/admin/referrals",
        message=f"Промокод {code} {'включён' if state else 'выключен'}",
    )


# -------------------------------------------------------------------- тарифы
@router.get("/plans", response_class=HTMLResponse)
async def plans_page(request: Request):
    """Тарифы: цены, лимиты и то, где клиент их видит."""
    auth = await require(request, "plans.manage")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        plans = list((await db.scalars(select(Plan).order_by(Plan.sort_order, Plan.price_rub))).all())
        # Сколько подписок висит на каждом тарифе — одним запросом на страницу.
        usage = {
            plan_id: int(total)
            for plan_id, total in (
                await db.execute(
                    select(Subscription.plan_id, func.count(Subscription.id)).group_by(Subscription.plan_id)
                )
            ).all()
            if plan_id is not None
        }

    return await page(
        request,
        "plans.html",
        auth,
        title="Тарифы",
        page="plans",
        plans=plans,
        usage=usage,
        active_total=sum(1 for plan in plans if plan.is_active),
        stars_enabled=settings.stars_enabled,
        manual_enabled=bool(settings.manual_payment_details),
        trial_days=settings.trial_days,
        trial_devices=settings.trial_devices,
        trial_gb=settings.trial_gb,
    )


@router.post("/plans/{plan_id}")
async def plan_save(plan_id: int, request: Request):
    """Сохранить тариф. Код тарифа не меняем: на него ссылаются заказы и бот."""
    auth = await require(request, "plans.manage")
    if isinstance(auth, Response):
        return auth

    form = await request.form()
    title = str(form.get("title") or "").strip()
    if not title:
        return flash_redirect("/admin/plans", error="У тарифа должно быть название")
    if len(title) > 64:
        return flash_redirect("/admin/plans", error="Название длиннее 64 символов — сократи")

    values: dict[str, int] = {}
    labels = {
        "days": "Дней",
        "price_rub": "Цена, ₽",
        "price_stars": "Цена в звёздах",
        "devices_limit": "Устройств",
        "traffic_limit_gb": "Трафик, ГБ",
        "sort_order": "Порядок",
    }
    for field, (low, high) in PLAN_LIMITS.items():
        value, error = _int_field(form, field, labels[field], low=low, high=high, default=0)
        if error:
            return flash_redirect("/admin/plans", error=error)
        values[field] = value

    async with SessionMaker() as db:
        plan = await db.get(Plan, plan_id)
        if plan is None:
            return flash_redirect("/admin/plans", error="Тариф не найден")

        before = {field: getattr(plan, field) for field in ("title", *PLAN_LIMITS)}
        plan.title = title
        for field, value in values.items():
            setattr(plan, field, value)
        after = {field: getattr(plan, field) for field in ("title", *PLAN_LIMITS)}

        await audit.log_action(
            db,
            "admin.plan_saved",
            actor=_actor(auth),
            payload={"plan_id": plan_id, "code": plan.code, "before": before, "after": after},
        )
        await db.commit()

    return flash_redirect("/admin/plans", message=f"Тариф «{title}» сохранён")


@router.post("/plans/{plan_id}/toggle")
async def plan_toggle(plan_id: int, request: Request):
    """Включить или выключить тариф.

    Последний активный тариф выключить нельзя: клиенту в боте нечего было бы
    предложить, а покупка молча ломалась бы.
    """
    auth = await require(request, "plans.manage")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        plan = await db.get(Plan, plan_id)
        if plan is None:
            return flash_redirect("/admin/plans", error="Тариф не найден")

        if plan.is_active:
            active = int(await db.scalar(select(func.count(Plan.id)).where(Plan.is_active.is_(True))) or 0)
            if active <= 1:
                return flash_redirect(
                    "/admin/plans",
                    error="Это последний активный тариф: клиентам нечего будет предложить. Сначала включи другой.",
                )

        plan.is_active = not plan.is_active
        code, state = plan.code, plan.is_active
        await audit.log_action(
            db,
            "admin.plan_saved",
            actor=_actor(auth),
            payload={"plan_id": plan_id, "code": code, "action": "toggle", "is_active": state},
        )
        await db.commit()

    return flash_redirect(
        "/admin/plans",
        message=f"Тариф {code} {'включён' if state else 'выключен'}",
    )


# ------------------------------------------------------------------ рассылки
def _audience_stmt(audience: str):  # noqa: ANN202 - SQLAlchemy Select
    """Кого касается рассылка. None — аудитория не из списка (ошибка, не «всем»).

    Определения намеренно простые и не пересекаются по смыслу:
      * ``active`` — подписка trial/active и ещё не истекла;
      * ``expired`` — подписка истекла или помечена истёкшей;
      * ``trials`` — прямо сейчас идёт пробный доступ;
      * ``all`` — все, кроме заблокированных.

    ``Subscription.user_id`` уникален, поэтому join не размножает строки.
    """
    now = datetime.now(timezone.utc)
    base = select(User.tg_id).where(User.is_blocked.is_(False))
    if audience == "active":
        return base.join(Subscription, Subscription.user_id == User.id).where(
            Subscription.status.in_(("trial", "active")),
            Subscription.expires_at > now,
        )
    if audience == "expired":
        return base.join(Subscription, Subscription.user_id == User.id).where(
            or_(Subscription.status == "expired", Subscription.expires_at <= now)
        )
    if audience == "trials":
        return base.join(Subscription, Subscription.user_id == User.id).where(
            Subscription.status == "trial",
            Subscription.expires_at > now,
        )
    if audience == "all":
        return base
    return None


async def _audience_ids(db, audience: str) -> list[int]:  # noqa: ANN001
    stmt = _audience_stmt(audience)
    if stmt is None:
        return []
    return [int(tg_id) for tg_id in (await db.scalars(stmt)).all() if tg_id]


async def _audience_count(db, audience: str) -> int:  # noqa: ANN001
    stmt = _audience_stmt(audience)
    if stmt is None:
        return 0
    return int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)


@router.get("/broadcast", response_class=HTMLResponse)
async def broadcast_page(request: Request):
    """Рассылка: форма, предпросмотр числа получателей и история отправок."""
    auth = await require(request, "broadcast.send")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        history = list(
            (
                await db.scalars(
                    select(Broadcast)
                    .order_by(Broadcast.created_at.desc(), Broadcast.id.desc())
                    .limit(BROADCAST_HISTORY_LIMIT)
                )
            ).all()
        )
        counts = {code: await _audience_count(db, code) for code, _ in AUDIENCES}

    return await page(
        request,
        "broadcast.html",
        auth,
        title="Рассылки",
        page="broadcast",
        audiences=AUDIENCES,
        audience_labels=dict(AUDIENCES),
        counts=counts,
        history=history,
        statuses=BROADCAST_STATUS,
        max_chars=BROADCAST_MAX_CHARS,
        bot_ready=getattr(request.app.state, "bot", None) is not None,
    )


@router.post("/broadcast")
async def broadcast_start(request: Request, text: str = Form(""), audience: str = Form("")):
    """Поставить рассылку в очередь и отдать отправку фоновой задаче.

    Маршрут не ждёт отправки: сотни сообщений с паузами — это минуты, и держать
    запрос открытым нельзя. Итог появится в истории, прогресс — там же.
    """
    auth = await require(request, "broadcast.send")
    if isinstance(auth, Response):
        return auth

    text = (text or "").strip()
    if not text:
        return flash_redirect("/admin/broadcast", error="Текст рассылки пустой")
    if len(text) > BROADCAST_MAX_CHARS:
        return flash_redirect(
            "/admin/broadcast",
            error=f"Текст длиннее {BROADCAST_MAX_CHARS} символов — Telegram не примет сообщение",
        )
    if audience not in AUDIENCE_CODES:
        return flash_redirect("/admin/broadcast", error="Выбери аудиторию из списка")

    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        # Без бота рассылка физически невозможна. Лучше отказать сразу и не
        # создавать запись, которая навсегда останется «с ошибкой».
        return flash_redirect("/admin/broadcast", error="Бот не подключён к веб-слою — отправлять некому")

    async with SessionMaker() as db:
        row = Broadcast(text=text, audience=audience, status="running", created_by=auth.name)
        db.add(row)
        await db.flush()
        broadcast_id = row.id
        await audit.log_action(
            db,
            "admin.broadcast_started",
            actor=_actor(auth),
            payload={"broadcast_id": broadcast_id, "audience": audience, "chars": len(text)},
        )
        await db.commit()

    task = asyncio.create_task(_run_broadcast(broadcast_id, bot))
    _TASKS.add(task)
    task.add_done_callback(_task_finished)

    return flash_redirect(
        "/admin/broadcast",
        message=f"Рассылка #{broadcast_id} запущена. Ход и итог — в истории ниже.",
    )


@router.post("/broadcast/{broadcast_id}/cancel")
async def broadcast_cancel(broadcast_id: int, request: Request):
    """Остановить рассылку. Фоновая задача проверяет статус перед каждым сообщением."""
    auth = await require(request, "broadcast.send")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        row = await db.get(Broadcast, broadcast_id)
        if row is None:
            return flash_redirect("/admin/broadcast", error="Рассылка не найдена")
        if row.status != "running":
            return flash_redirect("/admin/broadcast", error="Эта рассылка уже не идёт")

        row.status = "canceled"
        row.finished_at = utcnow()
        await audit.log_action(
            db,
            "admin.broadcast_canceled",
            actor=_actor(auth),
            payload={"broadcast_id": broadcast_id, "sent": row.sent, "total": row.total},
        )
        await db.commit()

    return flash_redirect("/admin/broadcast", message=f"Рассылка #{broadcast_id} остановлена")


async def _run_broadcast(broadcast_id: int, bot) -> None:  # noqa: ANN001 - aiogram Bot | None
    """Отправить рассылку в фоне и записать итог.

    Сессия своя: запрос панели к этому моменту уже закрыт, чужая сессия не
    пережила бы возврат ответа. Статус перечитываем перед каждым сообщением —
    иначе кнопка «Остановить» только рисовалась бы, а рассылка шла дальше.
    """
    async with SessionMaker() as db:
        row = await db.get(Broadcast, broadcast_id)
        if row is None:
            return

        if bot is None:
            row.status = "failed"
            row.error = "бот не подключён — отправлять некому"
            row.finished_at = utcnow()
            await db.commit()
            logger.error("Рассылка %s: бот недоступен", broadcast_id)
            return

        recipients = await _audience_ids(db, row.audience)
        row.total = len(recipients)
        await db.commit()

        sent = failed = 0
        status = "running"
        try:
            for index, tg_id in enumerate(recipients, start=1):
                await db.refresh(row)
                if row.status != "running":
                    break  # остановлено из панели
                try:
                    await bot.send_message(tg_id, row.text, disable_web_page_preview=True)
                    sent += 1
                except Exception as exc:  # noqa: BLE001 - один человек не должен рвать рассылку
                    failed += 1
                    logger.info("Рассылка %s: сообщение %s не ушло: %s", broadcast_id, tg_id, exc)
                if index % 10 == 0 or index == len(recipients):
                    row.sent, row.failed = sent, failed
                    await db.commit()
                await asyncio.sleep(BROADCAST_PAUSE_SECONDS)
        except asyncio.CancelledError:
            # Приложение выключается: сохраняем то, что успели, и не врём статусом.
            row.sent, row.failed = sent, failed
            row.status, row.finished_at = "canceled", utcnow()
            await db.commit()
            raise
        except Exception as exc:  # noqa: BLE001 - причину показываем владельцу
            row.sent, row.failed = sent, failed
            row.status, row.error, row.finished_at = "failed", str(exc)[:500], utcnow()
            await db.commit()
            logger.error("Рассылка %s упала: %s", broadcast_id, exc)
            status = "failed"
        else:
            row.sent, row.failed = sent, failed
            if row.status == "running":
                row.status = "done"
            row.finished_at = utcnow()
            await db.commit()
            status = row.status

        total = row.total

    # Уведомление — уже вне транзакции: оно не должно влиять на итог рассылки.
    label = BROADCAST_STATUS.get(status, (status, ""))[0]
    try:
        await notifications.notify_admins(
            bot,
            f"📣 Рассылка #{broadcast_id}: {label.lower()}. Доставлено {sent} из {total}, не дошло {failed}.",
        )
    except Exception as exc:  # noqa: BLE001 - уведомление не важнее рассылки
        logger.warning("Не смог уведомить админов о рассылке %s: %s", broadcast_id, exc)
