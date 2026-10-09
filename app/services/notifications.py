"""Уведомления пользователям: напоминания о продлении, истечение, ответы поддержки."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from html import escape, unescape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Subscription, User
from app.services import notify_bot, subscriptions

logger = logging.getLogger(__name__)
settings = get_settings()

#: Теги, которые мы сами ставим в текст уведомлений.
_TAG_RE = re.compile(r"</?(?:b|i|u|s|code|pre|a|tg-spoiler)(?:\s[^>]*)?>", re.IGNORECASE)


async def _send(bot: Bot, tg_id: int, text: str, markup=None) -> bool:  # noqa: ANN001 - InlineKeyboardMarkup
    """Отправить сообщение и не потерять его из-за разметки.

    Вторая попытка без HTML: если в имени клиента оказались ``<`` или ``>``
    (Telegram такие имена разрешает), Telegram отклоняет сообщение целиком с
    «can't parse entities». Терять уведомление об оплате из-за чужого ника
    нельзя — отправляем тот же текст простым, разметку убираем.
    """
    try:
        await bot.send_message(tg_id, text, disable_web_page_preview=True, reply_markup=markup)
        return True
    except TelegramBadRequest as exc:
        if "parse entities" not in str(exc).lower():
            logger.warning("Не удалось отправить сообщение %s: %s", tg_id, exc)
            return False
        logger.warning("Разметка не разобралась (%s) — шлю простым текстом", tg_id)
        try:
            await bot.send_message(
                tg_id,
                plain(text),
                parse_mode=None,
                disable_web_page_preview=True,
                reply_markup=markup,
            )
            return True
        except TelegramAPIError as second:  # пользователь мог заблокировать бота
            logger.warning("Не удалось отправить сообщение %s: %s", tg_id, second)
            return False
    except TelegramAPIError as exc:  # пользователь мог заблокировать бота
        logger.warning("Не удалось отправить сообщение %s: %s", tg_id, exc)
        return False


def safe(value: object) -> str:
    """Экранировать текст для HTML-сообщения.

    Имена клиентов приходят от Telegram, а Telegram разрешает в имени ``<``
    и ``>`` (живой пример: ``^l<oc₽``). Без экранирования Telegram отклоняет
    сообщение целиком — «can't parse entities» — и уведомление об оплате
    такого клиента просто не приходит: деньги есть, а сообщения нет.
    """
    return escape(str(value), quote=False)


def plain(text: str) -> str:
    """Убрать нашу разметку: запасной вариант, когда HTML не разобрался."""
    return unescape(_TAG_RE.sub("", text))


async def notify_expiring(bot: Bot, session: AsyncSession, days_before: int) -> int:
    """Напомнить тем, у кого подписка кончается через N дней."""
    subs = await subscriptions.due_for_reminder(session, days_before)
    sent = 0
    for sub in subs:
        user = await session.get(User, sub.user_id)
        if user is None or user.is_blocked:
            continue
        when = "завтра" if days_before == 1 else f"через {days_before} дня"
        text = (
            f"⏳ Подписка заканчивается {when}.\n\n"
            f"Продлить в один клик — раздел «Моя подписка» → «Продлить».\n"
            "Если ничего не делать, доступ отключится автоматически, данные сохраним 30 дней."
        )
        if await _send(bot, user.tg_id, text):
            sent += 1
    return sent


async def notify_expired(bot: Bot, session: AsyncSession, changed: list[Subscription]) -> int:
    sent = 0
    for sub in changed:
        user = await session.get(User, sub.user_id)
        if user is None or user.is_blocked:
            continue
        text = (
            "⌛️ Подписка закончилась, доступ отключён.\n\n"
            "Продлить можно в любой момент — доступ вернётся сразу после оплаты, "
            "настройки в приложении менять не нужно."
        )
        if await _send(bot, user.tg_id, text):
            sent += 1
    return sent


def admin_target(bot: Bot | None) -> Bot | None:
    """Каким ботом слать сообщение команде.

    Приоритет — бот уведомлений. Если он настроен, но не поднялся, отдаём
    основной бот (``NOTIFY_FALLBACK_TO_MAIN=false`` это выключает) — иначе
    про отозванный токен уведомлений никто бы не узнал. Бот не настроен
    совсем — прежнее поведение: уведомления идут основным ботом.
    """
    target = notify_bot.bot()
    if target is not None:
        return target
    if not notify_bot.configured():
        return bot
    if not settings.notify_fallback_to_main:
        return None
    logger.warning(
        "Бот уведомлений недоступен (%s) — шлю основным ботом",
        notify_bot.problem() or "нет бота",
    )
    return bot


async def notify_admins(bot: Bot | None, text: str) -> int:
    """Сообщение команде: оплаты, заявки, алерты, сводки.

    Уходит **ботом уведомлений** (``NOTIFY_BOT_TOKEN``) в чаты
    ``NOTIFY_CHAT_IDS``, а если он не настроен — основным ботом, как раньше.
    Смысл разделения: клиентская личка не смешивается с оперативной сводкой,
    и уведомление об оплате не теряется среди переписки с клиентами.

    Возвращает число доставленных сообщений: 0 — не дошло никуда.
    """
    target = admin_target(bot)
    if target is None:
        logger.warning("Сообщение команде не отправлено (нет бота): %s", text[:120])
        return 0

    sent = 0
    for chat_id in settings.notify_chat_id_list:
        if await _send(target, chat_id, text):
            sent += 1
    return sent


async def notify_admins_with_buttons(bot: Bot | None, text: str, markup) -> int:  # noqa: ANN001
    """Сообщение команде с кнопками решения — заявка на ручную оплату.

    Кнопки уводят решение в тот же чат, где видно уведомление: подтвердить
    оплату с телефона, не открывая админку. Пространство имён в
    ``callback_data`` задаёт ``notify_bot.namespace()`` — обработчик живёт
    в том боте, который отправил сообщение.
    """
    target = admin_target(bot)
    if target is None:
        logger.warning("Заявка не отправлена (нет бота): %s", text[:120])
        return 0

    sent = 0
    for chat_id in settings.notify_chat_id_list:
        if await _send(target, chat_id, text, markup):
            sent += 1
    return sent


#: Об одной и той же ошибке пишем не чаще, чем раз в это время: упавший
#: обработчик срабатывает на каждом апдейте, и без паузы чат превратился бы
#: в поток одинаковых сообщений — их перестают читать.
ERROR_NOTIFY_COOLDOWN = timedelta(minutes=10)
_error_seen: dict[str, datetime] = {}


async def notify_error(
    bot: Bot | None,
    *,
    where: str,
    exc: BaseException,
    user_id: int | None = None,
) -> bool:
    """Сообщить команде, что у клиента что-то сломалось.

    Ошибка в обработчике раньше оставалась только в логе на сервере: клиент
    получал «что-то пошло не так», а владелец узнавал об этом из жалобы.
    Одинаковые ошибки глушим на ``ERROR_NOTIFY_COOLDOWN``.
    """
    if not settings.notify_errors:
        return False

    signature = f"{where}:{type(exc).__name__}"
    now = datetime.now(timezone.utc)
    last = _error_seen.get(signature)
    if last is not None and now - last < ERROR_NOTIFY_COOLDOWN:
        return False
    _error_seen[signature] = now

    text = (
        "🔴 <b>Ошибка в боте</b>\n"
        f"Где: <code>{safe(where)}</code>\n"
        f"Тип: <code>{type(exc).__name__}</code>\n"
        f"Текст: {safe(str(exc)[:300]) or '—'}"
    )
    if user_id:
        text += f"\nКлиент: <code>{user_id}</code>"
    text += "\n\nПодробности — в <code>logs/bot.log</code>."
    return bool(await notify_admins(bot, text))


async def payment_notice(
    session: AsyncSession,
    order,  # noqa: ANN001 - Order
    user,  # noqa: ANN001 - User
    *,
    title: str = "Оплата получена",
    provider: str = "",
    plan_title: str = "",
    note: str = "",
) -> str:
    """Одно уведомление об оплате для всех способов: что купили и кто платил.

    Раньше каждый провайдер писал своим текстом, и часть уведомлений приходила
    без суммы, тарифа или клиента — приходилось открывать админку, чтобы
    понять, что вообще произошло. Здесь единый формат плюс две вещи, которых
    не хватало: итог дня («120 ₽ — это норма или провал?») и ссылка в админку.
    """
    from app.services import digest

    if order.amount_rub:
        amount = f"<b>{order.amount_rub} ₽</b>"
        if order.discount_rub:
            amount += f", скидка {order.discount_rub} ₽"
        if order.stars_amount:
            # Звёздный заказ: рубли клиенту важны (с ними сравнивают тариф),
            # а звёзды — сколько реально списали.
            amount += f" ({order.stars_amount} ⭐)"
    elif order.stars_amount:
        amount = f"<b>{order.stars_amount} ⭐</b>"
    else:
        amount = "<b>0 ₽</b>"

    head = f"Заказ #{order.id} · {amount}"
    if plan_title:
        head += f" · {safe(plan_title)}"
    parts = [
        f"💰 <b>{safe(title)}</b>",
        head,
        f"Способ: {safe(provider or order.provider)} · клиент: {safe(user.display_name)} "
        f"(<code>{user.tg_id}</code>)",
    ]
    if note:
        parts.append(safe(note))
    totals = await digest.payment_totals_note(session)
    if totals:
        parts.append(totals.strip())
    if settings.admin_panel_url:
        parts.append(f"🌐 {settings.admin_panel_url}/orders")
    return "\n".join(parts)


async def notify_referral_reward(bot: Bot, reward) -> bool:  # noqa: ANN001 - referral.Reward
    """Сказать пригласившему, что друг оплатил и бонус начислен.

    Без этого сообщения люди не понимают, что рефералка работает:
    «я привёл друга, а где мои дни?» — самый частый вопрос в поддержку.
    Продление друга приходит отдельным текстом: там награда меньше,
    но повторяется, и это стоит проговорить.
    """
    referrer = reward.referrer
    if referrer.is_blocked:
        return False

    if reward.referrer_accrued:
        tail = (
            f"Дни уже в запасе: <b>{referrer.bonus_days_balance} дн.</b>\n\n"
            "Как только подключишься (пробный доступ или оплата) — они добавятся к сроку "
            "автоматически."
        )
    else:
        expires = ""
        if reward.referrer_sub is not None and reward.referrer_sub.expires_at:
            expires = f"\nПодписка теперь активна до <b>{reward.referrer_sub.expires_at:%d.%m.%Y}</b>."
        tail = f"Дни уже начислены.{expires}"

    if reward.renewal_days:
        text = (
            "🔁 <b>Твой друг продлил подписку!</b>\n\n"
            f"Тебе начислено ещё <b>+{reward.referrer_days} дней</b> — "
            f"это {reward.renewal_number}-я оплата друга.\n\n"
            f"{tail}\n\n"
            "Дни капают за каждое продление — раздел «Пригласить друга»."
        )
    else:
        text = (
            "🎉 <b>Твой друг оплатил подписку!</b>\n\n"
            f"Тебе начислено <b>+{reward.referrer_days} дней</b> бесплатно.\n\n"
            f"{tail}\n\n"
            "Приглашай ещё — дни копятся: раздел «Пригласить друга»."
        )
    return await _send(bot, referrer.tg_id, text)


async def notify_referral_limit(bot: Bot, reward) -> bool:  # noqa: ANN001 - referral.Reward
    """Честно предупредить, что месячный лимит наград исчерпан."""
    referrer = reward.referrer
    if referrer.is_blocked:
        return False
    text = (
        "Друг оплатил подписку, но месячный лимит наград исчерпан — "
        f"бонус за эту оплату не начислен.\n\n"
        f"Лимит: {settings.referral_max_rewards_per_month} наград в месяц. "
        "Напиши в поддержку — разберёмся."
    )
    return await _send(bot, referrer.tg_id, text)
