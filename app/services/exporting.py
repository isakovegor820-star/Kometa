"""CSV-выгрузки для админ-панели.

Формат один на все отчёты, чтобы файлы одинаково открывались в русском Excel:

* разделитель — ``;`` (запятая в русской локали это десятичный знак);
* перевод строки — ``\\r\\n`` (требование RFC 4180);
* кодировка — UTF-8 **с BOM** (``utf-8-sig``): без BOM Excel показывает
  кириллицу кракозябрами.

Никаких внешних зависимостей: только stdlib. Значения экранирует
:mod:`csv`, поэтому комментарии с ``;``, кавычками и переводами строк
остаются одной ячейкой.

Модуль намеренно не тянет веб-слой на импорте: ``app.web.ui`` подключается
внутри :func:`audit_csv` — выгрузка нужна и боту, и фоновым задачам.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Iterable
from datetime import date, datetime, timezone
from typing import Any

from app.db.models import Event, Order, Plan, Subscription, User
from app.services.stats import PROVIDER_TITLES, DayPoint

#: Разделитель колонок: точка с запятой — как ждёт русский Excel.
DELIMITER = ";"
#: Перевод строки по RFC 4180.
LINE_TERMINATOR = "\r\n"
#: Пустая ячейка вместо «None» в выгрузке.
EMPTY = ""

DT_FORMAT = "%d.%m.%Y %H:%M"
DAY_FORMAT = "%d.%m.%Y"


def _cell(value: int | None) -> int | str:
    """Пустая ячейка вместо None: «None» в отчёте выглядел бы как значение."""
    return EMPTY if value is None else value


#: Человеческие статусы заказов. «Возврат» подставляется отдельно: панель
#: может называть его иначе («возврат средств»).
ORDER_STATUS_LABELS = {
    "pending": "ждёт оплаты",
    "paid": "оплачен",
    "canceled": "отменён",
    "expired": "просрочен",
}

#: Статусы подписки — те же слова, что в панели.
SUBSCRIPTION_STATUS_LABELS = {
    "trial": "пробный",
    "active": "активна",
    "expired": "истекла",
    "blocked": "заблокирована",
}


# ------------------------------------------------------------------ служебное
def dt(value: datetime | None, fmt: str = DT_FORMAT) -> str:
    """Дата в UTC: сервер живёт по одному времени, панель и выгрузка совпадают."""
    if not value:
        return EMPTY
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.strftime(fmt)


def day_text(value: date | None) -> str:
    """Дата без времени — для колонки «Дата» в дневном отчёте."""
    if not value:
        return EMPTY
    return value.strftime(DAY_FORMAT)


def filename(prefix: str, now: datetime | None = None) -> str:
    """Имя файла выгрузки: ``kometa-orders-2026-10-06.csv`` (дата UTC)."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return f"kometa-{prefix}-{moment.astimezone(timezone.utc).date().isoformat()}.csv"


def to_csv(header: list[str], rows: Iterable[Iterable[Any]]) -> bytes:
    """Собрать CSV: ``;``, CRLF, UTF-8 с BOM.

    ``None`` превращается в пустую ячейку — «None» в отчёте выглядел бы как
    значение. Экранирование (кавычки, разделители, переводы строк) делает
    :func:`csv.writer`.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(
        buffer,
        delimiter=DELIMITER,
        lineterminator=LINE_TERMINATOR,
        quoting=csv.QUOTE_MINIMAL,
    )
    writer.writerow(header)
    for row in rows:
        writer.writerow([EMPTY if value is None else value for value in row])
    return buffer.getvalue().encode("utf-8-sig")


def _order_status(order: Order, refunded_label: str) -> str:
    if order.status == "refunded":
        return refunded_label
    return ORDER_STATUS_LABELS.get(order.status, order.status)


# -------------------------------------------------------------------- заказы
ORDER_COLUMNS = [
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


def orders_csv(
    rows: list[tuple[Order, User | None, Plan | None]],
    *,
    refunded_label: str = "возврат",
) -> bytes:
    """Выгрузка заказов: одна строка — один заказ.

    «К оплате» — точная сумма перевода с уникальными копейками
    (:attr:`Order.pay_amount_text`), «Сумма» — сколько заказ стоит клиенту.
    """
    body = []
    for order, user, plan in rows:
        body.append(
            [
                order.id,
                dt(order.created_at),
                user.display_name if user is not None else f"id{order.user_id}",
                _cell(user.tg_id if user is not None else None),
                plan.title if plan is not None else "Без тарифа",
                _cell(order.amount_rub),
                order.pay_amount_text,
                PROVIDER_TITLES.get(order.provider, order.provider),
                _order_status(order, refunded_label),
                dt(order.paid_at),
                dt(order.refunded_at),
                order.comment or EMPTY,
            ]
        )
    return to_csv(ORDER_COLUMNS, body)


# -------------------------------------------------------------- пользователи
USER_COLUMNS = [
    "ID",
    "Имя",
    "Username",
    "Telegram ID",
    "Статус",
    "Действует до",
    "Осталось дней",
    "Метки",
    "Заблокирован",
    "Регистрация",
    "Ссылка-подписка",
]


def users_csv(
    rows: list[tuple[User, Subscription | None]],
    *,
    link_builder: Callable[[str], str],
) -> bytes:
    """Выгрузка клиентов.

    ``link_builder(token)`` — функция сборки ссылки-подписки; в панели это
    :func:`app.services.subscriptions.subscription_link`, но здесь она
    передаётся снаружи: выгрузка не должна знать про настройки панели.
    """
    body = []
    for user, subscription in rows:
        body.append(
            [
                user.id,
                user.first_name or EMPTY,
                f"@{user.username}" if user.username else EMPTY,
                _cell(user.tg_id),
                SUBSCRIPTION_STATUS_LABELS.get(
                    subscription.status if subscription is not None else "", "нет подписки"
                ),
                dt(subscription.expires_at) if subscription is not None else EMPTY,
                subscription.days_left if subscription is not None else 0,
                user.tags or EMPTY,
                "да" if user.is_blocked else "нет",
                dt(user.created_at),
                link_builder(subscription.subscription_token) if subscription is not None else EMPTY,
            ]
        )
    return to_csv(USER_COLUMNS, body)


# --------------------------------------------------------------------- аудит
AUDIT_COLUMNS = ["Время", "Кто", "Роль", "Источник", "IP", "Действие", "Клиент ID", "Подробности"]


def audit_csv(events: list[Event]) -> bytes:
    """Журнал действий команды.

    Название действия берём из ``app.web.ui.event_label`` — единый словарь с
    панелью, поэтому в выгрузке те же слова, что на экране. Импорт внутри
    функции: веб-слой на импорте модуля не нужен.
    """
    from app.web.ui import event_label

    body = []
    for event in events:
        body.append(
            [
                dt(event.created_at),
                event.actor_name or "система",
                event.actor_role or EMPTY,
                event.source or EMPTY,
                event.ip or EMPTY,
                event_label(event.kind),
                _cell(event.user_id),
                event.payload or EMPTY,
            ]
        )
    return to_csv(AUDIT_COLUMNS, body)


# ------------------------------------------------------------------- выручка
REVENUE_COLUMNS = ["Дата", "Заказов", "Выручка, ₽"]


def revenue_csv(days: list[DayPoint]) -> bytes:
    """Выручка по дням — та же последовательность, что на графике."""
    body = [[day_text(point.day), point.orders, point.rub] for point in days]
    return to_csv(REVENUE_COLUMNS, body)
