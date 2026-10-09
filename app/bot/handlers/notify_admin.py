"""Команды бота уведомлений: сводка по запросу и кнопки заявок.

Бот уведомлений — служебный: клиентов в нём нет, поэтому здесь нет ни гейта
подписки, ни клиентских сценариев. Только то, что нужно команде: посмотреть
цифры и подтвердить заявку, не открывая ноутбук.

Доступ: id из ``NOTIFY_CHAT_IDS`` или ``ADMIN_IDS``. Если бот стоит в группе
команды, «свои» определяются по ``ADMIN_IDS`` — добавь туда коллег.

Сообщения **клиентам** отсюда уходят основным ботом (``notify_bot.customer_bot``):
человек должен получить письмо от бота, которого знает, а не от служебного.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts, view
from app.config import get_settings
from app.db.models import User
from app.panels.base import PanelError
from app.services import digest, notifications, notify_bot, orders, stats, subscriptions

logger = logging.getLogger(__name__)
settings = get_settings()
router = Router(name="notify-admin")


def staff_ids() -> set[int]:
    """Кто может пользоваться служебным ботом."""
    return set(settings.notify_chat_id_list) | set(settings.admin_id_list)


class IsStaff(BaseFilter):
    """Пускать только команду: в сводке — выручка и телефоны клиентов.

    Бот уведомлений может стоять в группе команды, поэтому проверяем автора
    сообщения, а не id чата.
    """

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        return user is not None and user.id in staff_ids()


router.message.filter(IsStaff())
router.callback_query.filter(IsStaff())


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message) -> None:
    """Что это за бот, куда он пишет и что умеет."""
    chat_id = message.chat.id
    lines = [
        " <b>Kometa — бот уведомлений</b>",
        "",
        "Сюда приходят события сервиса:",
        "• 💰 оплаты и заявки на подтверждение;",
        "• ⚠️ алерты нод и панели, ошибки бота;",
        "• 📊 сводка за сутки (деньги, люди, инфраструктура).",
        "",
        "<b>Команды</b>",
        "/status — состояние сервиса и админ-панель",
        "/money — деньги: сегодня, 7 и 30 дней",
        "/nodes — ноды и открытые алерты",
        "/digest — полная сводка прямо сейчас",
        "",
        f"<code>chat_id</code> этого чата: <code>{chat_id}</code> — "
        "впиши его в <code>NOTIFY_CHAT_IDS</code>, чтобы уведомления шли сюда.",
    ]
    if notify_bot.problem():
        lines += ["", f"⚠️ Токен бота уведомлений не принят: {notify_bot.problem()}"]
    await message.answer("\n".join(lines))


@router.message(Command("status"))
async def cmd_status(message: Message, session: AsyncSession) -> None:
    """Цифры сервиса: то же, что /admin у основного бота, но в чате команды."""
    snapshot = await stats.collect(session)
    text = snapshot.as_text()
    url = _web_panel_url()
    if url:
        text += f"\n\n Админ-панель: {url}"
    await message.answer(text)


@router.message(Command("money"))
async def cmd_money(message: Message, session: AsyncSession) -> None:
    await message.answer(await digest.build_money(session))


@router.message(Command("nodes"))
async def cmd_nodes(message: Message, session: AsyncSession) -> None:
    await message.answer(await digest.build_infra(session))


@router.message(Command("digest"))
async def cmd_digest(message: Message, session: AsyncSession) -> None:
    await message.answer(await digest.build_daily(session, title="Сводка по запросу"))


def _web_panel_url() -> str:
    """Публичный адрес админ-панели (пусто, если он локальный)."""
    base = settings.public_base_url.rstrip("/")
    if not base or "127.0.0.1" in base or "localhost" in base:
        return ""
    return f"{base}/admin"


# ------------------------------------------------------- заявки на подтверждение
@router.callback_query(F.data.startswith("adm:confirm:"))
async def cb_confirm_order(call: CallbackQuery, session: AsyncSession) -> None:
    """Подтвердить ручную оплату прямо из чата уведомлений.

    Логика та же, что у кнопок основного бота (``app/bot/handlers/admin.py``),
    но письма клиенту уходят основным ботом: подтверждение пришло из
    служебного чата, а клиент знает только бота продаж.
    """
    order = await orders.get_order(session, _order_id(call.data))
    if order is None:
        await call.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    if order.status != "pending":
        await call.answer(f"Заказ уже в статусе {order.status}", show_alert=True)
        return

    user = await session.get(User, order.user_id)
    panels = await subscriptions.all_user_panels(session)
    client_bot = notify_bot.customer_bot()
    try:
        sub, already = await orders.mark_paid(
            session,
            order,
            panels,
            confirmed_by=call.from_user.id if call.from_user else None,
            bot=client_bot,
        )
    except (PanelError, ValueError) as exc:
        logger.exception("Подтверждение заявки #%s из бота уведомлений: %s", order.id, exc)
        await call.answer("Панель не выдала доступ — смотри уведомление выше", show_alert=True)
        await notifications.notify_admins(
            client_bot,
            f"⚠️ Панель не выдала доступ по заказу #{order.id}: <code>{exc}</code>",
        )
        return

    await _safe_edit(call, texts.ADMIN_CONFIRMED.format(order_id=order.id), order.id)
    await call.answer("Подтверждено ✅")

    if sub is None and not already and not order.gift_token:
        # Оплата зафиксирована, выдача не подтверждена — заказ доведёт фоновая
        # задача. Это не ошибка подтверждения, но админ должен видеть статус.
        await notifications.notify_admins(
            client_bot,
            f"⚠️ Заказ #{order.id} подтверждён, но доступ не выдан: "
            f"<code>{order.grant_last_error or 'панель не ответила'}</code>\n"
            "Повторю выдачу автоматически в течение нескольких минут.",
        )

    if client_bot is None or user is None:
        logger.warning("Заказ #%s подтверждён, но клиенту не сообщить: нет основного бота", order.id)
        return

    # Подарочный заказ: покупателю уходит сертификат, а не доступ.
    # Через getattr: поле и модуль подарков есть не во всех сборках (подарки
    # выкатываются отдельно), и падать на этом подтверждение оплаты нельзя.
    gift_token = getattr(order, "gift_token", None)
    if not already and gift_token:
        gift_service = _gift_service()
        if gift_service is not None:
            sent = await gift_service.notify_buyer(session, order, client_bot, user)
            if not sent:
                await notifications.notify_admins(
                    client_bot,
                    f"⚠️ Заказ-подарок #{order.id} оплачен, но сертификат не доставлен "
                    f"(покупатель <code>{user.tg_id}</code>). Напиши ему вручную.",
                )
        else:
            await notifications.notify_admins(
                client_bot,
                f"⚠️ Заказ #{order.id} — подарочный, но модуль подарков не установлен. "
                "Выдай сертификат вручную.",
            )
        return

    if sub is None or already:
        return
    try:
        await client_bot.send_message(
            user.tg_id,
            texts.order_paid_text(
                order,
                expires=sub.expires_at.strftime("%d.%m.%Y %H:%M") if sub.expires_at else "—",
                days=sub.days_left,
                link=subscriptions.subscription_link(sub.subscription_token),
            ),
        )
        await client_bot.send_message(
            user.tg_id,
            texts.SUBSCRIPTION_LINK_HINT.format(
                link=subscriptions.subscription_link(sub.subscription_token)
            ),
        )
    except Exception as exc: # noqa: BLE001 - доступ выдан, письмо вторично
        logger.warning("Доступ выдан, но письмо клиенту %s не ушло: %s", user.tg_id, exc)
        await notifications.notify_admins(
            client_bot,
            f"⚠️ Заказ #{order.id} подтверждён, доступ выдан, но письмо клиенту "
            f"<code>{user.tg_id}</code> не ушло: {exc}",
        )


@router.callback_query(F.data.startswith("adm:reject:"))
async def cb_reject_order(call: CallbackQuery, session: AsyncSession) -> None:
    """Отклонить заявку: деньги не пришли (или пришли не те)."""
    order = await orders.get_order(session, _order_id(call.data))
    if order is None:
        await call.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    await orders.cancel_order(
        session, order, reason=f"rejected by {call.from_user.id if call.from_user else '—'}"
    )
    await _safe_edit(call, texts.ADMIN_REJECTED.format(order_id=order.id), order.id)
    await call.answer("Отклонено")

    client_bot = notify_bot.customer_bot()
    user = await session.get(User, order.user_id)
    if client_bot is None or user is None:
        return
    try:
        await client_bot.send_message(
            user.tg_id,
            f"Заказ #{order.id} отклонён. Если оплата прошла — напиши в поддержку, разберёмся.",
        )
    except Exception: # noqa: BLE001 - клиент мог заблокировать бота
        logger.warning("Не смог уведомить пользователя %s об отклонении заказа", user.tg_id)


def _gift_service(): # noqa: ANN202 - модуль подарков или None
    """Модуль подарочных сертификатов, если он есть в этой сборке.

    Подарки выкатываются отдельно от уведомлений: на сборке без них
    подтверждение оплаты всё равно должно работать.
    """
    try:
        from app.services import gift as gift_service
    except ImportError:
        return None
    return gift_service


def _order_id(data: str | None) -> int:
    """Номер заказа из callback_data вида ``adm:confirm:17``."""
    try:
        return int((data or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return 0


async def _safe_edit(call: CallbackQuery, text: str, order_id: int) -> None:
    """Показать итог вместо кнопок.

    Кнопки могли нажать дважды (второй админ, случайный тап) — тогда
    редактировать нечего, и падать из-за этого незачем. Клавиатуру снимаем:
    решение уже принято, второй раз нажимать нечего.
    """
    if call.message is None:
        return
    try:
        await view.edit_screen(call.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[]))
    except Exception: # noqa: BLE001 - сообщение могло устареть
        logger.debug("Не удалось обновить сообщение заявки #%s", order_id)


def build_router() -> Router:
    return router
