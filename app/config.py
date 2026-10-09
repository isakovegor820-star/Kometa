"""Конфигурация приложения.

Все секреты — только через переменные окружения / .env (см. .env.example).
В коде никаких токенов и реквизитов.
"""

from __future__ import annotations

import re
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

    # --- Бот уведомлений (только для команды) ---
    #: Отдельный бот ТОЛЬКО для уведомлений команде: оплаты, заявки, алерты
    #: нод, сводка. Пусто — уведомления идут через основного бота (прежнее
    #: поведение). Смысл разделения: оперативная сводка не тонет в личке
    #: клиентского бота, а сам клиентский бот не выглядит «служебным».
    notify_bot_token: str = ""
    #: Куда слать уведомления: id чатов через запятую (личка, группа, канал).
    #: Пусто — берём ADMIN_IDS. Для группы/канала нужен отрицательный id
    #: (``-100…``), и бот должен быть в этом чате.
    notify_chat_ids: str = ""
    #: Отдельный бот недоступен (токен отозван, сеть, лимиты) — не терять
    #: уведомление совсем: True — отправить через основного бота.
    notify_fallback_to_main: bool = True
    #: Час ежедневной сводки по UTC (18 = 21:00 МСК). Пусто — сводку не шлём.
    notify_digest_hour_utc: int = 18
    #: Показывать в уведомлении об оплате итог дня («сегодня: 3 оплаты»).
    notify_payment_totals: bool = True
    #: Сообщать об ошибках в обработчиках: одна и та же ошибка — раз в 10 минут.
    notify_errors: bool = True

    # --- Обязательная подписка на основной канал ---
    #: Канал для проверки подписки: @юзернейм или числовой id (приватный — -100…).
    #: Пусто — берём имя из CHANNEL_URL, если это публичная ссылка t.me/<имя>.
    channel_id: str = ""
    #: Требовать подписку на канал, пока у человека нет активной подписки.
    #: Включать, только когда бот добавлен АДМИНОМ канала: иначе Telegram не
    #: показывает участников и проверять нечем (см. CHANNEL_GATE_FAIL_OPEN).
    channel_gate_enabled: bool = False
    #: Сколько часов доверять успешной проверке, не дёргая Telegram API.
    #: 0 — проверять каждый раз (точнее, но дороже по лимитам API).
    channel_gate_cache_hours: int = 12
    #: Что делать, если проверить не удалось (бот не админ, сеть, лимиты):
    #: True — пускаем (теряем гейт, но не всех клиентов разом), False — показываем
    #: экран подписки. О сбое пишем в лог и раз в час — админам.
    channel_gate_fail_open: bool = True

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

    #: Домен дежурного DNS-канала (например ``t.example.com``) — нужен только
    #: для отчёта о готовности: сам туннель живёт отдельным приложением,
    #: в подписку он не попадает (docs/DNS-ТУННЕЛЬ-2026-10.md).
    dns_tunnel_domain: str = ""

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
    #: Точный список локаций для описания бота и канала («🇩🇪 Германия · 🇫🇮 …»).
    #: Отдельно от ``locations_note``: там формулировка для документов, здесь —
    #: факт для публичных текстов. Расхождение этих двух строк и дало 08.10.2026
    #: «2 локации» в описании бота при трёх работающих серверах.
    location_list: str = ""
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
    #: Добавлять ли к пробному доступу накопленные бонусные дни.
    #: По умолчанию НЕТ: иначе заработанные на друзьях дни уходят в бесплатный
    #: триал, и рефералка ничего не приносит (30 дней за друга → 33 дня бесплатно).
    #: Бонусы применяются к первой ОПЛАТЕ — там они сокращают срок окупаемости.
    trial_applies_bonus_days: bool = False
    order_ttl_minutes: int = 30

    # --- Реферальная программа ---
    #: Сколько дней получает пригласивший после первой оплаты друга.
    #: 14 дней ≈ 46 ₽ — это ~20 % потолка привлечения (см. docs/МАРКЕТИНГ-ЭКОНОМИКА.md).
    referral_bonus_days_referrer: int = 14
    #: Сколько дней получает пригласивший, когда друг ОПЛАЧИВАЕТ ПРОДЛЕНИЕ.
    #: Привязывает рефералку к удержанию, а не только к первой оплате.
    referral_bonus_days_renewal: int = 14
    #: Сколько дней получает приглашённый вместе со скидкой.
    referral_bonus_days_invited: int = 3
    #: Скидка приглашённому на первую оплату, проценты.
    referral_discount_percent: int = 30
    #: Потолок скидки в рублях (0 = без потолка). 240 ₽ — чтобы годовой тариф
    #: (959 ₽) не отдавать вдвое дешевле: такая скидка стоила бы 480 ₽, вдвое
    #: больше потолка привлечения.
    referral_discount_max_rub: int = 240
    #: Защита от накрутки: сколько наград одному человеку в календарный месяц.
    referral_max_rewards_per_month: int = 10
    #: Сколько раз реферальный промокод может сработать (0 = без ограничения).
    promo_referral_uses_limit: int = 50

    # --- Подарочные сертификаты ---
    #: Включить покупку подписки в подарок (сертификат с активацией позже).
    gift_enabled: bool = True
    #: Сколько дней сертификат можно активировать после покупки.
    gift_valid_days: int = 365
    #: Наценка на подарочный сертификат, проценты. Подарок стоит дороже
    #: обычной подписки: это не скидка, а отдельный продукт с отсрочкой.
    gift_markup_percent: int = 25
    #: Сколько дней получает покупатель сертификата, если получатель активировал его.
    gift_buyer_bonus_days: int = 7

    # --- Автосценарии в боте (жизненный цикл клиента) ---
    #: Включить автосценарии: подсказки, win-back и апселлы без ручной рассылки.
    lifecycle_enabled: bool = True
    #: Не беспокоить человека чаще, чем раз в N дней. Защита от «спама из бота».
    lifecycle_min_gap_days: int = 5
    #: Через сколько дней после начала триала без оплаты напомнить о тарифах.
    lifecycle_trial_days: int = 3
    #: Через сколько дней после окончания подписки попробовать вернуть клиента.
    lifecycle_winback_days: int = 7
    #: Через сколько дней после оплаты предложить тариф подлиннее (апселл).
    lifecycle_upsell_days: int = 14
    #: Через сколько дней активной подписки напомнить про реферальную программу.
    lifecycle_referral_days: int = 5

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
    stars_watch_threshold: int = 800
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

    # --- Platega.io: карты, СБП/QR, крипта ---
    #: MerchantId и API-ключ из личного кабинета platega.io (Настройки).
    platega_merchant_id: str = ""
    platega_secret: str = ""
    #: Какие методы показывать клиенту. Актуальные номера (docs.platega.io):
    #: 2 — СБП/QR, 3 — ЕРИП, 11 — карты, 12 — зарубежные карты, 13 — крипта,
    #: 14 — SberPay. Устаревший 10 (карты в старой документации) → 11.
    platega_methods: str = "2,11"
    #: Единицы суммы в API Platega: rubles (актуальная схема, по умолчанию)
    #: или kopecks. Менять только после `python -m app.tools.platega_check --probe`.
    platega_amount_unit: str = "rubles"
    #: Передавать metadata (userId/имя/IP) — требование антифрода у части
    #: магазинов Platega. Включать, если так сказал менеджер.
    platega_send_metadata: bool = False
    #: Рассчитываем ли на вебхук. ``false`` — сознательный режим «без публичного
    #: адреса»: оплата подтверждается опросом (``job_check_platega``) и кнопкой
    #: «Проверить оплату», доступ выдаётся в течение ~2 минут после платежа.
    #: Эндпоинт вебхука при этом продолжает работать: если адрес появится и его
    #: впишут в кабинет, оплата начнёт подтверждаться мгновенно, переключать
    #: ничего не нужно. Флаг влияет только на вердикт ``platega_check``.
    platega_use_webhook: bool = True
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
    def notify_chat_id_list(self) -> list[int]:
        """Куда слать админские уведомления: ``NOTIFY_CHAT_IDS`` или ``ADMIN_IDS``.

        Отличие от ``admin_id_list`` — отрицательные id: уведомления часто
        уводят в отдельную группу или канал команды (``-100…``). Пустая строка
        возвращает ADMIN_IDS, поэтому старые настройки работают как раньше.
        """
        source = self.notify_chat_ids.strip() or self.admin_ids
        result: list[int] = []
        for chunk in re.split(r"[,;\s]+", source):
            chunk = chunk.strip()
            if not re.fullmatch(r"-?\d+", chunk):
                continue
            value = int(chunk)
            if value and value not in result:
                result.append(value)
        return result

    @property
    def admin_panel_url(self) -> str:
        """Публичный адрес админ-панели (пусто — локальный адрес или выключена).

        Ссылка в уведомлении бесполезна, если ведёт на 127.0.0.1: с телефона
        она не откроется. Поэтому локальные адреса не подставляем вовсе.
        """
        base = self.public_base_url.rstrip("/")
        if not base or "127.0.0.1" in base or "localhost" in base:
            return ""
        return f"{base}/admin"

    @property
    def resolved_channel_id(self) -> str:
        """Канал для проверки подписки: ``@юзернейм`` или числовой id.

        Пусто — проверять нечего, и гейт не включается совсем. Это осознанно:
        лучше пустить всех, чем показывать «подпишись» на канал, который бот
        не видит (приватный канал по ссылке-приглашению так не проверить —
        ему нужен именно числовой CHANNEL_ID).
        """
        raw = self.channel_id.strip()
        if raw:
            return raw if raw.lstrip("-").isdigit() else "@" + raw.lstrip("@")
        # Публичная ссылка https://t.me/имя годится и без отдельного CHANNEL_ID.
        match = re.fullmatch(
            r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,32})/?",
            self.channel_url.strip(),
        )
        return "@" + match.group(1) if match else ""

    @property
    def channel_link(self) -> str:
        """Ссылка на канал для кнопки «Подписаться»."""
        if self.channel_url.strip():
            return self.channel_url.strip()
        chat_id = self.resolved_channel_id
        return f"https://t.me/{chat_id.lstrip('@')}" if chat_id.startswith("@") else ""

    @property
    def inbound_id_list(self) -> list[int]:
        # Разбор общий с нодами: три разных парсера одной строки расходились
        # в мелочах — «1 2» без запятой давало двенадцатый инбаунд.
        from app.panels.base import parse_inbound_ids

        return parse_inbound_ids(self.panel_inbound_ids)

    @property
    def platega_method_list(self) -> list[int]:
        """Методы оплаты Platega из строки «2,11,13» — только актуальные.

        Разбор живёт рядом с самими методами (``app/payments/platega.py``):
        устаревший ``10`` переводится в ``11``, чужие номера отбрасываются,
        дубликаты схлопываются. Иначе один и тот же ``code`` приезжал бы в
        реестр дважды и один способ оплаты молча затирал другой.
        """
        from app.payments.platega import parse_payment_methods

        return parse_payment_methods(self.platega_methods)

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
