"""Клиенты: список, карточка, ручные операции и заметки.

Карточка клиента — главный ответ на вопрос поддержки «что у него вообще
происходило». Поэтому на одной странице: подписка, ссылка, заказы, события,
рефералка, заметки и метки, а действия — тут же, в два клика.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import String, cast, func, or_, select

from app.db.models import (
    Event,
    Order,
    Partner,
    PersonalLink,
    Plan,
    Referral,
    Subscription,
    User,
    UserNote,
)
from app.db.session import SessionMaker
from app.services import attribution, audit, stats as stats_service, subscriptions
from app.web import ui
from app.web.admin.common import filters, flash_redirect, make_page, notify, page, parse_page, require

logger = logging.getLogger(__name__)
router = APIRouter()

FILTER_KEYS = ("q", "status", "tag", "sort")

STATUS_TABS: tuple[tuple[str, str], ...] = (
    ("all", "Все"),
    ("trial", "Пробные"),
    ("active", "Активные"),
    ("expired", "Истёкшие"),
    ("blocked", "Заблокированные"),
    ("none", "Без подписки"),
)


def _search_condition(query: str):  # noqa: ANN202
    like = f"%{query.strip()}%"
    conditions = [User.username.ilike(like), User.first_name.ilike(like), cast(User.tg_id, String).like(like)]
    digits = "".join(ch for ch in query if ch.isdigit())
    if digits:
        conditions.append(cast(User.id, String) == digits)
    return or_(*conditions)


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    auth = await require(request, "users.view")
    if isinstance(auth, Response):
        return auth

    current = filters(request, FILTER_KEYS)
    status = current["status"] or "all"
    if status not in {code for code, _ in STATUS_TABS}:
        status = "all"
    page_no, per_page = parse_page(request, default_per_page=50)
    # Боковая карточка клиента: ?card=<id> открывает её рядом со списком,
    # не уводя со страницы (мастер-деталь). Без JS это просто ссылка.
    try:
        card_id = int(request.query_params.get("card") or 0)
    except ValueError:
        card_id = 0

    async with SessionMaker() as db:
        stmt = select(User, Subscription).outerjoin(Subscription, Subscription.user_id == User.id)
        if current["q"]:
            stmt = stmt.where(_search_condition(current["q"]))
        if current["tag"]:
            stmt = stmt.where(User.tags.ilike(f"%{current['tag']}%"))
        if status == "blocked":
            stmt = stmt.where(User.is_blocked.is_(True))
        elif status == "none":
            stmt = stmt.where(Subscription.id.is_(None))
        elif status != "all":
            stmt = stmt.where(Subscription.status == status)
        else:
            # «Все» — это все, включая заблокированных: их видно по метке.
            pass

        total = int(
            await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        )
        sort = current["sort"] or "new"
        order_by = {
            "new": User.id.desc(),
            "old": User.id.asc(),
            "expires": Subscription.expires_at.asc().nullslast(),
        }.get(sort, User.id.desc())
        rows = (
            await db.execute(stmt.order_by(order_by).limit(per_page).offset((page_no - 1) * per_page))
        ).all()

        items = [
            {
                "user": user,
                "sub": sub,
                "link": subscriptions.subscription_link(sub.subscription_token) if sub else "",
                "tags": ui.split_tags(user.tags),
                "orders": 0,
            }
            for user, sub in rows
        ]
        # Число оплат по клиенту — одним запросом на страницу, не по строке.
        if items:
            ids = [item["user"].id for item in items]
            paid = dict(
                (
                    await db.execute(
                        select(Order.user_id, func.count(Order.id))
                        .where(Order.user_id.in_(ids), Order.status == "paid")
                        .group_by(Order.user_id)
                    )
                ).all()
            )
            for item in items:
                item["orders"] = int(paid.get(item["user"].id, 0))

        counts = {
            "all": int(await db.scalar(select(func.count(User.id))) or 0),
            "blocked": int(
                await db.scalar(select(func.count(User.id)).where(User.is_blocked.is_(True))) or 0
            ),
        }
        snapshot = await stats_service.collect(db)
        tags = sorted(
            {
                tag
                for (raw,) in (await db.execute(select(User.tags).where(User.tags != "").distinct())).all()
                for tag in ui.split_tags(raw)
            }
        )
        card_data = None
        if card_id:
            card_user = await db.get(User, card_id)
            if card_user is not None:
                card_data = await _card_context(
                    db, card_user, orders_limit=6, events_limit=8, notes_limit=4
                )

    # Ссылку на карточку собирает шаблон: ему доступен query_string с фильтрами.
    return await page(
        request,
        "users.html",
        auth,
        title="Пользователи",
        page="users",
        items=items,
        current=current,
        status=status,
        tabs=STATUS_TABS,
        counts=counts,
        tags=tags,
        stats=snapshot,
        card=card_data,
        card_id=card_id,
        page_data=make_page(items, total, page_no, per_page),
    )


@router.get("/users/{user_id}", response_class=HTMLResponse)
async def user_card(user_id: int, request: Request):
    auth = await require(request, "users.view")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        user = await db.get(User, user_id)
        if user is None:
            return flash_redirect("/admin/users", error="Пользователь не найден")
        context = await _card_context(db, user)

    return await page(
        request,
        "user_card.html",
        auth,
        title=user.display_name,
        page="users",
        **context,
    )


async def _card_context(
    db,  # noqa: ANN001 - AsyncSession
    user: User,
    *,
    orders_limit: int = 25,
    events_limit: int = 30,
    notes_limit: int = 0,
) -> dict:
    """Данные карточки клиента: подписка, заказы, события, рефералка, суммы.

    Одним помощником пользуются и полная страница клиента, и боковая карточка
    в списке: иначе два экрана рано или поздно начнут показывать разное.
    Лимиты — потому что боковой панели не нужны все 30 событий клиента.
    """
    sub = await subscriptions.get_subscription(db, user.id)
    plan = await db.get(Plan, sub.plan_id) if sub and sub.plan_id else None
    order_rows = list(
        (
            await db.scalars(
                select(Order).where(Order.user_id == user.id).order_by(Order.id.desc()).limit(orders_limit)
            )
        ).all()
    )
    plans_by_id = (
        {
            p.id: p
            for p in await db.scalars(
                select(Plan).where(Plan.id.in_({o.plan_id for o in order_rows if o.plan_id}))
            )
        }
        if order_rows
        else {}
    )
    orders = [{"order": order, "plan": plans_by_id.get(order.plan_id)} for order in order_rows]

    user_events = list(
        (
            await db.scalars(
                select(Event).where(Event.user_id == user.id).order_by(Event.id.desc()).limit(events_limit)
            )
        ).all()
    )
    notes = (
        list(
            (
                await db.scalars(
                    select(UserNote)
                    .where(UserNote.user_id == user.id)
                    .order_by(UserNote.id.desc())
                    .limit(notes_limit)
                )
            ).all()
        )
        if notes_limit
        else []
    )
    invited = int(await db.scalar(select(func.count(Referral.id)).where(Referral.referrer_id == user.id)) or 0)
    invited_paid = int(
        await db.scalar(
            select(func.count(Referral.id)).where(
                Referral.referrer_id == user.id, Referral.paid_order_id.is_not(None)
            )
        )
        or 0
    )
    referrer_row = await db.scalar(select(Referral).where(Referral.invited_id == user.id))
    referrer = await db.get(User, referrer_row.referrer_id) if referrer_row else None

    # Откуда человек пришёл: источник привлечения и, если есть, конкретная ссылка.
    # Без этого в карточке не видно, по чьей рекомендации клиент появился.
    partner_row = await db.get(Partner, user.partner_id) if user.partner_id else None
    personal_link = (
        await db.get(PersonalLink, user.personal_link_id) if user.personal_link_id else None
    )
    source_title = attribution.SOURCE_TITLES.get(user.source or "", "") if user.source else ""
    paid_total = int(
        await db.scalar(
            select(func.coalesce(func.sum(Order.amount_rub), 0)).where(
                Order.user_id == user.id, Order.status == "paid"
            )
        )
        or 0
    )
    refunded_total = int(
        await db.scalar(
            select(func.coalesce(func.sum(Order.amount_rub), 0)).where(
                Order.user_id == user.id, Order.status == "refunded"
            )
        )
        or 0
    )

    return {
        "user": user,
        "sub": sub,
        "plan": plan,
        "link": subscriptions.subscription_link(sub.subscription_token) if sub else "",
        "orders": orders,
        "events": user_events,
        "notes": notes,
        "tags": ui.split_tags(user.tags),
        "invited": invited,
        "invited_paid": invited_paid,
        "referrer": referrer,
        "partner_row": partner_row,
        "personal_link": personal_link,
        "source_title": source_title,
        "paid_total": paid_total,
        "refunded_total": refunded_total,
        "plans": await active_plans(db),
        "status_label": ui.status_pair(sub.status if sub else None),
    }


# ------------------------------------------------------------------ действия
async def _load_user(db, user_id: int) -> User | None:  # noqa: ANN001
    return await db.get(User, user_id)


@router.post("/users/{user_id}/grant")
async def user_grant(user_id: int, request: Request, days: int = Form(0), reason: str = Form("")):
    auth = await require(request, "users.act")
    if isinstance(auth, Response):
        return auth

    return await _change_days(request, auth, user_id, abs(days), reason, direction="grant")


@router.post("/users/{user_id}/write-off")
async def user_write_off(user_id: int, request: Request, days: int = Form(0), reason: str = Form("")):
    auth = await require(request, "users.act")
    if isinstance(auth, Response):
        return auth

    return await _change_days(request, auth, user_id, -abs(days), reason, direction="write_off")


async def _change_days(request: Request, auth, user_id: int, days: int, reason: str, *, direction: str):  # noqa: ANN001
    if days == 0:
        return flash_redirect(f"/admin/users/{user_id}", error="Укажи число дней")

    async with SessionMaker() as db:
        user = await db.get(User, user_id)
        if user is None:
            return flash_redirect("/admin/users", error="Пользователь не найден")
        try:
            sub, to_balance, problem = await subscriptions.adjust_days(
                db, user, days, await subscriptions.all_user_panels(db), reason=reason or direction
            )
        except Exception as exc:  # noqa: BLE001 - панель может не ответить
            await db.rollback()
            logger.error("Смена дней у %s не удалась: %s", user_id, exc)
            return flash_redirect(f"/admin/users/{user_id}", error=f"Панель недоступна: {exc}")

        if problem:
            await db.rollback()
            return flash_redirect(f"/admin/users/{user_id}", error=problem)

        await audit.log_action(
            db,
            "admin.grant_days" if days > 0 else "admin.write_off_days",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            user_id=user.id,
            payload={"days": days, "reason": reason, "to_balance": to_balance, "sub_id": sub.id if sub else None},
        )
        await db.commit()
        tg_id = user.tg_id
        name = user.display_name
        balance = int(user.bonus_days_balance or 0)
        expires = sub.expires_at if sub else None

    if days > 0:
        if to_balance:
            await notify(
                request,
                tg_id,
                f"🎁 Тебе начислено {days} дн. доступа. Дни добавятся к подписке при следующей оплате.",
            )
            message = f"{name}: +{days} дн. в накопительный баланс (всего {balance})"
        else:
            await notify(
                request,
                tg_id,
                f"🎁 Тебе начислено {days} дн. доступа. Подписка активна до {expires:%d.%m.%Y %H:%M}.",
            )
            message = f"{name}: +{days} дн., подписка до {expires:%d.%m.%Y %H:%M}"
    else:
        await notify(request, tg_id, f"Срок доступа изменён на {days} дн. Если это ошибка — напиши в поддержку.")
        message = f"{name}: {days} дн., подписка до {expires:%d.%m.%Y %H:%M}" if expires else f"{name}: дни списаны"

    return flash_redirect(f"/admin/users/{user_id}", message=message)


@router.post("/users/{user_id}/block")
async def user_block(user_id: int, request: Request, block: int = Form(1), reason: str = Form("")):
    auth = await require(request, "users.act")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        user = await db.get(User, user_id)
        if user is None:
            return flash_redirect("/admin/users", error="Пользователь не найден")
        user.is_blocked = bool(block)
        sub = await subscriptions.get_subscription(db, user.id)
        if sub is not None:
            # Блокировка рубит и доступ, и бота; разблокировка возвращает статус,
            # который был до неё (пробный остаётся пробным).
            await subscriptions.set_enabled(sub, await subscriptions.all_user_panels(db), enabled=not block)
        await audit.log_action(
            db,
            "admin.block_user" if block else "admin.unblock_user",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            user_id=user.id,
            payload={"reason": reason},
        )
        await db.commit()
        name = user.display_name
        tg_id = user.tg_id

    if not block:
        await notify(request, tg_id, "Доступ восстановлен. Если что-то не подключается — напиши в поддержку.")
    action = "заблокирован" if block else "разблокирован"
    return flash_redirect(f"/admin/users/{user_id}", message=f"{name} {action}")


@router.post("/users/{user_id}/revoke")
async def user_revoke(user_id: int, request: Request, reason: str = Form("")):
    """Отозвать доступ, не блокируя клиента в боте (шеринг, возврат, спор)."""
    auth = await require(request, "users.act")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        user = await db.get(User, user_id)
        if user is None:
            return flash_redirect("/admin/users", error="Пользователь не найден")
        sub = await subscriptions.get_subscription(db, user.id)
        if sub is None:
            return flash_redirect(f"/admin/users/{user_id}", error="У клиента нет подписки")
        try:
            await subscriptions.revoke_access(db, sub, await subscriptions.all_user_panels(db), reason=reason)
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            return flash_redirect(f"/admin/users/{user_id}", error=f"Не получилось отозвать доступ: {exc}")
        await audit.log_action(
            db,
            "admin.revoke_access",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            user_id=user.id,
            payload={"reason": reason},
        )
        await db.commit()
        tg_id = user.tg_id
        name = user.display_name

    await notify(
        request,
        tg_id,
        "Доступ по подписке приостановлен. Если это ошибка — напиши в поддержку, разберёмся быстро.",
    )
    return flash_redirect(f"/admin/users/{user_id}", message=f"{name}: доступ отозван")


@router.post("/users/{user_id}/restore")
async def user_restore(user_id: int, request: Request):
    auth = await require(request, "users.act")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        user = await db.get(User, user_id)
        if user is None:
            return flash_redirect("/admin/users", error="Пользователь не найден")
        user.is_blocked = False
        sub = await subscriptions.get_subscription(db, user.id)
        if sub is None:
            return flash_redirect(f"/admin/users/{user_id}", error="У клиента нет подписки")
        await subscriptions.restore_access(db, sub, await subscriptions.all_user_panels(db), reason="admin")
        await audit.log_action(
            db,
            "admin.unblock_user",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            user_id=user.id,
            payload={"action": "restore"},
        )
        await db.commit()
        tg_id = user.tg_id
        name = user.display_name

    await notify(request, tg_id, "Доступ восстановлен. Если что-то не подключается — напиши в поддержку.")
    return flash_redirect(f"/admin/users/{user_id}", message=f"{name}: доступ восстановлен")


@router.post("/users/{user_id}/tags")
async def user_tags(user_id: int, request: Request, tags: str = Form("")):
    auth = await require(request, "users.act")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        user = await db.get(User, user_id)
        if user is None:
            return flash_redirect("/admin/users", error="Пользователь не найден")
        before = user.tags or ""
        user.tags = ui.join_tags(ui.split_tags(tags))[:128]
        await audit.log_action(
            db,
            "admin.tags_changed",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            user_id=user.id,
            payload={"before": before, "after": user.tags},
        )
        await db.commit()

    return flash_redirect(f"/admin/users/{user_id}", message="Метки сохранены")


@router.post("/users/{user_id}/notes")
async def user_add_note(user_id: int, request: Request, text: str = Form("")):
    auth = await require(request, "users.notes")
    if isinstance(auth, Response):
        return auth

    text = (text or "").strip()
    if not text:
        return flash_redirect(f"/admin/users/{user_id}", error="Пустая заметка")

    async with SessionMaker() as db:
        user = await db.get(User, user_id)
        if user is None:
            return flash_redirect("/admin/users", error="Пользователь не найден")
        db.add(UserNote(user_id=user.id, author=auth.name, author_role=auth.role, text=text[:4000]))
        await audit.log_action(
            db,
            "admin.note_added",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            user_id=user.id,
            payload={"text": text[:200]},
        )
        await db.commit()

    return flash_redirect(f"/admin/users/{user_id}", message="Заметка добавлена")


@router.post("/users/{user_id}/notes/{note_id}/delete")
async def user_delete_note(user_id: int, note_id: int, request: Request):
    auth = await require(request, "users.notes")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        note = await db.get(UserNote, note_id)
        if note is None or note.user_id != user_id:
            return flash_redirect(f"/admin/users/{user_id}", error="Заметка не найдена")
        await db.delete(note)
        await audit.log_action(
            db,
            "admin.note_added",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            user_id=user_id,
            payload={"deleted_note_id": note_id},
        )
        await db.commit()

    return flash_redirect(f"/admin/users/{user_id}", message="Заметка удалена")


@router.post("/users/{user_id}/message")
async def user_message(user_id: int, request: Request, text: str = Form("")):
    """Личное сообщение клиенту от поддержки."""
    auth = await require(request, "users.notes")
    if isinstance(auth, Response):
        return auth

    text = (text or "").strip()
    if not text:
        return flash_redirect(f"/admin/users/{user_id}", error="Пустое сообщение")

    async with SessionMaker() as db:
        user = await db.get(User, user_id)
        if user is None:
            return flash_redirect("/admin/users", error="Пользователь не найден")
        tg_id = user.tg_id

    sent = await notify(request, tg_id, text)
    if not sent:
        return flash_redirect(f"/admin/users/{user_id}", error="Бот не смог отправить сообщение (клиент закрыл чат?)")
    return flash_redirect(f"/admin/users/{user_id}", message="Сообщение отправлено")


@router.post("/users/{user_id}/extend-plan")
async def user_extend_plan(user_id: int, request: Request, plan_id: int = Form(0)):
    """Продлить доступ по тарифу без оплаты (компенсация, договорённость)."""
    auth = await require(request, "users.act")
    if isinstance(auth, Response):
        return auth

    async with SessionMaker() as db:
        user = await db.get(User, user_id)
        plan = await db.get(Plan, plan_id) if plan_id else None
        if user is None or plan is None:
            return flash_redirect(f"/admin/users/{user_id}", error="Не нашёл клиента или тариф")
        sub, to_balance, problem = await subscriptions.adjust_days(
            db, user, plan.days, await subscriptions.all_user_panels(db), reason=f"admin_plan:{plan.code}"
        )
        if problem:
            await db.rollback()
            return flash_redirect(f"/admin/users/{user_id}", error=problem)
        await audit.log_action(
            db,
            "admin.grant_days",
            actor=audit.Actor(name=auth.name, role=auth.role, tg_id=auth.tg_id),
            user_id=user.id,
            payload={"plan": plan.code, "days": plan.days},
        )
        await db.commit()
        tg_id = user.tg_id
        name = user.display_name

    if to_balance:
        await notify(request, tg_id, f"🎁 Тариф «{plan.title}» активирован: {plan.days} дн. добавлены в баланс.")
    else:
        await notify(request, tg_id, f"🎁 Тариф «{plan.title}» активирован без оплаты. Спасибо, что с нами!")
    return flash_redirect(f"/admin/users/{user_id}", message=f"{name}: выдан тариф «{plan.title}» ({plan.days} дн.)")


# Планы нужны в карточке для кнопки «выдать тариф» — грузим в шаблоне отдельно.
async def active_plans(db) -> list[Plan]:  # noqa: ANN001
    return list(await db.scalars(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order)))
