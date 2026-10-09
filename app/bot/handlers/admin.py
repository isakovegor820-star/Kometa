"""Админские команды: статистика, подтверждение платежей, управление доступом."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.bot.filters import IsAdmin
from app.config import get_settings
from app.db.models import Order, PromoCode, Subscription, User
from app.panels.base import panel_label
from app.panels.registry import registry
from app.services import (
    downtime,
    notifications,
    orders,
    promo,
    referral,
    stats,
    subscriptions,
    watchdog,
)

logger = logging.getLogger(__name__)
router = Router(name="admin")
settings = get_settings()
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class Broadcast(StatesGroup):
    text = State()


def web_panel_url() -> str:
    """Публичный адрес админ-панели (пусто, если он локальный)."""
    base = settings.public_base_url.rstrip("/")
    if not base or "127.0.0.1" in base or "localhost" in base:
        return ""
    return f"{base}/admin"


@router.message(Command("admin"))
async def cmd_admin(message: Message, session: AsyncSession) -> None:
    snapshot = await stats.collect(session)
    url = web_panel_url()
    text = snapshot.as_text()
    if not settings.admin_panel_password:
        text += "\n\n Веб-панель выключена: задай <code>ADMIN_PANEL_PASSWORD</code> в .env"
    elif url:
        text += f"\n\n Веб-панель: {url}"
    else:
        text += (
            f"\n\n Веб-панель: <code>{settings.public_base_url.rstrip('/')}/admin</code>"
            "\n(адрес локальный — с телефона не откроется, пока не будет домена)"
        )
    await message.answer(text, reply_markup=keyboards.admin_panel_kb(pending_count=snapshot.pending_orders, panel_url=url or None))


@router.message(Command("stats"))
async def cmd_stats(message: Message, session: AsyncSession) -> None:
    await message.answer((await stats.collect(session)).as_text())


@router.callback_query(F.data == "admin:stats")
async def cb_stats(call: CallbackQuery, session: AsyncSession) -> None:
    await call.message.edit_text((await stats.collect(session)).as_text(), reply_markup=keyboards.back_to_menu_kb())
    await call.answer()


@router.callback_query(F.data == "admin:orders")
@router.message(Command("orders"))
async def show_pending_orders(event: Message | CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    pending = await orders.pending_orders(session)
    target = event.message if isinstance(event, CallbackQuery) else event
    if not pending:
        await target.answer("Ожидающих заявок нет ✅")
    else:
        for order in pending:
            user = await session.get(User, order.user_id)
            plan = await orders.get_plan(session, order.plan_id) if order.plan_id else None
            await target.answer(
                texts.ADMIN_NEW_ORDER.format(
                    order_id=order.id,
                    user=notifications.safe(user.display_name) if user else "—",
                    tg_id=user.tg_id if user else "—",
                    plan=plan.title if plan else "—",
                    amount=order.amount_rub,
                    provider=order.provider,
                ),
                reply_markup=keyboards.admin_order_kb(order.id),
            )
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.callback_query(F.data.startswith("admin:confirm:"))
async def cb_confirm_order(call: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    order_id = int(call.data.rsplit(":", 1)[1])
    order = await orders.get_order(session, order_id)
    if order is None:
        await call.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    if order.status != "pending":
        await call.answer(f"Заказ уже в статусе {order.status}", show_alert=True)
        return

    user = await session.get(User, order.user_id)
    panel = await subscriptions.all_user_panels(session)
    sub, already = await orders.mark_paid(session, order, panel, confirmed_by=call.from_user.id, bot=bot)
    await call.message.edit_text(texts.ADMIN_CONFIRMED.format(order_id=order.id))
    await call.answer("Подтверждено ✅")

    # Подарочный заказ: покупателю уходит сертификат, а не доступ.
    if not already and order.gift_token:
        from app.services import gift as gift_service

        sent = await gift_service.notify_buyer(session, order, bot, user)
        if not sent:
            await notifications.notify_admins(
                bot,
                f"⚠️ Заказ-подарок #{order.id} оплачен, но сертификат не доставлен "
                f"(покупатель <code>{user.tg_id if user else '—'}</code>). Напиши ему вручную.",
            )
        return

    if user is not None and sub is not None and not already:
        expires = sub.expires_at.strftime("%d.%m.%Y %H:%M") if sub.expires_at else "—"
        await bot.send_message(
            user.tg_id,
            texts.order_paid_text(
                order,
                expires=expires,
                days=sub.days_left,
                link=subscriptions.subscription_link(sub.subscription_token),
            ),
        )
        await bot.send_message(
            user.tg_id,
            texts.SUBSCRIPTION_LINK_HINT.format(link=subscriptions.subscription_link(sub.subscription_token)),
        )


@router.callback_query(F.data.startswith("admin:reject:"))
async def cb_reject_order(call: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    order_id = int(call.data.rsplit(":", 1)[1])
    order = await orders.get_order(session, order_id)
    if order is None:
        await call.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    await orders.cancel_order(session, order, reason=f"rejected by {call.from_user.id}")
    await call.message.edit_text(texts.ADMIN_REJECTED.format(order_id=order.id))
    await call.answer("Отклонено")
    user = await session.get(User, order.user_id)
    if user is not None:
        try:
            await bot.send_message(
                user.tg_id,
                f"Заказ #{order.id} отклонён. Если оплата прошла — напиши в поддержку, разберёмся.",
            )
        except Exception: # noqa: BLE001
            logger.warning("Не смог уведомить пользователя %s", user.tg_id)


@router.message(Command("user"))
async def cmd_user(message: Message, session: AsyncSession) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Использование: <code>/user 123456789</code>")
        return
    user = await subscriptions.get_user_by_tg(session, int(parts[1]))
    if user is None:
        await message.answer("Пользователь не найден")
        return
    sub = await subscriptions.get_subscription(session, user.id)
    orders_list = (
        await session.scalars(select(Order).where(Order.user_id == user.id).order_by(Order.created_at.desc()).limit(5))
    ).all()
    lines = [
        f"👤 <b>{notifications.safe(user.display_name)}</b> (<code>{user.tg_id}</code>)",
        f"Создан: {user.created_at:%d.%m.%Y}",
        f"Код: <code>{user.referral_code}</code>",
        f"Подписка: {sub.status if sub else '—'}",
    ]
    if sub:
        lines.append(f"До: {sub.expires_at:%d.%m.%Y %H:%M} ({sub.days_left} дн.)")
        lines.append(f"Ссылка: <code>{subscriptions.subscription_link(sub.subscription_token)}</code>")
    invited, paid = await referral.referral_stats(session, user)
    earned = await referral.earned_days(session, user)
    lines.append(
        f"Рефералка: пригласил {invited}, оплатили {paid}, заработал {earned} дн."
        + (f" (+{user.bonus_days_balance} в запасе)" if user.bonus_days_balance else "")
    )
    if orders_list:
        lines.append("\nПоследние заказы:")
        lines.extend(
            f"#{o.id} {o.amount_rub} ₽{f' (скидка {o.discount_rub})' if o.discount_rub else ''} — "
            f"{o.status} ({o.provider})"
            for o in orders_list
        )
    await message.answer("\n".join(lines))


@router.message(Command("grant"))
async def cmd_grant(message: Message, session: AsyncSession, bot: Bot) -> None:
    parts = (message.text or "").split()
    if len(parts) < 3 or not parts[1].isdigit() or not parts[2].lstrip("-").isdigit():
        await message.answer("Использование: <code>/grant 123456789 7</code> (дней, можно отрицательное)")
        return
    tg_id, days = int(parts[1]), int(parts[2])
    user = await subscriptions.get_user_by_tg(session, tg_id)
    if user is None:
        await message.answer("Пользователь не найден")
        return
    panel = await subscriptions.all_user_panels(session)
    if days >= 0:
        sub, accrued = await subscriptions.add_bonus_days(
            session, user, days, panel, reason=f"admin:{message.from_user.id}"
        )
        if accrued:
            await message.answer(
                f"✅ {notifications.safe(user.display_name)}: подписки нет, {accrued} дн. в запасе "
                f"(добавятся при подключении)"
            )
            return
    else:
        sub = await subscriptions.get_subscription(session, user.id)
        if sub is None:
            await message.answer("У пользователя нет подписки")
            return
        sub.expires_at = sub.expires_at + timedelta(days=days)
    if sub is None:
        await message.answer("Не удалось изменить подписку")
        return
    await message.answer(
        f"✅ {notifications.safe(user.display_name)}: до {sub.expires_at:%d.%m.%Y %H:%M}"
    )
    try:
        await bot.send_message(user.tg_id, f"Тебе начислено {days} дн. доступа. Спасибо, что с нами ")
    except Exception: # noqa: BLE001
        pass


async def _notify_compensation(bot: Bot, result: downtime.GrantResult) -> str:
    """Сообщить клиентам о компенсации и вернуть отчёт администратору.

    Сообщение сервисное (не рассылка-реклама): клиент видит, что дни не
    потерялись. Ошибки отправки не считаем сбоем — бота могли заблокировать.
    """
    if result.days <= 0:
        return "Период закрыт: прошло меньше суток, начислять нечего."
    sent = 0
    for tg_id in result.tg_ids:
        try:
            await bot.send_message(tg_id, texts.DOWNTIME_COMPENSATION.format(days=result.days))
            sent += 1
        except Exception: # noqa: BLE001 - клиент мог заблокировать бота
            continue
    return f"✅ {result.as_text()}\nСообщение доставлено: {sent}"


@router.message(Command("downtime"))
async def cmd_downtime(message: Message, session: AsyncSession) -> None:
    """Статус компенсации простоя: открытый период и история начислений."""
    opened = await downtime.current(session)
    history = await downtime.recent(session, 5)

    if opened is None:
        head = "Открытого периода простоя нет."
    else:
        hours = max(0, int((datetime.now(timezone.utc) - opened.started_at).total_seconds() // 3600))
        head = (
            f"🛟 Простой открыт с <b>{opened.started_at:%d.%m %H:%M}</b> UTC ({hours} ч).\n"
            "Когда связь вернётся: <code>/downtime_end</code> — подписки продлятся на полные сутки."
        )

    if history:
        rows = []
        for period in history:
            mark = "✅" if period.granted_at else "…"
            amount = f"{period.days} дн." if period.days else "без начисления"
            note = f" · {period.note}" if period.note else ""
            rows.append(f"{mark} {period.started_at:%d.%m %H:%M} → {amount}{note}")
        history_text = "\n".join(rows)
    else:
        history_text = "компенсаций ещё не было"

    await message.answer(
        f"{head}\n\n<b>История (UTC)</b>\n{history_text}\n\n"
        "Ручное начисление: <code>/downtime_grant 3 причина</code>"
    )


@router.message(Command("downtime_start"))
async def cmd_downtime_start(message: Message, session: AsyncSession) -> None:
    """Открыть период простоя: связь у абонентов не работает."""
    note = (message.text or "").partition(" ")[2].strip()
    period, created = await downtime.start(session, note=note, actor=f"admin:{message.from_user.id}")
    if not created:
        await message.answer(
            f"Период уже открыт с {period.started_at:%d.%m %H:%M} UTC. "
            "Закрыть: <code>/downtime_end</code>"
        )
        return
    await message.answer(
        f"🛟 Простой открыт ({period.started_at:%d.%m %H:%M} UTC).\n"
        "Когда связь вернётся — <code>/downtime_end</code>: подписки продлятся на полные сутки "
        "и клиентам уйдёт сообщение."
    )


@router.message(Command("downtime_end"))
async def cmd_downtime_end(message: Message, session: AsyncSession, bot: Bot) -> None:
    """Закрыть период простоя и начислить компенсацию всем активным подпискам."""
    parts = (message.text or "").split()
    explicit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    panels = await subscriptions.all_user_panels(session)
    result = await downtime.finish(
        session, actor=f"admin:{message.from_user.id}", days=explicit, panels=panels
    )
    if not result.ok:
        await message.answer(f"⚠️ {result.as_text()}")
        return
    await message.answer(await _notify_compensation(bot, result))


@router.message(Command("downtime_grant"))
async def cmd_downtime_grant(message: Message, session: AsyncSession, bot: Bot) -> None:
    """Начислить дни вручную, без периода: разовая компенсация."""
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/downtime_grant 3 причина</code>")
        return
    days = int(parts[1])
    note = " ".join(parts[2:]).strip()
    panels = await subscriptions.all_user_panels(session)
    result = await downtime.grant(
        session, days, note=note, actor=f"admin:{message.from_user.id}", panels=panels
    )
    if not result.ok:
        await message.answer(f"⚠️ {result.as_text()}")
        return
    await message.answer(await _notify_compensation(bot, result))


@router.message(Command("emergency"))
async def cmd_emergency(message: Message) -> None:
    """Готов ли аварийный уровень: каналы, пробы, пинг, точка замера.

    Тот же отчёт, что и ``python -m app.tools.emergency_check`` на сервере,
    но с телефона: удобно проверять перед тем, как обещать клиентам резерв.
    """
    from app.tools import emergency_check

    report = await emergency_check.run()
    await message.answer(report.as_text())


@router.message(Command("referrals"))
async def cmd_referrals(message: Message, session: AsyncSession) -> None:
    """Сводка по рефералке: сколько привели, сколько оплатили, во что обошлось."""
    ref_stats = await referral.program_stats(session)
    code_stats = await promo.program_stats(session)
    lines = [
        "👥 <b>Реферальная программа</b>\n",
        f"Пришли по ссылкам: <b>{ref_stats['invited']}</b>",
        f"Из них оплатили: <b>{ref_stats['paid']}</b> ({ref_stats['conversion']}%)",
        f"Начислено дней: <b>{ref_stats['days_rewarded']}</b> "
        f"(в запасе {ref_stats['days_in_balance']})",
        f"Скидок активировано: <b>{code_stats['used']}</b> на <b>{code_stats['discount_total']} ₽</b>",
    ]
    tops = await referral.top_referrers(session)
    if tops:
        lines.append("\n<b>Топ пригласивших</b>")
        lines.extend(
            f"• {notifications.safe(user.display_name)} — привёл {invited}, оплатили {paid}"
            for user, invited, paid in tops
        )
    await message.answer("\n".join(lines))


@router.message(Command("promos"))
async def cmd_promos(message: Message, session: AsyncSession) -> None:
    codes = await promo.list_codes(session, limit=30)
    if not codes:
        await message.answer("Промокодов пока нет. Создать: <code>/promo LAUNCH50 50 100 30</code>")
        return
    lines = ["🎟 <b>Промокоды</b>\n"]
    for row in codes:
        left = "∞" if row.uses_left is None else row.uses_left
        status = "✅" if row.is_active else "⛔️"
        kind = {"referral": "реф.", "partner": "партн."}.get(row.kind, "адм.")
        lines.append(f"{status} <code>{row.code}</code> ({kind}) — {row.percent}%, осталось {left}")
    lines.append(
        "\nСоздать: <code>/promo КОД ПРОЦЕНТ [активаций] [дней]</code>"
        "\nВыключить: <code>/promo_off КОД</code>"
    )
    await message.answer("\n".join(lines))


@router.message(Command("promo"))
async def cmd_promo_new(message: Message, session: AsyncSession) -> None:
    """Создать обычный промокод: /promo LAUNCH50 50 100 30."""
    parts = (message.text or "").split()
    if len(parts) < 3 or not parts[2].isdigit():
        await message.answer(
            "Использование: <code>/promo LAUNCH50 50 100 30</code>\n"
            "(код, процент скидки, сколько активаций, сколько дней живёт)"
        )
        return
    code_raw = parts[1]
    percent = int(parts[2])
    uses = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    days = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
    try:
        row = await promo.create_admin_code(
            session,
            code_raw,
            percent=percent,
            uses_limit=uses,
            days=days,
            note=f"создал {message.from_user.id}",
        )
    except ValueError as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    await message.answer(
        f"✅ Промокод <code>{row.code}</code>: скидка {row.percent}% на первую оплату\n"
        f"Активаций: {'без ограничения' if not row.uses_limit else row.uses_limit}, "
        f"срок: {'бессрочно' if row.expires_at is None else f'{days} дн.'}\n\n"
        "Клиент вводит его в разделе «Тарифы» → «🎟 У меня есть промокод»."
    )


@router.message(Command("promo_off"))
async def cmd_promo_off(message: Message, session: AsyncSession) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: <code>/promo_off LAUNCH50</code>")
        return
    row = await session.scalar(select(PromoCode).where(PromoCode.code == promo.normalize(parts[1])))
    if row is None:
        await message.answer("Такого промокода нет")
        return
    row.is_active = False
    await session.flush()
    await message.answer(f"⛔️ Промокод <code>{row.code}</code> выключен")


@router.message(Command("block"))
@router.message(Command("unblock"))
async def cmd_block(message: Message, session: AsyncSession) -> None:
    block = (message.text or "").startswith("/block")
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/block 123456789</code>")
        return
    user = await subscriptions.get_user_by_tg(session, int(parts[1]))
    if user is None:
        await message.answer("Пользователь не найден")
        return
    user.is_blocked = block
    sub = await subscriptions.get_subscription(session, user.id)
    if sub is not None:
        await subscriptions.set_enabled(sub, await subscriptions.all_user_panels(session), enabled=not block)
    await message.answer(
        f"{'⛔️ Заблокирован' if block else '✅ Разблокирован'}: "
        f"{notifications.safe(user.display_name)}"
    )


@router.message(Command("watch"))
async def cmd_watch(message: Message) -> None:
    """Проверить клиентов панели прямо сейчас (не дожидаясь суточной задачи)."""
    report = await watchdog.run_watch(registry.primary())
    if report is None:
        await message.answer("⚠️ Панель не отдала список клиентов — проверь доступ и попробуй снова.")
        return
    await message.answer(watchdog.format_report(report))


@router.message(Command("sync"))
async def cmd_sync(message: Message, session: AsyncSession) -> None:
    """Раздать активных клиентов на все панели (после подключения новой страны)."""
    panels = await subscriptions.all_user_panels(session)
    checked, failed = await subscriptions.sync_all_subscriptions(session, panels)

    lines = [
        "🔄 <b>Синхронизация клиентов</b>",
        f"Панелей: <b>{len(panels)}</b> — " + ", ".join(
            getattr(p, "location_title", "") or p.name for p in panels
        ),
        f"Проверено подписок: <b>{checked}</b>",
    ]
    lines.append(
        f"⚠️ Не ответили: {', '.join(failed)}" if failed else "✅ Все панели приняли клиентов"
    )
    await message.answer("\n".join(lines))


@router.message(Command("nodes"))
@router.callback_query(F.data == "admin:nodes")
async def cmd_nodes(event: Message | CallbackQuery, session: AsyncSession) -> None:
    lines = ["🖥 <b>Панели и ноды</b>\n"]
    for panel in await registry.all_panels(session):
        # Имя панели — страна: у всех xui-панелей name одинаковый («xui»), и по
        # выводу нельзя было понять, какая локация сломалась.
        title = panel_label(panel)
        try:
            inbounds = await panel.list_inbounds()
            ok = await panel.health()
            status = "✅" if ok else "⚠️"
            lines.append(f"{status} <b>{title}</b>: инбаундов {len(inbounds)}")
            for ib in inbounds:
                lines.append(f"  • {ib.remark} ({ib.protocol}, {ib.port})")
        except Exception as exc: # noqa: BLE001
            lines.append(f"❌ <b>{title}</b>: ошибка — <code>{exc}</code>")
    text = "\n".join(lines)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=keyboards.back_to_menu_kb())
        await event.answer()
    else:
        await event.answer(text)


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    await state.set_state(Broadcast.text)
    await message.answer("Пришли текст рассылки (HTML разрешён). /cancel — отмена.")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено")


@router.message(Broadcast.text)
async def do_broadcast(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    await state.clear()
    recipients = (await session.scalars(select(User).where(User.is_blocked.is_(False)))).all()
    sent = failed = 0
    await message.answer(texts.ADMIN_BROADCAST_START.format(count=len(recipients)))
    for user in recipients:
        try:
            await bot.send_message(user.tg_id, message.html_text, disable_web_page_preview=True)
            sent += 1
        except Exception: # noqa: BLE001
            failed += 1
    await message.answer(texts.ADMIN_BROADCAST_DONE.format(sent=sent, failed=failed))


@router.message(Command("help_admin"))
async def cmd_help_admin(message: Message) -> None:
    await message.answer(
        "<b>Админ-команды</b>\n"
        "/admin — панель\n"
        "/stats — статистика\n"
        "/orders — заявки на оплату\n"
        "/user &lt;tg_id&gt; — карточка пользователя\n"
        "/grant &lt;tg_id&gt; &lt;дней&gt; — начислить/списать дни\n"
        "/block | /unblock &lt;tg_id&gt;\n"
        "/nodes — состояние панелей\n"
        "/watch — проверить клиентов и трафик панели\n"
        "/sync — раздать клиентов на все ноды (новая страна)\n"
        "/broadcast — рассылка"
    )


@router.message()
async def fallback(message: Message, session: AsyncSession, user: User) -> None:
    """Неизвестная команда — показываем меню, чтобы человек не терялся."""
    from app.bot.handlers.start import main_menu_view

    if message.text and message.text.startswith("/"):
        text, markup = await main_menu_view(session, user)
        await message.answer(text, reply_markup=markup)
        return
    await message.answer(
        "Не понял сообщение. Нажми /menu — там всё нужное, или напиши в поддержку.",
    )
