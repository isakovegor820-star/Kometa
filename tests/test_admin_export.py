"""Тесты агрегатов для страницы финансов и CSV-выгрузок админ-панели.

Данные создаём сервисами (``subscriptions`` + ``orders``), а не SQL вручную:
так тест ловит и регресс в самих сервисах. Панель — фикстура ``panel``
(``FakePanel``), сеть не используется.
"""

from __future__ import annotations

import codecs
import csv
import io
from datetime import date, datetime, timedelta, timezone

from app.db.models import Event, Order
from app.services import audit, exporting, orders, stats, subscriptions

#: Разделитель, который ждёт русский Excel.
SEP = ";"


# ---------------------------------------------------------------------------
# Помощники
# ---------------------------------------------------------------------------
def rows_of(data: bytes) -> list[list[str]]:
    """Разобрать выгрузку обратно: BOM снимаем, как это делает Excel."""
    text = data.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text, newline=""), delimiter=SEP))


def text_of(data: bytes) -> str:
    return data.decode("utf-8-sig")


async def paid_order(session, panel, *, tg_id: int, plan_index: int = 0, **user_kwargs):
    """Клиент + оплаченный заказ на тариф ``plan_index``."""
    user, _ = await subscriptions.get_or_create_user(session, tg_id=tg_id, **user_kwargs)
    plan = (await orders.list_plans(session))[plan_index]
    order = await orders.create_order(session, user, plan, provider="manual")
    await orders.mark_paid(session, order, panel)
    await session.flush()
    return user, order, plan


# ---------------------------------------------------------------------------
# to_csv: формат файла
# ---------------------------------------------------------------------------
def test_to_csv_has_bom_and_semicolons():
    data = exporting.to_csv(["Номер", "Сумма"], [[1, 199]])

    assert data.startswith(codecs.BOM_UTF8), "без BOM Excel покажет кракозябры"
    assert data[:3] == b"\xef\xbb\xbf"
    # В «сыром» utf-8 первый символ — это как раз BOM.
    assert data.decode("utf-8").startswith("\ufeff")

    text = text_of(data)
    assert text.splitlines()[0] == f"Номер{SEP}Сумма"
    assert text.splitlines()[1] == f"1{SEP}199"
    assert text.endswith("\r\n"), "RFC 4180 требует CRLF"


def test_to_csv_escapes_semicolons_quotes_and_newlines():
    tricky = 'оплата; "срочно"'
    multiline = "первая строка\nвторая строка"

    data = exporting.to_csv(["Комментарий", "Ещё"], [[tricky, multiline], [None, ""]])

    raw = text_of(data)
    # Значение с «;» и кавычками целиком завёрнуто в кавычки, внутренние удвоены.
    assert f'"{tricky.replace(chr(34), chr(34) * 2)}"' in raw

    parsed = rows_of(data)
    assert parsed[0] == ["Комментарий", "Ещё"]
    assert parsed[1] == [tricky, multiline], "значение не должно рваться на две ячейки"
    assert parsed[2] == ["", ""], "None и пустая строка — пустая ячейка"
    assert len(parsed) == 3, "перевод строки внутри значения не создаёт новую строку"


def test_to_csv_empty_rows_only_header():
    data = exporting.to_csv(["Дата", "Выручка, ₽"], [])

    assert rows_of(data) == [["Дата", "Выручка, ₽"]]
    assert text_of(data).endswith("\r\n")


# ---------------------------------------------------------------------------
# revenue_by_day
# ---------------------------------------------------------------------------
async def test_revenue_by_day_returns_exactly_days_points(session, panel):
    user, order, _ = await paid_order(session, panel, tg_id=9101, first_name="Иван")

    points = await stats.revenue_by_day(session, 7)

    assert len(points) == 7
    assert [point.day for point in points] == sorted(point.day for point in points), "от старых к новым"
    today = datetime.now(timezone.utc).date()
    assert points[-1].day == today, "последняя точка — сегодня"
    assert points[0].day == today - timedelta(days=6)
    assert points[-1].rub == order.amount_rub
    assert points[-1].orders == 1
    # Пустые дни присутствуют и обнулены, а не выпадают из списка.
    assert all(point.rub == 0 and point.orders == 0 for point in points[:-1])


