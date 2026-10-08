"""Обязательная подписка на основной канал.

Правило: пока у человека НЕТ активной подписки, бот просит подписаться на
канал и до подтверждения не пускает дальше. У кого доступ активен (пробный
или оплаченный) — пользуется ботом как раньше: заплативший клиент не должен
терять доступ к своей ссылке из-за выхода из канала.

Как это работает:
  * подписку спрашиваем у Telegram через ``getChatMember`` по CHANNEL_ID —
    поэтому бот обязан быть АДМИНОМ канала, иначе участников не показывают;
  * успешную проверку запоминаем в ``users.channel_verified_at`` и
    CHANNEL_GATE_CACHE_HOURS часов не дёргаем API снова: иначе каждый апдейт
    превращается в запрос к Telegram, а лимиты общие для всего бота;
  * если проверить не удалось (бот не админ, сеть, лимиты API), по умолчанию
    ПУСКАЕМ и предупреждаем админов: сломанный гейт не должен закрывать бота
    всем сразу. Поведение переключается CHANNEL_GATE_FAIL_OPEN=false.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import User, utcnow
from app.services import subscriptions

logger = logging.getLogger(__name__)
settings = get_settings()

#: Всё, что начинается с этого префикса (кнопка «Я подписался»), гейт не трогает.
CALLBACK_PREFIX = "channel:"

#: Кнопка «Я подписался»: единственный колбэк, который проходит сквозь гейт.
CALLBACK_CHECK = CALLBACK_PREFIX + "check"

#: Статусы участника, которые считаем подпиской на канал.
SUBSCRIBED_STATUSES = frozenset({"creator", "administrator", "member"})

#: Ответы Telegram «такого пользователя в чате нет» — это не сбой проверки,
#: а честный ответ «не подписан»: подписанный участник бы нашёлся.
_ABSENT_MARKERS = ("user not found", "participant not found", "member not found")

#: Как часто напоминать админам, что проверка подписки не работает (секунды).
ALERT_COOLDOWN_SECONDS = 3600
#: Когда в последний раз предупреждали админов. None — ещё ни разу, и это важно
#: не путать с «0.0»: часы берём у ``time.monotonic()``, а он считает секунды
#: от запуска машины, поэтому на только что поднятом контейнере «now < 3600»
#: и предупреждение о сломанном гейте молча терялось бы в первый час работы.
_alerted_at: float | None = None

#: Сколько секунд помнить ответ «не подписан» в памяти процесса.
#: Человек, который ходит по экрану подписки и жмёт кнопки, иначе стоил бы
#: запроса к Telegram на каждое нажатие; антифлуд режет только до 5 действий
#: в 3 секунды, а лимиты API общие для всего бота. Кнопка «Я подписался» этот
#: кэш игнорирует — она проверяет заново (см. app/bot/handlers/channel.py).
NEGATIVE_CACHE_SECONDS = 15
_not_subscribed_at: dict[int, float] = {}

#: Пауза после сбоя проверки. Сбой почти всегда общий (бот не админ канала),
#: а не про конкретного человека: нет смысла долбить API на каждом апдейте.
FAILURE_BACKOFF_SECONDS = 30
_unavailable_until: float = 0.0


@dataclass(frozen=True)
class Verdict:
    """Итог проверки: пускать ли человека дальше и почему.

    :param allowed: можно ли пользоваться ботом.
    :param subscribed: True — подписка есть, False — нет, None — не проверили.
    :param reason: короткая причина для логов и тестов.
    """

    allowed: bool
    subscribed: bool | None = None
    reason: str = ""


def configured() -> bool:
    """Гейт включён и канал известен (иначе проверять нечего)."""
    return bool(settings.channel_gate_enabled and settings.resolved_channel_id)


def applies_to(tg_id: int) -> bool:
    """Применим ли гейт к этому человеку. Админов не трогаем никогда."""
    return configured() and tg_id not in settings.admin_id_list


def remember(user: User) -> None:
    """Запомнить успешную проверку — до истечения срока API больше не дёргаем."""
    user.channel_verified_at = utcnow()


def _cache_fresh(user: User) -> bool:
    if user.channel_verified_at is None:
        return False
    hours = int(settings.channel_gate_cache_hours)
    if hours <= 0:
        return False
    return utcnow() - user.channel_verified_at < timedelta(hours=hours)


def _recently_not_subscribed(tg_id: int) -> bool:
    """Ответ «не подписан» держим NEGATIVE_CACHE_SECONDS секунд."""
    seen = _not_subscribed_at.get(tg_id)
    if seen is None:
        return False
    if time.monotonic() - seen >= NEGATIVE_CACHE_SECONDS:
        return False
    return True


def _mark_not_subscribed(tg_id: int) -> None:
    now = time.monotonic()
    # Заодно чистим словарь: он живёт всё время работы процесса.
    for stale in [key for key, seen in _not_subscribed_at.items() if now - seen >= NEGATIVE_CACHE_SECONDS]:
        _not_subscribed_at.pop(stale, None)
    _not_subscribed_at[tg_id] = now


def _recently_unavailable() -> bool:
    """Пауза после сбоя: не долбим Telegram API, пока он не отвечает."""
    return time.monotonic() < _unavailable_until


def _mark_unavailable() -> None:
    global _unavailable_until
    _unavailable_until = time.monotonic() + FAILURE_BACKOFF_SECONDS


async def is_subscribed(bot: Bot, tg_id: int) -> bool | None:
    """Подписан ли человек на канал.

    :returns: True — подписан, False — нет, None — проверить не удалось.
    """
    chat_id = settings.resolved_channel_id
    if not chat_id:
        return None
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=tg_id)
    except TelegramBadRequest as exc:
        if any(marker in str(exc).lower() for marker in _ABSENT_MARKERS):
            return False
        logger.warning("Проверка подписки на %s: Telegram отказал — %s", chat_id, exc)
        return None
    except TelegramForbiddenError as exc:
        logger.warning("Проверка подписки на %s: нет прав (бот не админ канала?) — %s", chat_id, exc)
        return None
    except TelegramAPIError as exc:
        logger.warning("Проверка подписки на %s не удалась: %s", chat_id, exc)
        return None

    status = str(getattr(member, "status", "") or "")
    if status in SUBSCRIBED_STATUSES:
        return True
    # Ограниченный участник (не может писать, но читает) остаётся подписчиком.
    if getattr(member, "is_member", None) is True:
        return True
    return False


async def verdict(session: AsyncSession, bot: Bot, user: User, *, force: bool = False) -> Verdict:
    """Пускать ли человека дальше: активная подписка, кэш или проверка канала.

    :param force: проверить в Telegram заново, не веря кэшам. Так работает кнопка
        «Я подписался»: человек только что подписался и ждёт, что его пустят.
    """
    if not applies_to(user.tg_id):
        return Verdict(allowed=True, reason="off")
    if await _has_active_subscription(session, user):
        # Доступ уже оплачен (или это пробный период) — гейт не наше дело.
        return Verdict(allowed=True, subscribed=True, reason="active_subscription")
    if not force and _cache_fresh(user):
        return Verdict(allowed=True, subscribed=True, reason="cache")
    if not force and _recently_not_subscribed(user.tg_id):
        return Verdict(allowed=False, subscribed=False, reason="not_subscribed_recent")
    if not force and _recently_unavailable():
        # Совсем недавно проверить не получилось — не тратим запрос впустую.
        return Verdict(allowed=bool(settings.channel_gate_fail_open), reason="unavailable_recent")

    subscribed = await is_subscribed(bot, user.tg_id)
    if subscribed is True:
        remember(user)
        return Verdict(allowed=True, subscribed=True, reason="subscribed")
    if subscribed is False:
        _mark_not_subscribed(user.tg_id)
        return Verdict(allowed=False, subscribed=False, reason="not_subscribed")

    # Проверка не удалась: чаще всего бот не админ канала.
    _mark_unavailable()
    await _warn_admins_once(bot)
    return Verdict(allowed=bool(settings.channel_gate_fail_open), reason="unavailable")


async def admin_check(bot: Bot) -> tuple[bool, str]:
    """Стартовая самопроверка: видит ли бот участников канала.

    ``getChatMember`` по чужому id Telegram отдаёт только администратору
    канала, поэтому «бот видит себя» ≈ «бот админ». Этого достаточно, чтобы
    поймать самую частую ошибку настройки: гейт включили, а бота в канал
    администратором не добавили.

    :returns: (всё хорошо, объяснение для лога и админов).
    """
    chat_id = settings.resolved_channel_id
    if not chat_id:
        return True, "канал не задан — гейт подписки выключен"
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id=chat_id, user_id=me.id)
    except TelegramAPIError as exc:
        return False, f"Telegram не дал посмотреть канал {chat_id}: {exc}"

    status = str(getattr(member, "status", "") or "")
    if status in {"creator", "administrator"}:
        return True, f"бот администратор канала {chat_id} — проверка подписки работает"
    return (
        False,
        f"бот в канале {chat_id} со статусом «{status}», а нужен администратор: "
        "проверку подписки Telegram не отдаст",
    )


async def _has_active_subscription(session: AsyncSession, user: User) -> bool:
    sub = await subscriptions.get_subscription(session, user.id)
    return bool(sub and sub.is_active)


async def _warn_admins_once(bot: Bot) -> None:
    """Раз в час сообщить админам: гейт включён, но проверить подписку нельзя."""
    global _alerted_at
    now = time.monotonic()
    if _alerted_at is not None and now - _alerted_at < ALERT_COOLDOWN_SECONDS:
        return
    _alerted_at = now

    from app.services import notifications  # локальный импорт: нет цикла модулей

    what_next = (
        "Пока проверка не работает, бот пускает всех: гейт включён, но не действует "
        "(CHANNEL_GATE_FAIL_OPEN=true)."
        if settings.channel_gate_fail_open
        else "Пока проверка не работает, бот показывает экран подписки всем "
        "(CHANNEL_GATE_FAIL_OPEN=false) — это может закрыть доступ клиентам."
    )
    await notifications.notify_admins(
        bot,
        "⚠️ <b>Подписка на канал не проверяется</b>\n"
        f"Канал: <code>{settings.resolved_channel_id}</code>\n\n"
        "Проверь, что бот добавлен в канал <b>администратором</b> и что CHANNEL_ID верный.\n\n"
        f"{what_next}",
    )
