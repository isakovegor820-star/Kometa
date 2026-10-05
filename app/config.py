"""Конфигурация приложения.

Все секреты — только через переменные окружения / .env (см. .env.example).
В коде никаких токенов и реквизитов.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram ---
    bot_token: str = ""
    admin_ids: str = ""
    channel_url: str = ""
    support_username: str = ""

    # --- База ---
    db_url: str = ""

    # --- Панель ---
    panel_type: str = "fake"  # fake | xui
    panel_url: str = ""
    panel_token: str = ""
    panel_username: str = ""
    panel_password: str = ""
    panel_inbound_ids: str = ""
    panel_sub_base: str = ""

    # --- Веб ---
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    public_base_url: str = "http://127.0.0.1:8080"

    # --- Продукт ---
    trial_days: int = 3
    trial_gb: int = 10
    trial_devices: int = 1
    referral_bonus_days_referrer: int = 7
    referral_bonus_days_invited: int = 3
    order_ttl_minutes: int = 30

    # --- Платежи ---
    manual_payment_details: str = ""
    manual_payment_note: str = ""
    cryptobot_token: str = ""
    stars_enabled: bool = False
    #: Курс для счетов в крипте: сколько рублей стоит 1 USDT (API курса рубля не отдаёт).
    crypto_rub_per_usdt: float = 95.0
    #: Курс для Stars: сколько звёзд стоит 1 рубль. Используется только как
    #: запасной вариант — основная цена берётся из тарифа (Plan.price_stars).
    #: 0.91 ≈ 1.1 ₽ за звезду: Telegram платит разработчику ~$0.013/⭐ (~1.2 ₽),
    #: поэтому при курсе 1:1 сервис работал бы в убыток.
    stars_per_rub: float = 0.91

    log_level: str = "INFO"

    # ------------------------------------------------------------------
    @property
    def admin_id_list(self) -> list[int]:
        return [int(x) for x in self.admin_ids.replace(" ", "").split(",") if x.strip().isdigit()]

    @property
    def inbound_id_list(self) -> list[int]:
        return [int(x) for x in self.panel_inbound_ids.replace(" ", "").split(",") if x.strip().isdigit()]

    @property
    def resolved_db_url(self) -> str:
        if self.db_url:
            return self.db_url
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{DATA_DIR / 'kometa.db'}"

    @property
    def subscription_base(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/sub"


@lru_cache
def get_settings() -> Settings:
    return Settings()
