"""Абстракция панели управления VPN.

Наш код не знает деталей конкретной панели: он работает через PanelClient.
Это позволяет заменить 3x-ui на Remnawave без переписывания бота.

Реализации:
  * app/panels/fake.py — локальная заглушка (разработка и тесты);
  * app/panels/xui.py  — реальная панель 3x-ui.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


class PanelError(RuntimeError):
    """Ошибка при обращении к панели (сеть, авторизация, валидация)."""


@dataclass(slots=True)
class Inbound:
    """Входящее подключение (инбаунд) в панели."""

    id: int
    remark: str
    protocol: str
    port: int
    network: str = ""
    security: str = ""


@dataclass(slots=True)
class UserSpec:
    """Параметры создаваемого пользователя."""

    email: str
    days: int
    traffic_gb: int = 0  # 0 = безлимит
    devices: int = 3
    note: str = ""


@dataclass(slots=True)
class PanelUser:
    """Состояние пользователя в панели."""

    uuid: str
    email: str
    enabled: bool = True
    expires_at: datetime | None = None
    traffic_limit_bytes: int = 0  # 0 = безлимит
    devices_limit: int = 0
    used_bytes: int = 0
    subscription_url: str = ""
    raw: dict = field(default_factory=dict)


class PanelClient(ABC):
    """Единый интерфейс панели."""

    name: str = "base"

    @abstractmethod
    async def health(self) -> bool:
        """Панель отвечает и авторизация проходит."""

    @abstractmethod
    async def list_inbounds(self) -> list[Inbound]:
        """Список инбаундов, из которых собирается подписка."""

    @abstractmethod
    async def create_user(self, spec: UserSpec) -> PanelUser:
        """Создать пользователя с заданным сроком/лимитами."""

    @abstractmethod
    async def get_user(self, uuid: str) -> PanelUser | None:
        """Получить состояние пользователя или None, если его нет."""

    async def find_user_by_email(self, email: str) -> PanelUser | None:
        """Найти пользователя по логину (email).

        Нужен для восстановления: если панель уже знает такого пользователя
        (например, БД бота восстановили из бэкапа), мы не должны терять
        оплаченный доступ — находим его и продлеваем.
        По умолчанию панель может не поддерживать поиск.
        """
        return None

    @abstractmethod
    async def update_user(
        self,
        uuid: str,
        *,
        extend_days: int | None = None,
        traffic_gb: int | None = None,
        devices: int | None = None,
        enable: bool | None = None,
    ) -> PanelUser:
        """Продлить/изменить лимиты/включить-выключить доступ."""

    @abstractmethod
    async def delete_user(self, uuid: str) -> None:
        """Удалить пользователя из панели."""

    @abstractmethod
    async def get_configs(self, uuid: str) -> list[str]:
        """Готовые строки конфигов (vless://, amneziawg://, ...) для подписки."""

    async def close(self) -> None:  # pragma: no cover - переопределяется при необходимости
        """Освободить ресурсы (HTTP-клиент и т.п.)."""
