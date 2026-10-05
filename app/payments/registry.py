"""Реестр платёжных провайдеров: собирает доступные способы оплаты из настроек."""

from __future__ import annotations

from aiogram import Bot

from app.config import get_settings
from app.payments.base import PaymentProvider
from app.payments.cryptobot import CryptoBotProvider
from app.payments.manual import ManualProvider
from app.payments.stars import StarsProvider


class PaymentRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, PaymentProvider] = {}

    def init(self, bot: Bot) -> None:
        """Вызывается один раз при старте бота."""
        settings = get_settings()
        self._providers = {"manual": ManualProvider()}
        if settings.cryptobot_token:
            self._providers["crypto"] = CryptoBotProvider(token=settings.cryptobot_token)
        if settings.stars_enabled and settings.bot_token:
            self._providers["stars"] = StarsProvider(bot=bot)

    def get(self, code: str) -> PaymentProvider | None:
        return self._providers.get(code)

    def available(self) -> list[PaymentProvider]:
        return list(self._providers.values())

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()
        self._providers.clear()


payments = PaymentRegistry()
