"""Заглушка панели: работает в памяти, нужна для разработки и тестов.

Не требует ни сервера, ни сети — бот можно поднять и проверить весь сценарий
«пробный доступ → оплата → продление» без реальной ноды.
"""

from __future__ import annotations

import base64
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone

from app.panels.base import Inbound, PanelClient, PanelError, PanelUser, UserSpec

DAY = 86400


class FakePanel(PanelClient):
    name = "fake"

    def __init__(self, sub_base: str = "http://127.0.0.1:8080/fake-panel", host: str = "127.0.0.1") -> None:
        self.sub_base = sub_base.rstrip("/")
        self.host = host
        self._users: dict[str, PanelUser] = {}
        self._by_email: dict[str, str] = {}

    # --- служебное -----------------------------------------------------
    async def health(self) -> bool:
        return True

    async def list_inbounds(self) -> list[Inbound]:
        return [
            Inbound(id=1, remark="DE-Reality", protocol="vless", port=443, network="tcp", security="reality"),
            Inbound(id=2, remark="DE-AmneziaWG", protocol="wireguard", port=51820, network="udp", security=""),
        ]

    def _make_configs(self, user: PanelUser) -> list[str]:
        vless = (
            f"vless://{user.uuid}@{self.host}:443"
            "?type=tcp&security=reality&fp=chrome&pbk=FAKEPUBLICKEY&sni=www.microsoft.com&sid=ab12"
            f"&flow=xtls-rprx-vision#{user.email}-DE"
        )
        awg = f"amneziawg://{user.uuid}@{self.host}:51820?obfs=1#{user.email}-DE-WG"
        return [vless, awg]

    # --- основной контракт ---------------------------------------------
    async def create_user(self, spec: UserSpec) -> PanelUser:
        if spec.email in self._by_email:
            raise PanelError(f"пользователь {spec.email} уже существует")
        uid = str(spec.uuid or uuid_lib.uuid4())
        user = PanelUser(
            uuid=uid,
            email=spec.email,
            enabled=True,
            expires_at=datetime.now(timezone.utc) + timedelta(days=spec.days),
            traffic_limit_bytes=spec.traffic_gb * 1024**3,
            devices_limit=spec.devices,
            subscription_url=f"{self.sub_base}/{uid}",
        )
        self._users[uid] = user
        self._by_email[spec.email] = uid
        return user

    async def get_user(self, uuid: str) -> PanelUser | None:
        return self._users.get(uuid)

    async def find_user_by_email(self, email: str) -> PanelUser | None:
        uuid = self._by_email.get(email)
        return self._users.get(uuid) if uuid else None

    async def list_users(self) -> list[PanelUser]:
        """Все клиенты — как их отдаёт настоящая панель (для аудита)."""
        return list(self._users.values())

    async def update_user(
        self,
        uuid: str,
        *,
        extend_days: int | None = None,
        traffic_gb: int | None = None,
        devices: int | None = None,
        enable: bool | None = None,
    ) -> PanelUser:
        user = self._users.get(uuid)
        if user is None:
            raise PanelError(f"пользователь {uuid} не найден")
        if extend_days:
            base = user.expires_at or datetime.now(timezone.utc)
            if base < datetime.now(timezone.utc):
                base = datetime.now(timezone.utc)
            user.expires_at = base + timedelta(days=extend_days)
        if traffic_gb is not None:
            user.traffic_limit_bytes = traffic_gb * 1024**3
        if devices is not None:
            user.devices_limit = devices
        if enable is not None:
            user.enabled = enable
        return user

    async def delete_user(self, uuid: str) -> None:
        user = self._users.pop(uuid, None)
        if user is not None:
            self._by_email.pop(user.email, None)

    async def get_configs(self, uuid: str) -> list[str]:
        user = self._users.get(uuid)
        if user is None:
            raise PanelError(f"пользователь {uuid} не найден")
        return self._make_configs(user)

    # --- удобно для тестов ---------------------------------------------
    def encode_subscription(self, configs: list[str]) -> str:
        return base64.b64encode("\n".join(configs).encode()).decode()
