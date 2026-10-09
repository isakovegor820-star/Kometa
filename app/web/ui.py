"""Общие помощники веб-панели: роли, форматирование, Jinja-глобалы.

Зачем отдельный модуль: панель состоит из десятков шаблонов, и подписи статусов,
формат денег и права ролей должны быть в одном месте. Иначе «оплачен», «Оплачен»
и «paid» расползаются по страницам, а модератор видит разные слова про одно и то же.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlencode

# --------------------------------------------------------------------- роли
ROLE_OWNER = "owner"
ROLE_MODERATOR = "moderator"
ROLE_SUPPORT = "support"

ROLES: dict[str, str] = {
    ROLE_OWNER: "Владелец",
    ROLE_MODERATOR: "Модератор",
    ROLE_SUPPORT: "Поддержка",
}

#: Что кому можно. Владелец — всё; модератор ведёт операции и деньги клиента;
#: поддержка смотрит и оставляет заметки, но ничего не меняет.
_CAPS: dict[str, frozenset[str]] = {
    ROLE_OWNER: frozenset(
        {
            "orders.view", "orders.act", "orders.refund",
            "users.view", "users.act", "users.notes",
            "finance.view", "finance.export",
            "nodes.view", "nodes.act", "nodes.secrets",
            "downtime.manage",
            "alerts.view", "alerts.act",
            "growth.view", "growth.act",
            "broadcast.send",
            "audit.view",
            "plans.manage",
            "team.manage",
            "system.act",
        }
    ),
    ROLE_MODERATOR: frozenset(
        {
            "orders.view", "orders.act", "orders.refund",
            "users.view", "users.act", "users.notes",
            "finance.view", "finance.export",
            "nodes.view", "alerts.view", "alerts.act",
            "growth.view", "growth.act",
            "audit.view",
            "system.act",
        }
    ),
    ROLE_SUPPORT: frozenset(
        {
            "orders.view",
            "users.view", "users.notes",
            "nodes.view", "alerts.view",
            "growth.view",
        }
    ),
}

ROLE_DESCRIPTIONS: dict[str, str] = {
    ROLE_OWNER: "Полный доступ: деньги, ноды, роли, рассылки.",
    ROLE_MODERATOR: "Подтверждает оплаты, продлевает доступ, делает возвраты, разбирает алерты.",
    ROLE_SUPPORT: "Только смотрит и оставляет заметки. Ничего не меняет и не видит секретов.",
}


def can(role: str | None, capability: str) -> bool:
    """Есть ли у роли право. Неизвестная роль не получает ничего."""
    return capability in _CAPS.get(role or "", frozenset())


#: Разделы для командной палитры (⌘K): право, адрес, подпись, ключевые слова.
#: Список держим рядом с правами, чтобы палитра не показывала раздел, который
#: роль всё равно не откроет.
CMD_SECTIONS: tuple[tuple[str, str, str, str], ...] = (
    ("orders.view", "/admin", "Дашборд", "обзор главная сводка"),
    ("orders.view", "/admin/orders", "Очередь заказов", "заказы оплаты подтвердить"),
    ("users.view", "/admin/users", "Пользователи", "клиенты люди база"),
    ("orders.refund", "/admin/refunds", "Возвраты", "возврат деньги refund"),
    ("downtime.manage", "/admin/downtime", "Компенсация простоя", "простой downtime авария"),
    ("finance.view", "/admin/finance", "Финансы и отчёты", "деньги выручка прибыль"),
    ("plans.manage", "/admin/plans", "Тарифы", "цены планы тариф"),
    ("nodes.view", "/admin/nodes", "Ноды", "серверы панели локации"),
    ("alerts.view", "/admin/alerts", "Алерты", "ошибки проблемы"),
    ("growth.view", "/admin/referrals", "Рефералы и промокоды", "промо приглашения"),
    ("growth.view", "/admin/partners", "Партнёры и рефералы", "партнёры ссылки выплаты блогеры рефералы"),
    ("growth.view", "/admin/links", "Персональные ссылки", "персональные ссылки именные скидка под человека"),
    ("broadcast.send", "/admin/broadcast", "Рассылки", "сообщения рассылка"),
    ("audit.view", "/admin/audit", "Журнал действий", "аудит история кто что"),
    ("team.manage", "/admin/team", "Команда и роли", "доступы сотрудники"),
)


def cmd_sections(caps: Iterable[str]) -> list[dict[str, str]]:
    """Разделы палитры, доступные этой роли."""
    allowed = set(caps)
    return [{"title": title, "url": url, "keys": keys} for cap, url, title, keys in CMD_SECTIONS if cap in allowed]


def capabilities(role: str) -> frozenset[str]:
    return _CAPS.get(role, frozenset())


def role_label(role: str | None) -> str:
    return ROLES.get(role or "", role or "—")


# ------------------------------------------------------------------ подписи
SUBSCRIPTION_STATUS: dict[str, tuple[str, str]] = {
    "trial": ("Пробный", "info"),
    "active": ("Активна", "ok"),
    "expired": ("Истекла", "warn"),
    "blocked": ("Заблокирована", "err"),
}

ORDER_STATUS: dict[str, tuple[str, str]] = {
    "pending": ("Ждёт оплаты", "warn"),
    "paid": ("Оплачен", "ok"),
    "canceled": ("Отменён", "neutral"),
    "expired": ("Просрочен", "neutral"),
    "refunded": ("Возврат", "err"),
}

PROVIDER_LABELS: dict[str, str] = {
    "manual": "Перевод (СБП/карта)",
    "sbp": "СБП",
    "nspk": "СБП",
    "platega_sbp": "СБП (Platega)",
    "platega_card": "Карта МИР (Platega)",
    "platega_intl": "Зарубежная карта (Platega)",
    "crypto": "Крипта (Crypto Pay)",
    "stars": "Telegram Stars",
}

ORDER_KIND: dict[str, str] = {"purchase": "Покупка", "renew": "Продление", "trial": "Пробный"}

#: Человеческие названия событий журнала. Ключ — kind из app/services/events.py.
EVENT_LABELS: dict[str, str] = {
    "start": "Запуск бота",
    "trial_started": "Выдан пробный доступ",
    "order_created": "Создан заказ",
    "order_paid": "Заказ оплачен",
    "order_canceled": "Заказ отменён",
    "order_refunded": "Возврат денег",
    "subscription_expired": "Подписка истекла",
    "subscription_extended": "Подписка продлена",
    "subscription_revoked": "Доступ отозван",
    "subscription_restored": "Доступ восстановлен",
    "referral_rewarded": "Начислена реферальная награда",
    "referral_joined": "Пришёл по приглашению",
    "referral_bonus_accrued": "Бонусные дни в баланс",
    "bonus_days_applied": "Бонусные дни применены",
    "promo_applied": "Промокод применён",
    "promo_redeemed": "Промокод использован",
    "panel_error": "Ошибка панели",
    "error": "Ошибка",
}

#: Действия администраторов — пишутся сервисом audit с префиксом admin.
ADMIN_ACTION_LABELS: dict[str, str] = {
    "admin.login": "Вход в панель",
    "admin.login_failed": "Неудачный вход",
    "admin.logout": "Выход из панели",
    "admin.order_confirm": "Подтвердил оплату",
    "admin.order_reject": "Отклонил заказ",
    "admin.order_bulk": "Массовая обработка заказов",
    "admin.order_refund": "Оформил возврат",
    "admin.grant_days": "Начислил дни",
    "admin.write_off_days": "Списал дни",
    "admin.block_user": "Заблокировал клиента",
    "admin.unblock_user": "Разблокировал клиента",
    "admin.revoke_access": "Отозвал доступ",
    "admin.note_added": "Добавил заметку",
    "admin.tags_changed": "Изменил метки",
    "admin.node_saved": "Сохранил ноду",
    "admin.node_toggle": "Переключил ноду",
    "admin.node_deleted": "Удалил ноду",
    "admin.node_check": "Проверил ноды",
    "admin.promo_created": "Создал промокод",
    "admin.promo_toggled": "Переключил промокод",
    "admin.plan_saved": "Изменил тариф",
    "admin.autopay_run": "Запустил проверку выписки",
    "admin.broadcast_started": "Запустил рассылку",
    "admin.broadcast_canceled": "Остановил рассылку",
    "admin.alert_resolved": "Закрыл алерт",
    "admin.alert_ack": "Взял алерт в работу",
    "admin.downtime_start": "Открыл период простоя",
    "admin.downtime_end": "Закрыл простой и начислил дни",
    "admin.downtime_grant": "Начислил дни за простой",
    "admin.team_created": "Создал доступ",
    "admin.team_updated": "Изменил доступ",
    "admin.export": "Выгрузил данные",
    "admin.watchdog_run": "Прогнал аудит клиентов",
    "admin.sync_nodes": "Синхронизировал ноды",
}

SEVERITY_LABELS: dict[str, str] = {"err": "Критично", "warn": "Внимание", "info": "Инфо", "ok": "Норма"}


def event_label(kind: str) -> str:
    """Человеческое название события: и клиентского, и админского."""
    if kind in ADMIN_ACTION_LABELS:
        return ADMIN_ACTION_LABELS[kind]
    return EVENT_LABELS.get(kind, kind.replace("_", " "))


def status_pair(status: str | None, table: dict[str, tuple[str, str]] = SUBSCRIPTION_STATUS) -> tuple[str, str]:
    return table.get(status or "", (status or "—", "neutral"))


# ------------------------------------------------------------- форматирование
def _group_digits(value: Any) -> str:
    text = f"{int(value):,}".replace(",", "\u2009")  # тонкий пробел — «1 590 ₽»
    return text


def money(value: Any, currency: str = "₽") -> str:
    """Сумма с разделителем разрядов: 1590 → «1 590 ₽»."""
    if value is None or value == "":
        return "—"
    try:
        return f"{_group_digits(value)} {currency}"
    except (TypeError, ValueError):
        return str(value)


def num(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        return _group_digits(value)
    except (TypeError, ValueError):
        return str(value)


def percent(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value):.{digits}f} %"
    except (TypeError, ValueError):
        return "—"


def dt(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Дата в UTC. Панель живёт на сервере, поэтому время одно для всех."""
    if not value:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.strftime(fmt)


