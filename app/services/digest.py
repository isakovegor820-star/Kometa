"""Сводки для команды: деньги, люди, подписки, ноды.

Один источник правды для «что происходит в сервисе»: отсюда берут текст
ежедневный дайджест (21:00 МСК), команда ``/digest`` в боте уведомлений и
короткая приписка к уведомлению об оплате («сегодня: 3 оплаты на 1 240 ₽»).

Цифры считаются по базе, а не по памяти процесса: после перезапуска сводка
такая же, как до него. Деньги за «сегодня» — по московским суткам: владелец
сервиса и клиенты живут в МСК, и «выручка сегодня» в 00:30 МСК не должна
показывать вчерашний день.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Alert, Node, Order, Plan, Subscription, User
from app.services import orders as orders_service
from app.services import stats as stats_service

#: Московское время: сутки, недели и «сегодня» считаем по нему.
MSK = timezone(timedelta(hours=3))
#: Сколько дней вперёд считаем «скоро закончится».
EXPIRING_DAYS = 3


def msk_day_start(days_ago: int = 0, *, now: datetime | None = None) -> datetime:
    """Начало московских суток N дней назад, в UTC (как хранится в базе)."""
    moment = (now or datetime.now(timezone.utc)).astimezone(MSK)
    start = (moment - timedelta(days=days_ago)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc)


def msk_stamp(moment: datetime | None = None) -> str:
    """Дата и время по МСК для подписи сводки."""
    return (moment or datetime.now(timezone.utc)).astimezone(MSK).strftime("%d.%m %H:%M")


@dataclass
class MoneyToday:
    """Оплаты за текущие московские сутки."""

    orders: int = 0
    rub: int = 0

    def line(self) -> str:
        if not self.orders:
            return "Сегодня оплат ещё не было"
        return f"Сегодня: {self.orders} {plural(self.orders, 'оплата', 'оплаты', 'оплат')} на {self.rub} ₽"


def plural(count: int, one: str, few: str, many: str) -> str:
    """Русская форма слова по числу: 1 оплата, 2 оплаты, 5 оплат."""
    tail = abs(count) % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


async def money_today(session: AsyncSession, *, now: datetime | None = None) -> MoneyToday:
    """Сколько оплат пришло за сегодня (МСК) — для приписки к оплате."""
    start = msk_day_start(now=now)
    row = (
        await session.execute(
            select(func.count(Order.id), func.coalesce(func.sum(Order.amount_rub), 0)).where(
                Order.status == "paid",
                Order.paid_at.is_not(None),
                Order.paid_at >= start,
            )
        )
    ).one()
    return MoneyToday(orders=int(row[0] or 0), rub=int(row[1] or 0))


async def payment_totals_note(session: AsyncSession, *, now: datetime | None = None) -> str:
    """Приписка к уведомлению об оплате: «сколько уже собрано сегодня».

    Одна оплата в вакууме ничего не говорит: 120 ₽ — это норма или провал?
    Итог дня рядом с суммой отвечает на этот вопрос без открытия админки.
    """
    from app.config import get_settings

    if not get_settings().notify_payment_totals:
        return ""
    today = await money_today(session, now=now)
    return f"\n\n📈 {today.line()}"


@dataclass
class SubscriptionsSoon:
    """Подписки, которые заканчиваются в ближайшие дни."""

    paid: int = 0
    trials: int = 0
    potential_rub: int = 0


@dataclass
class NodeState:
    """Состояние нод: сколько активных и сколько реально проблемных."""

    active: int = 0
    total: int = 0
    panel_down: list[str] = field(default_factory=list)
    probe_down: list[str] = field(default_factory=list)

    @property
    def ok(self) -> int:
        return max(0, self.active - len(set(self.panel_down) | set(self.probe_down)))


@dataclass
class AlertState:
    """Открытые алерты по важности."""

    err: int = 0
    warn: int = 0
    info: int = 0

    @property
    def total(self) -> int:
        return self.err + self.warn + self.info


async def subscriptions_soon(
    session: AsyncSession, days: int = EXPIRING_DAYS, *, now: datetime | None = None
) -> SubscriptionsSoon:
    """Кто заканчивается в ближайшие N дней — и на сколько это денег."""
    moment = now or datetime.now(timezone.utc)
    horizon = moment + timedelta(days=days)
    result = SubscriptionsSoon()

    for status, attr in (("active", "paid"), ("trial", "trials")):
        count = await session.scalar(
            select(func.count(Subscription.id)).where(
                Subscription.status == status,
                Subscription.expires_at > moment,
                Subscription.expires_at <= horizon,
            )
        )
        setattr(result, attr, int(count or 0))

    # Потенциал продления: сколько принесли бы эти люди, продли всё по своему
    # тарифу. Цена тарифа — из плана; плана нет (старая подписка) — не считаем.
    potential = await session.scalar(
        select(func.coalesce(func.sum(Plan.price_rub), 0))
        .select_from(Subscription)
        .join(Plan, Plan.id == Subscription.plan_id)
        .where(
            Subscription.status == "active",
            Subscription.expires_at > moment,
            Subscription.expires_at <= horizon,
        )
    )
    result.potential_rub = int(potential or 0)
    return result


async def node_state(session: AsyncSession) -> NodeState:
    """Ноды: активные, «панель не отвечает», «порт не пускает клиента»."""
    state = NodeState()
    state.total = int(await session.scalar(select(func.count(Node.id))) or 0)
    nodes = list((await session.scalars(select(Node).where(Node.is_active.is_(True)))).all())
    state.active = len(nodes)
    for node in nodes:
        label = f"{node.title}"
        if node.last_check_at is not None and not node.last_check_ok:
            state.panel_down.append(label)
        if node.last_probe_at is not None and not node.last_probe_ok:
            state.probe_down.append(label)
    return state


async def alert_state(session: AsyncSession) -> AlertState:
    """Открытые алерты: сколько ошибок, предупреждений и заметок."""
    state = AlertState()
    rows = (
        await session.execute(
            select(Alert.severity, func.count(Alert.id))
            .where(Alert.status == "open")
            .group_by(Alert.severity)
        )
    ).all()
    for severity, count in rows:
        if severity == "err":
            state.err = int(count)
        elif severity == "warn":
            state.warn = int(count)
        else:
            state.info = int(count)
    return state


async def build_money(session: AsyncSession) -> str:
    """Деньги: сегодня, 7 и 30 дней, средний чек, счета и заявки.

    Счета и заявки разделены намеренно. «Ожидают подтверждения» раньше считало
    все неоплаченные счета подряд, и владелец шёл подтверждать то, что клиент
    ещё не оплатил: доступ по Stars и Platega выдаётся автоматически.
    """
    snapshot = await stats_service.collect(session)
    today = await money_today(session)
    lines = [
        "💰 <b>Деньги</b>",
        f"• {today.line()}",
        f"• За 7 дней: <b>{snapshot.revenue_week} ₽</b> · за 30: <b>{snapshot.revenue_month} ₽</b>",
        f"• Средний чек (30 дн.): <b>{snapshot.avg_check_month} ₽</b> · возвратов: {snapshot.refunds_month}",
        f"• Вчера: <b>{snapshot.revenue_yesterday} ₽</b>",
    ]
    manual = await orders_service.manual_requests(session)
    if manual:
        lines.append(f"• ⏳ Ждут подтверждения (перевод по реквизитам): <b>{manual}</b>")
    invoices = await orders_service.pending_invoices(session)
    if invoices:
        lines.append(
            f"• 🧾 Счета ждут оплаты клиентом: {invoices} — <i>делать ничего не нужно,"
            " доступ включится сам</i>"
        )
    return "\n".join(lines)


async def build_people(session: AsyncSession) -> str:
    """Люди и подписки: база, конверсия, что заканчивается."""
    snapshot = await stats_service.collect(session)
    soon = await subscriptions_soon(session)
    new_today = int(
        await session.scalar(
            select(func.count(User.id)).where(User.created_at >= msk_day_start())
        )
        or 0
    )
    lines = [
        "👥 <b>Люди</b>",
        f"• Новых сегодня: <b>{new_today}</b> · всего: <b>{snapshot.users_total}</b>",
        f"• Активных платных: <b>{snapshot.active_paid}</b> · на пробном: {snapshot.trials_active}",
        f"• Платили хотя бы раз: {snapshot.paying_total} (конверсия {snapshot.conversion:.1f}%)",
        f"• ⌛️ Заканчиваются в {EXPIRING_DAYS} дня: <b>{soon.paid}</b> платных, {soon.trials} пробных",
    ]
    if soon.potential_rub:
        lines.append(f"• На кону продлений: <b>~{soon.potential_rub} ₽</b>")
    return "\n".join(lines)


async def build_infra(session: AsyncSession) -> str:
    """Ноды и алерты: что сломано прямо сейчас."""
    nodes = await node_state(session)
    alerts = await alert_state(session)
    lines = [
        "🖥 <b>Инфраструктура</b>",
        f"• Ноды: живых <b>{nodes.ok}</b> из {nodes.active} активных (всего {nodes.total})",
    ]
    if nodes.panel_down:
        lines.append("• 🔴 Панель не отвечает: " + ", ".join(nodes.panel_down[:5]))
    if nodes.probe_down:
        lines.append("• ⚠️ Порт не пускает клиента: " + ", ".join(nodes.probe_down[:5]))
    if not nodes.panel_down and not nodes.probe_down and nodes.active:
        lines.append("• ✅ Все ноды отвечают")
    if alerts.total:
        lines.append(
            f"• Алерты: 🔴 {alerts.err} · 🟡 {alerts.warn} · ⚪️ {alerts.info}"
        )
    else:
        lines.append("• ✅ Открытых алертов нет")
    return "\n".join(lines)


async def build_daily(session: AsyncSession, *, title: str = "Сводка за сутки") -> str:
    """Полная сводка: деньги, люди, инфраструктура и следующий шаг.

    Порядок именно такой: сначала то, ради чего сервис существует (деньги и
    люди), потом инфраструктура. Если сообщение придёт ночью и его прочитают
    по диагонали, важное должно быть сверху.
    """
    blocks = [
        f"📊 <b>Kometa — {title}</b> · {msk_stamp()} МСК",
        await build_money(session),
        await build_people(session),
        await build_infra(session),
    ]
    hint = await build_hint(session)
    if hint:
        blocks.append(hint)
    return "\n\n".join(blocks)


async def build_hint(session: AsyncSession) -> str:
    """Что требует руки: заявки на ручную оплату, алерты, проблемные ноды.

    Неоплаченные счета сюда не попадают: пока клиент не заплатил, делать
    нечего, а «подтвердить заявок: 3» заставляло владельца идти подтверждать
    то, что оплатится само.
    """
    problems: list[str] = []
    manual = await orders_service.manual_requests(session)
    if manual:
        problems.append(f"подтвердить переводов: {manual}")
    alerts = await alert_state(session)
    if alerts.err:
        problems.append(f"разобрать алертов: {alerts.err}")
    nodes = await node_state(session)
    if nodes.panel_down or nodes.probe_down:
        problems.append("проверить ноды: " + ", ".join(sorted(set(nodes.panel_down + nodes.probe_down))[:3]))
    if not problems:
        return "✅ <i>Всё под контролем — действий не требуется.</i>"
    return "🔧 <b>Требует внимания:</b> " + "; ".join(problems) + "."


async def build_startup(
    session: AsyncSession,
    *,
    sales_bot: str = "",
    notify_username: str = "",
) -> str:
    """Стартовое сообщение: что поднялось и в каком состоянии сервис.

    «Бот запущен» без цифр бесполезно: после перезапуска хочется сразу видеть,
    что база читается, ноды живы и сколько переводов ждёт решения.
    """
    snapshot = await stats_service.collect(session)
    nodes = await node_state(session)
    lines = [
        f"🚀 <b>Kometa запущена</b> · {msk_stamp()} МСК",
        f"Продажи: @{sales_bot}" if sales_bot else "Продажи: —",
        f"Уведомления: {notify_username}" if notify_username else "Уведомления: основным ботом",
        f"👥 Пользователей: {snapshot.users_total} · платных активных: {snapshot.active_paid} · "
        f"на пробном: {snapshot.trials_active}",
        f"🖥 Ноды: живых {nodes.ok} из {nodes.active}",
    ]
    manual = await orders_service.manual_requests(session)
    if manual:
        lines.append(f"🧾 Ждут подтверждения (перевод): <b>{manual}</b>")
    invoices = await orders_service.pending_invoices(session)
    if invoices:
        lines.append(f"💳 Счета ждут оплаты клиентом: {invoices}")
    if nodes.panel_down or nodes.probe_down:
        broken = sorted(set(nodes.panel_down + nodes.probe_down))
        lines.append("⚠️ Проблемные ноды: " + ", ".join(broken[:5]))
    return "\n".join(lines)
