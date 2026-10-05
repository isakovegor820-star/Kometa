"""Реестр платёжных провайдеров: собирает доступные способы оплаты из настроек.

Важное свойство: реестр умеет собираться сам. Если `init()` забыли вызвать
(например, в тестах или в новом скрипте), ручная оплата всё равно доступна —
иначе бот молча показывал бы «приём оплаты недоступен» и терял продажи.
"""

from __future__ import annotations

from aiogram import Bot

from app.config import get_settings
from app.payments.base import PaymentProvider
from app.payments.manual import ManualProvider


class PaymentRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, PaymentProvider] = {}
        self._bot: Bot | None = None

    def init(self, bot: Bot) -> None:
        """Вызывается при старте бота: запоминаем Bot для Stars и собираем список."""
        self._bot = bot
        self._providers = {}
        self._build()

    def _build(self) -> None:
        settings = get_settings()
        providers: dict[str, PaymentProvider] = {}
        # Перевод по СБП показываем только когда реквизиты реально заполнены:
        # иначе пользователь увидит счёт с пустыми реквизитами и уйдёт.
        if settings.manual_payment_details.strip():
            providers["manual"] = ManualProvider()
        if settings.cryptobot_token:
            from app.payments.cryptobot import CryptoBotProvider

            providers["crypto"] = CryptoBotProvider(
                token=settings.cryptobot_token,
                rub_per_usdt=settings.crypto_rub_per_usdt,
                expires_in=settings.order_ttl_minutes * 60,  # счёт живёт столько же, сколько заказ
            )
        if settings.stars_enabled and settings.bot_token and self._bot is not None:
            from app.payments.stars import StarsProvider

            providers["stars"] = StarsProvider(bot=self._bot, stars_per_rub=settings.stars_per_rub)
        self._providers = providers

    def _ensure_ready(self) -> None:
        if not self._providers:
            self._build()

    def reload(self) -> None:
        """Пересобрать список (например, после смены настроек в тестах)."""
        self._providers = {}
        self._build()

    def get(self, code: str) -> PaymentProvider | None:
        self._ensure_ready()
        return self._providers.get(code)

    def available(self) -> list[PaymentProvider]:
        self._ensure_ready()
        return list(self._providers.values())

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()
        self._providers.clear()


payments = PaymentRegistry()