def dt_short(value: datetime | None) -> str:
    return dt(value, "%d.%m %H:%M")


def dt_day(value: datetime | None) -> str:
    return dt(value, "%d.%m.%Y")


def ago(value: datetime | None) -> str:
    """«5 мин назад» — быстрее читается в очередях, чем точное время."""
    if not value:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - value
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return dt_short(value)
    if seconds < 60:
        return "только что"
    if seconds < 3600:
        return f"{seconds // 60} мин назад"
    if seconds < 86400:
        return f"{seconds // 3600} ч назад"
    if seconds < 86400 * 7:
        return f"{seconds // 86400} дн назад"
    return dt_day(value)


def initials(name: str | None) -> str:
    text = (name or "").strip()
    if not text:
        return "?"
    parts = [part for part in text.replace("@", " ").split() if part]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][:1] + parts[1][:1]).upper()


def plural(count: int, one: str, few: str, many: str) -> str:
    """Русское склонение: 1 день / 2 дня / 5 дней."""
    n = abs(int(count)) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def days_word(count: int) -> str:
    return f"{count} {plural(count, 'день', 'дня', 'дней')}"


def mask_secret(value: str | None, keep: int = 4) -> str:
    """Токен в списке показываем обрезанным: утёкший экран не должен стоить доступ."""
    if not value:
        return "—"
    text = str(value)
    if len(text) <= keep:
        return "•" * len(text)
    return "•" * 8 + text[-keep:]


