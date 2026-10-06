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

    # --- Админ-панель (/admin) ---
    #: Пароль для входа. Пусто = панель выключена (безопасное поведение по умолчанию).
    admin_panel_password: str = ""
    #: Ключ подписи сессионной cookie. Пусто = используется токен бота.
    admin_panel_secret: str = ""
    #: Время жизни сессии в часах.
    admin_session_hours: int = 12
    #: Панель доступна только с localhost (через SSH-туннель). Так безопасно
    #: работать без домена и HTTPS: пароль не уходит в открытый интернет.
    admin_local_only: bool = True
    #: Список IP через запятую, которым панель доступна дополнительно
    #: (например, домашний IP владельца). Работает при ADMIN_LOCAL_ONLY=true.
    admin_allowed_ips: str = ""

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
    #: Сколько звёзд стоит 1 рубль. Используется только как
    #: запасной вариант — основная цена берётся из тарифа (Plan.price_stars).
    #: 0.91 ≈ 1.1 ₽ за звезду: Telegram платит разработчику ~$0.013/⭐ (~1.2 ₽),
    #: поэтому при курсе 1:1 сервис работал бы в убыток.
    stars_per_rub: float = 0.91
    #: Покупки звёздами от этой суммы попадают под контроль админа: дешёвые
    #: звёзды у перекупов бывают крадеными, и Telegram может списать их обратно.
    stars_watch_threshold: int = 1000
    #: Ссылка на бот/сервис, где клиент может купить звёзды, если у него их нет.
    #: Пусто — кнопка не показывается.
    stars_reseller_url: str = ""

    # --- Экономика приёма платежей (для отчёта о прибыли) ---
    #: Комиссия канала в процентах от оборота (0 = без комиссии).
    fee_percent_manual: float = 0.0
    fee_percent_crypto: float = 0.0
    fee_percent_wata: float = 3.5
    #: Сколько процентов теряется при выводе звёзд через Fragment.
    fragment_withdrawal_percent: float = 5.0
    #: Сколько долларов Telegram платит разработчику за одну звезду.
    stars_payout_usd: float = 0.013
    #: Курс доллара для расчёта «звёздной» выручки.
    usd_rub_rate: float = 92.0
    #: Постоянные расходы в месяц (серверы, домен) — для расчёта прибыли.
    monthly_costs_rub: float = 0.0

    # --- Автопроверка переводов по выписке банка ---
    #: Включить автоматическое подтверждение оплат по выписке.
    autopay_enabled: bool = False
    #: Как часто проверять выписку (минуты).
    autopay_interval_minutes: int = 5
    #: Допуск при сверке суммы (копейки): некоторые банки округляют.
    autopay_tolerance_kopecks: int = 0
    #: Файлы выписки (CSV/TXT), которые складывает банк или скрипт.
    statement_csv_glob: str = "data/statements/*.csv"
    #: Состояние обработки файлов (чтобы не подтвердить один платёж дважды).
    statement_state_file: str = "data/statement_state.json"
    #: Почтовые уведомления банка (IMAP).
    bank_imap_host: str = ""
    bank_imap_port: int = 993
    bank_imap_user: str = ""
    bank_imap_password: str = ""
    bank_imap_folder: str = "INBOX"
    #: Сообщать админам о поступлениях, которые не удалось сопоставить с заказом.
    autopay_notify_unmatched: bool = True

    # --- WATA: карты РФ и зарубежные, СБП, T-Pay, SberPay ---
    #: Access token терминала из личного кабинета merchant.wata.pro (живёт 1–12 месяцев).
    wata_token: str = ""
    #: Боевой API; песочница — https://api-sandbox.wata.pro/api/h2h
    wata_base_url: str = "https://api.wata.pro/api/h2h"
    #: Публичный ключ для проверки подписи вебхука. Пусто — скачаем через API.
    wata_public_key: str = ""
    #: Срок жизни платёжной ссылки в минутах (WATA: от 10 минут до 30 дней).
    wata_link_ttl_minutes: int = 30
    #: Куда вернуть плательщика после оплаты (необязательно).
    wata_success_redirect_url: str = ""
    wata_fail_redirect_url: str = ""

    # --- Platega.io: карты МИР, СБП/QR, крипта ---
    #: MerchantId и API-ключ из личного кабинета my.platega.io (Настройки).
    platega_merchant_id: str = ""
    platega_secret: str = ""
    #: Какие методы показывать клиенту: 2 — СБП/QR, 10 — карты МИР, 12 — зарубежные карты.
    platega_methods: str = "2,10"
    #: Единицы суммы в API Platega: kopecks (по умолчанию) или rubles.
    platega_amount_unit: str = "kopecks"
    #: Куда вернуть плательщика после оплаты (необязательно).
    platega_return_url: str = ""
    platega_failed_url: str = ""

    log_level: str = "INFO"

    # ------------------------------------------------------------------
    @property
    def admin_id_list(self) -> list[int]:
        return [int(x) for x in self.admin_ids.replace(" ", "").split(",") if x.strip().isdigit()]

    @property
    def inbound_id_list(self) -> list[int]:
        return [int(x) for x in self.panel_inbound_ids.replace(" ", "").split(",") if x.strip().isdigit()]

    @property
    def platega_method_list(self) -> list[int]:
        """Методы оплаты Platega из строки «2,10,12» (2 — СБП, 10 — карты МИР)."""
        return [int(x) for x in self.platega_methods.replace(" ", "").split(",") if x.strip().isdigit()]

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