async def test_revenue_by_day_ignores_unpaid_and_refunded(session, panel):
    user, paid, plan = await paid_order(session, panel, tg_id=9102)
    pending = await orders.create_order(session, user, plan, provider="manual")
    paid.status = "refunded"
    paid.refunded_at = datetime.now(timezone.utc)
    await session.flush()

    points = await stats.revenue_by_day(session, 3)

    assert len(points) == 3
    assert sum(point.rub for point in points) == 0
    assert sum(point.orders for point in points) == 0
    assert pending.status == "pending"


async def test_revenue_by_day_zero_days_is_empty(session):
    assert await stats.revenue_by_day(session, 0) == []


# ---------------------------------------------------------------------------
# revenue_by_plan
# ---------------------------------------------------------------------------
async def test_revenue_by_plan_groups_and_sorts_by_net(session, panel):
    _, cheap, cheap_plan = await paid_order(session, panel, tg_id=9201)
    _, pricey, pricey_plan = await paid_order(session, panel, tg_id=9202, plan_index=3)

    # Заказ без тарифа (например, ручная продажа до появления тарифов).
    orphan = Order(
        user_id=cheap.user_id,
        plan_id=None,
        amount_rub=50,
        base_amount_rub=50,
        provider="manual",
        status="paid",
        external_id="manual-no-plan",
        paid_at=datetime.now(timezone.utc),
    )
    session.add(orphan)
    await session.flush()

    result = await stats.revenue_by_plan(session, 30)

    titles = [item.title for item in result]
    assert titles == [pricey_plan.title, cheap_plan.title, stats.NO_PLAN_TITLE]
    assert [item.net_rub for item in result] == sorted((item.net_rub for item in result), reverse=True)

    by_title = {item.title: item for item in result}
    assert by_title[pricey_plan.title].orders == 1
    assert by_title[pricey_plan.title].gross_rub == pricey.amount_rub
    assert by_title[cheap_plan.title].gross_rub == cheap.amount_rub
    assert by_title[stats.NO_PLAN_TITLE].plan_id is None
    assert by_title[stats.NO_PLAN_TITLE].orders == 1
    assert by_title[stats.NO_PLAN_TITLE].gross_rub == 50
    assert by_title[stats.NO_PLAN_TITLE].net_rub > 0


async def test_revenue_by_plan_merges_same_plan(session, panel):
    await paid_order(session, panel, tg_id=9203)
    await paid_order(session, panel, tg_id=9204)

    result = await stats.revenue_by_plan(session, 30)

    assert len(result) == 1
    assert result[0].orders == 2
    assert result[0].gross_rub == 398  # 199 + 199


async def test_revenue_by_plan_skips_refunds(session, panel):
    _, order, _ = await paid_order(session, panel, tg_id=9205)
    order.status = "refunded"
    order.refunded_at = datetime.now(timezone.utc)
    await session.flush()

    assert await stats.revenue_by_plan(session, 30) == []


# ---------------------------------------------------------------------------
# refund_summary + новые поля collect()
# ---------------------------------------------------------------------------
async def test_refund_summary_counts_only_window(session, panel):
    _, fresh, _ = await paid_order(session, panel, tg_id=9301)
    _, old, _ = await paid_order(session, panel, tg_id=9302)

    fresh.status = "refunded"
    fresh.refunded_at = datetime.now(timezone.utc)
    old.status = "refunded"
    old.refunded_at = datetime.now(timezone.utc) - timedelta(days=40)
    await session.flush()

    summary = await stats.refund_summary(session, 30)

    assert summary == {"count": 1, "rub": fresh.amount_rub}


async def test_refund_summary_empty(session):
    assert await stats.refund_summary(session, 30) == {"count": 0, "rub": 0}


async def test_collect_fills_new_finance_fields(session, panel):
    _, order, _ = await paid_order(session, panel, tg_id=9303)

    summary = await stats.collect(session)

    assert summary.paid_orders_month == 1
    assert summary.revenue_month == order.amount_rub
    assert summary.avg_check_month == order.amount_rub  # revenue_month // paid_orders_month
    assert summary.refunds_month == 0
    assert summary.revenue_yesterday == 0  # оплата сегодня, не вчера
    # Старые поля никуда не делись.
    assert summary.paying_total == 1
    assert summary.revenue_today == order.amount_rub


async def test_collect_avg_check_is_zero_without_payments(session):
    summary = await stats.collect(session)

    assert summary.paid_orders_month == 0
    assert summary.avg_check_month == 0
    assert summary.refunds_month == 0
    # as_text не должен падать на пустых данных.
    assert "Средний чек за 30 дней" in summary.as_text()