def link_label(url: str | None, *, head: int = 12, tail: int = 4) -> str:
    """Короткая подпись для длинной ссылки: ``sub/26e8f0a1c…f0a6``.

    Зачем: в таблице ссылка-подписка занимала ~460 px и выталкивала столбец
    действий за край экрана. Полный адрес остаётся в подсказке и в буфере
    обмена (кнопка копирования берёт его из ``data-copy``), поэтому подпись
    можно сокращать без потери функции.
    """
    text = (url or "").strip()
    if not text:
        return "—"
    without_scheme = text.split("://", 1)[-1]
    path = without_scheme.split("/", 1)[1] if "/" in without_scheme else ""
    parts = [part for part in path.split("/") if part]
    if not parts:
        return text[: head + tail + 1] + ("…" if len(text) > head + tail + 1 else "")
    label = "/".join(parts[-2:]) if len(parts) > 1 else parts[-1]
    if len(label) <= head + tail + 1:
        return label
    return f"{label[:head]}…{label[-tail:]}"


# ---------------------------------------------------------------- ссылки
def query_string(base: dict[str, Any], **changes: Any) -> str:
    """Собрать query-строку, сохранив текущие фильтры (для пагинации и сортировок).

    Важно: значение ``all`` — осмысленный фильтр («все статусы»), его нельзя
    выбрасывать. Иначе вкладка «Все» превращается в «ждут оплаты», а пагинация
    на этой вкладке уводит в другой список.
    """
    merged = {**base, **changes}
    clean = {k: v for k, v in merged.items() if v is not None and v != ""}
    return urlencode(clean)


def page_range(current: int, total_pages: int, window: int = 2) -> list[int | None]:
    """Номера страниц для пагинации. None — многоточие."""
    if total_pages <= 1:
        return [1] if total_pages == 1 else []
    pages: list[int | None] = []
    last = 0
    for page in range(1, total_pages + 1):
        if page <= 1 or page > total_pages - 1 or abs(page - current) <= window:
            if last and page - last > 1:
                pages.append(None)
            pages.append(page)
            last = page
    return pages


def split_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    seen: list[str] = []
    for chunk in str(raw).replace(";", ",").split(","):
        tag = chunk.strip()
        if tag and tag not in seen:
            seen.append(tag)
    return seen


def join_tags(tags: Iterable[str]) -> str:
    return ",".join(t.strip() for t in tags if t and t.strip())
