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
    #: Юзернейм бота — подставляется в юридические документы (@name).
    bot_username: str = ""
    #: Почта поддержки (необязательно): банк просит контакт, группу не принимает.
    support_email: str = ""

    # --- Подписка ---
    #: Режим «белых списков»: у оператора проходят только TCP 80/443/22, поэтому
    #: UDP-профили (AmneziaWG/WireGuard, Hysteria2, TUIC) в подписке не отдаём —
    #: клиент иначе долбится в заведомо мёртвый профиль и считает, что VPN сломан.
    #: Включается, когда у ваших абонентов активен режим ограничений.
    subscription_tcp_only: bool = False

    #: URL, по которому клиент замеряет задержку профилей (Clash ``url-test``,
    #: sing-box ``urltest``). Пусто — берём ``<PUBLIC_BASE_URL>/ping``, если адрес
    #: публичный, иначе стандартный gstatic. Свой 204-эндпоинт нужен потому, что
    #: под ограничениями внешний тест-URL недоступен и клиент помечает мёртвыми
    #: ВСЕ профили, включая живые.
    subscription_test_url: str = ""

    #: Проба нод «глазами клиента»: TCP + TLS-рукопожатие до инбаунда с замером
    #: задержки. Панель может отвечать, а порт для клиента — нет, поэтому одной
    #: проверки API недостаточно.
    node_probe_enabled: bool = True
    node_probe_timeout: float = 5.0

    # --- Юридические документы (политика и соглашение) ---
    #: Исполнитель: ФИО самозанятого или наименование ИП/ООО. Пусто = плейсхолдер,
    #: документы с плейсхолдером банк не принимает — заполнить до отправки.
    legal_operator_name: str = ""
    legal_operator_inn: str = ""
    #: Дата актуальной редакции документов, ДД.ММ.ГГГГ.
    legal_updated_at: str = "06.10.2026"
    #: Постоянные ссылки на опубликованные документы (Telegra.ph или сайт).
    #: Пока пусто — бот показывает полный текст документа прямо в чате.
    privacy_url: str = ""
    terms_url: str = ""
    #: Ссылка на опубликованный прайс (страница «Цены и тарифы»).
    pricing_url: str = ""
    #: Сколько локаций и какие — фраза целиком, попадает в документы и в цены.
    locations_note: str = "несколько локаций с переключением в один тап"
    #: Имя профиля в приложении клиента (заголовок ``profile-title`` подписки).
    #: Кириллицу часть клиентов не читает из заголовка — отдаём её в base64.
    subscription_title: str = "Kometa"
    #: Как называть локации внутри подписки (фрагмент после ``#`` в конфигах).
    #: Панель отдаёт служебные имена вида ``DE-REALITY-firefox-u123-10GB📊`` —
    #: в приложении это выглядит мусором. Пусто — оставляем имена панели.
    location_title: str = ""

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
    #: TLS веб-слоя: пути к сертификату и ключу. Оба пусты — работаем по http.
    #: Нужно потому, что Happ и v2rayNG отказываются добавлять подписку по
    #: незащищённой схеме («Небезопасная схема HTTP запрещена»).
    web_ssl_cert: str = ""
    web_ssl_key: str = ""

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
    #: Доверять заголовку X-Forwarded-For. Включать только когда панель стоит
    #: за своим реверс-прокси: иначе IP подделывается одним заголовком и
    #: ограничение «только localhost» перестаёт работать.
    admin_trust_proxy: bool = False

    # --- Продукт ---
    trial_days: int = 3
    #: Сколько ГБ входит в пробный доступ. 0 = безлимит (так и продаём).
    trial_gb: int = 0
    trial_devices: int = 1
    order_ttl_minutes: int = 30

    # --- Реферальная программа ---
    #: Сколько дней получает пригласивший после первой оплаты друга.
    referral_bonus_days_referrer: int = 30
    #: Сколько дней получает приглашённый вместе со скидкой.
    referral_bonus_days_invited: int = 3
    #: Скидка приглашённому на первую оплату, проценты.
    referral_discount_percent: int = 50
    #: Потолок скидки в рублях (0 = без потолка). Например, 300 ₽ —
    #: чтобы годовой тариф не отдавать вдвое дешевле.
    referral_discount_max_rub: int = 0
    #: Защита от накрутки: сколько наград одному человеку в календарный месяц.
    referral_max_rewards_per_month: int = 10
    #: Сколько раз реферальный промокод может сработать (0 = без ограничения).
    promo_referral_uses_limit: int = 50

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
    #: Прямой перевод на карту/по СБП без эквайринга — комиссии нет.
    fee_percent_manual: float = 0.0
    #: СБП через банк-партнёра (НСПК): QR-код или оплата по ссылке из
    #: банковского приложения — 8 % по условиям партнёра от 06.10.2026.
    #: Ставка зависит от объёма оборотов и пересматривается по мере роста.
    fee_percent_sbp: float = 8.0
    #: Криптоплатежи: 5 % — ставка партнёра от 06.10.2026.
    fee_percent_crypto: float = 5.0
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

    # --- Контроль клиентов (сторож аномалий) ---
    #: Суточная проверка клиентов панели с отчётом админам в Telegram.
    watch_enabled: bool = True
    #: Сколько ГБ за сутки считать аномалией и показывать в отчёте.
    watch_daily_gb: int = 100
    #: Снимок трафика: по нему считаем суточную разницу.
    watch_snapshot_file: str = "data/watch_snapshot.json"
    #: Клиенты, которые не считаются аномалией (свои устройства), через запятую.
    watch_ignore: str = ""
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

    #: Пробный доступ открыт? Живёт ОТДЕЛЬНО от продаж: можно пустить людей
    #: на 3 дня бесплатно, пока оплата ещё не подключена (SALES_ENABLED=false).
    trial_enabled: bool = True
    #: Продажи открыты? Выключить, пока нода не готова: бот не будет
    #: принимать деньги за услугу, которую пока не может выдать.
    sales_enabled: bool = True
    #: Что показывать клиенту, когда продажи закрыты.
    sales_closed_note: str = "Сервис готовится к запуску — продажи откроются совсем скоро."
    #: Что показывать, когда закрыт именно пробный доступ.
    trial_closed_note: str = "Пробный доступ откроется совсем скоро — следи за каналом."

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
    def support_contact(self) -> str:
        """Контакт поддержки для документов и кнопок: @юзернейм и/или почта.

        Группа в качестве поддержки не подходит — банк-партнёр принимает
        юзернейм, почту или тикет-систему, но не общий чат.
        """
        parts: list[str] = []
        if self.support_username:
            parts.append("@" + self.support_username.lstrip("@"))
        if self.support_email:
            parts.append(self.support_email)
        return " или ".join(parts)

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