async def test_collect_counts_refunds(session, panel):
    _, order, _ = await paid_order(session, panel, tg_id=9304)
    order.status = "refunded"
    order.refunded_at = datetime.now(timezone.utc)
    await session.flush()

    summary = await stats.collect(session)

    assert summary.refunds_month == 1


# ---------------------------------------------------------------------------
# orders_csv
# ---------------------------------------------------------------------------
async def test_orders_csv_has_client_and_russian_status(session, panel):
    user, order, plan = await paid_order(session, panel, tg_id=9401, first_name="Иван", username="ivan")

    parsed = rows_of(exporting.orders_csv([(order, user, plan)]))

    assert parsed[0] == exporting.ORDER_COLUMNS
    assert parsed[0] == [
        "Номер",
        "Дата",
        "Клиент",
        "Telegram ID",
        "Тариф",
        "Сумма, ₽",
        "К оплате, ₽",
        "Способ",
        "Статус",
        "Оплачен",
        "Возврат",
        "Комментарий",
    ]

    row = dict(zip(parsed[0], parsed[1], strict=True))
    assert row["Номер"] == str(order.id)
    assert row["Клиент"] == "Иван"
    assert row["Telegram ID"] == str(user.tg_id)
    assert row["Тариф"] == plan.title
    assert row["Сумма, ₽"] == str(order.amount_rub)
    assert row["К оплате, ₽"] == order.pay_amount_text
    assert row["Статус"] == "оплачен"
    assert row["Оплачен"] == order.paid_at.strftime("%d.%m.%Y %H:%M")
    assert row["Возврат"] == ""


async def test_orders_csv_pending_and_refund_labels(session, panel):
    user, paid, plan = await paid_order(session, panel, tg_id=9402, first_name="Пётр")
    pending = await orders.create_order(session, user, plan, provider="manual")
    await session.flush()

    parsed = rows_of(exporting.orders_csv([(paid, user, plan), (pending, user, plan)]))
    statuses = [row[parsed[0].index("Статус")] for row in parsed[1:]]
    assert statuses == ["оплачен", "ждёт оплаты"]

    paid.status = "refunded"
    paid.refunded_at = datetime.now(timezone.utc)
    paid.refund_note = "клиент передумал"
    await session.flush()

    parsed = rows_of(exporting.orders_csv([(paid, user, plan)], refunded_label="возврат средств"))
    row = dict(zip(parsed[0], parsed[1], strict=True))
    assert row["Статус"] == "возврат средств"
    assert row["Возврат"] == paid.refunded_at.strftime("%d.%m.%Y %H:%M")


async def test_orders_csv_survives_missing_user_and_plan(session, panel):
    _, order, _ = await paid_order(session, panel, tg_id=9403)

    parsed = rows_of(exporting.orders_csv([(order, None, None)]))
    row = dict(zip(parsed[0], parsed[1], strict=True))

    assert row["Клиент"] == f"id{order.user_id}"
    assert row["Тариф"] == "Без тарифа"
    assert row["Telegram ID"] == ""


async def test_orders_csv_escapes_comment_with_separator(session, panel):
    user, order, plan = await paid_order(session, panel, tg_id=9404)
    order.comment = 'оплата; "срочно"'
    await session.flush()

    parsed = rows_of(exporting.orders_csv([(order, user, plan)]))

    assert parsed[1][parsed[0].index("Комментарий")] == 'оплата; "срочно"'


# ---------------------------------------------------------------------------
# users_csv
# ---------------------------------------------------------------------------
async def test_users_csv_has_subscription_link(session, panel):
    user, order, _ = await paid_order(session, panel, tg_id=9501, first_name="Анна", username="anna")
    user.tags = "vip,шеринг"
    await session.flush()
    subscription = await subscriptions.get_subscription(session, user.id)
    assert subscription is not None

    parsed = rows_of(exporting.users_csv([(user, subscription)], link_builder=subscriptions.subscription_link))

    assert parsed[0] == exporting.USER_COLUMNS
    row = dict(zip(parsed[0], parsed[1], strict=True))
    assert row["ID"] == str(user.id)
    assert row["Имя"] == "Анна"
    assert row["Username"] == "@anna"
    assert row["Telegram ID"] == str(user.tg_id)
    assert row["Статус"] == "активна"
    assert row["Действует до"] == subscription.expires_at.strftime("%d.%m.%Y %H:%M")
    assert row["Осталось дней"] == str(subscription.days_left)
    assert row["Метки"] == "vip,шеринг"
    assert row["Заблокирован"] == "нет"
    # Ссылка собирается переданной функцией — та же, что видит клиент.
    assert row["Ссылка-подписка"] == subscriptions.subscription_link(subscription.subscription_token)
    assert subscription.subscription_token in row["Ссылка-подписка"]


