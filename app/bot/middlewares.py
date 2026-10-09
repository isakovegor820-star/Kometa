"""Middleware: сессия БД, антифлуд, подгрузка пользователя, гейт подписки."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot import gate, texts
from app.config import get_settings
from app.db.session import SessionMaker
from app.services import channel_gate, subscriptions

settings = get_settings()

#: Команды, которые обязаны работать всегда: по ним человек как раз и попадает
#: на экран подписки, а поддержка нужна именно тогда, когда ничего не выходит.
ALWAYS_ALLOWED_COMMANDS = frozenset({"/start", "/support"})

#: Кнопки уже созданного заказа — деньги в пути. Гейт их не трогает: у клиента
#: подписка могла кончиться, пока он оплачивал по СБП (окно заказа — 30 минут),
#: и тогда «Оплатил, но доступа нет» упиралось бы в экран подписки, а админ не
#: узнал бы об оплате вообще (AUTOPAY_ENABLED по умолчанию выключен). Каждая из
#: этих кнопок работает только с существующим заказом и без него отвечает
#: «заказ не найден», так что обходом это не пользуется. Выбор тарифа и способа
#: оплаты (``pay:*``, ``plans``, ``promo:*``) остаются за гейтом.
ORDER_CALLBACK_PREFIXES = ("order:manual:", "order:check:", "order:cancel:")


class DbSessionMiddleware(BaseMiddleware):
        """Одна сессия БД на апдейт + коммит в конце обработки."""

        async def __call__(
                self,
                handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                event: TelegramObject,
                data: dict[str, Any],
        ) -> Any:
                async with SessionMaker() as session:
                        data["session"] = session
                        try:
                                result = await handler(event, data)
                                await session.commit()
                                return result
                        except Exception:
                                await session.rollback()
                                raise


class UserMiddleware(BaseMiddleware):
        """Подкладывает в data нашего User (создаёт при первом обращении)."""

        async def __call__(self, handler, event, data):
                tg_user = data.get("event_from_user")
                session = data.get("session")
                if tg_user is not None and session is not None:
                        user, _ = await subscriptions.get_or_create_user(
                                session,
                                tg_id=tg_user.id,
                                username=tg_user.username,
                                first_name=tg_user.first_name,
                        )
                        data["user"] = user
                return await handler(event, data)


class ChannelGateMiddleware(BaseMiddleware):
        """Не пускает дальше тех, кто не подписан на канал.

        Гейт стоит в middleware, а не в хендлерах, намеренно: так он закрывает и
        кнопки, и новые разделы, которые появятся позже, — забыть про него в новом
        хендлере невозможно.

        Кого пропускаем без проверки:
            * админов — им гейт не нужен;
            * тех, у кого подписка на сервис активна: заплативший клиент не должен
                терять свою ссылку из-за выхода из канала;
            * ``/start`` — по нему человек как раз и попадает на экран подписки
                (и заодно привязывается пригласивший);
            * ``/support`` — поддержка нужна именно тогда, когда ничего не выходит;
            * кнопку «Я подписался»;
            * успешную оплату звёздами: это тоже сообщение, и терять из-за гейта
                оплаченный заказ нельзя.
        """

        async def __call__(
                self,
                handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                event: TelegramObject,
                data: dict[str, Any],
        ) -> Any:
                user = data.get("user")
                bot = data.get("bot")
                session = data.get("session")
                if user is None or bot is None or session is None:
                        return await handler(event, data)
                if _gate_exempt(event) or not channel_gate.applies_to(user.tg_id):
                        return await handler(event, data)

                result = await channel_gate.verdict(session, bot, user)
                if result.allowed:
                        return await handler(event, data)

                await _show_gate(event)
                return None


class ThrottlingMiddleware(BaseMiddleware):
        """Простая защита от флуда: не больше N действий в окно."""

        def __init__(self, rate: int = 5, window: float = 3.0) -> None:
                self.rate = rate
                self.window = window
                self._hits: dict[int, list[float]] = {}

        async def __call__(self, handler, event, data):
                tg_user = data.get("event_from_user")
                if tg_user is None or tg_user.id in settings.admin_id_list:
                        return await handler(event, data)

                now = time.monotonic()
                hits = [t for t in self._hits.get(tg_user.id, []) if now - t < self.window]
                if len(hits) >= self.rate:
                        self._hits[tg_user.id] = hits
                        if isinstance(event, Message):
                                await event.answer(texts.RATE_LIMITED)
                        elif isinstance(event, CallbackQuery):
                                await event.answer(texts.RATE_LIMITED, show_alert=False)
                        return None

                hits.append(now)
                self._hits[tg_user.id] = hits
                return await handler(event, data)


# ------------------------------------------------------- гейт подписки на канал
def _command_name(text: str | None) -> str:
        """Имя команды из текста сообщения: ``/start ref_x`` → ``/start``."""
        if not text or not text.startswith("/"):
                return ""
        return text.split()[0].split("@")[0].lower()


def _gate_exempt(event: TelegramObject) -> bool:
        """События, которые гейт пропускает всегда (см. ChannelGateMiddleware)."""
        if isinstance(event, CallbackQuery):
                data = event.data or ""
                if data.startswith(channel_gate.CALLBACK_PREFIX):
                        return True
                # Кнопки уже созданного заказа: см. ORDER_CALLBACK_PREFIXES.
                return data.startswith(ORDER_CALLBACK_PREFIXES)
        if isinstance(event, Message):
                # Оплата звёздами приходит обычным сообщением с successful_payment.
                # Заблокировать его гейтом — значит не выдать оплаченный доступ.
                if event.successful_payment is not None:
                        return True
                return _command_name(event.text) in ALWAYS_ALLOWED_COMMANDS
        return False


async def _show_gate(event: TelegramObject) -> None:
        """Объяснить, что делать: экран подписки вместо запрошенного действия."""
        if isinstance(event, CallbackQuery):
                # Сначала всплывающее окно: человек нажал кнопку и должен увидеть
                # причину прямо там, а не гадать, почему ничего не произошло.
                # Старая кнопка может «просрочиться» — это не повод падать.
                with contextlib.suppress(TelegramAPIError):
                        await event.answer(
                                texts.CHANNEL_GATE_SHORT.format(check_btn=texts.CHANNEL_GATE_BTN_CHECK),
                                show_alert=True,
                        )
                await gate.show_on_call(event)
                return
        if isinstance(event, Message):
                with contextlib.suppress(TelegramAPIError):
                        await gate.show(event)