async def test_users_csv_without_subscription(session):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=9502, username="nobody")
    user.is_blocked = True
    await session.flush()

    parsed = rows_of(exporting.users_csv([(user, None)], link_builder=subscriptions.subscription_link))
    row = dict(zip(parsed[0], parsed[1], strict=True))

    assert row["Статус"] == "нет подписки"
    assert row["Действует до"] == ""
    assert row["Осталось дней"] == "0"
    assert row["Заблокирован"] == "да"
    assert row["Ссылка-подписка"] == ""
    assert row["Username"] == "@nobody"


# ---------------------------------------------------------------------------
# audit_csv
# ---------------------------------------------------------------------------
async def test_audit_csv_translates_admin_action(session):
    user, _ = await subscriptions.get_or_create_user(session, tg_id=9601, username="client")
    admin_event = await audit.log_action(
        session,
        "admin.order_confirm",
        actor=audit.Actor(name="Аня", role="owner", ip="10.0.0.7", source=audit.SOURCE_WEB),
        user_id=user.id,
        payload={"order_id": 7, "amount": 199},
    )
    system_event = Event(kind="order_paid", user_id=user.id, payload='{"order_id": 7}')
    session.add(system_event)
    await session.flush()

    parsed = rows_of(exporting.audit_csv([admin_event, system_event]))

    assert parsed[0] == exporting.AUDIT_COLUMNS
    row = dict(zip(parsed[0], parsed[1], strict=True))
    assert row["Действие"] == "Подтвердил оплату"  # код admin.order_confirm переведён
    assert row["Кто"] == "Аня"
    assert row["Роль"] == "owner"
    assert row["Источник"] == "web"
    assert row["IP"] == "10.0.0.7"
    assert row["Клиент ID"] == str(user.id)
    assert row["Время"] == admin_event.created_at.strftime("%d.%m.%Y %H:%M")

    second = dict(zip(parsed[0], parsed[2], strict=True))
    assert second["Кто"] == "система"
    assert second["Действие"] == "Заказ оплачен"  # клиентское событие тоже по-русски


# ---------------------------------------------------------------------------
# revenue_csv и filename
# ---------------------------------------------------------------------------
async def test_revenue_csv_matches_day_points(session, panel):
    _, order, _ = await paid_order(session, panel, tg_id=9701)

    points = await stats.revenue_by_day(session, 3)
    parsed = rows_of(exporting.revenue_csv(points))

    assert parsed[0] == ["Дата", "Заказов", "Выручка, ₽"]
    assert len(parsed) == 4  # шапка + 3 дня
    today = datetime.now(timezone.utc).date()
    assert parsed[-1] == [today.strftime("%d.%m.%Y"), "1", str(order.amount_rub)]
    assert parsed[1] == [(today - timedelta(days=2)).strftime("%d.%m.%Y"), "0", "0"]


def test_revenue_csv_accepts_day_points():
    data = exporting.revenue_csv(
        [
            stats.DayPoint(day=date(2026, 10, 5), rub=199, orders=1),
            stats.DayPoint(day=date(2026, 10, 6), rub=0, orders=0),
        ]
    )

    assert rows_of(data) == [
        ["Дата", "Заказов", "Выручка, ₽"],
        ["05.10.2026", "1", "199"],
        ["06.10.2026", "0", "0"],
    ]


def test_filename_uses_utc_date():
    assert exporting.filename("orders", now=datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)) == (
        "kometa-orders-2026-10-06.csv"
    )
    # 7 октября 02:00 по Москве — это ещё 6 октября по UTC.
    moscow = timezone(timedelta(hours=3))
    assert exporting.filename("orders", now=datetime(2026, 10, 7, 2, 0, tzinfo=moscow)) == (
        "kometa-orders-2026-10-06.csv"
    )
    assert exporting.filename("revenue", now=datetime(2026, 1, 9, 23, 59, tzinfo=timezone.utc)) == (
        "kometa-revenue-2026-01-09.csv"
    )


def test_filename_without_now_uses_today():
    name = exporting.filename("users")

    assert name == f"kometa-users-{datetime.now(timezone.utc).date().isoformat()}.csv"
    assert name.endswith(".csv")
