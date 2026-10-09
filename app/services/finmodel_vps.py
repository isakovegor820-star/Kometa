"""Экономика связки VPS: одна панель + 1–3 ноды (Remnawave или 3x-ui).

Зачем отдельный модуль. «Сколько стоит сервис в месяц» — это не цена одного
сервера, а сумма четырёх вещей: панель (управление), ноды (трафик), резервный
фонд (ротация IP, переезды, замены) и комиссия канала оплаты. Плюс есть вторая
цифра, которая важнее первой: **себестоимость клиента-месяца** — именно она
определяет, при каком числе подписчиков сервис живёт, а не живёт на донаты.

Все функции чистые: их можно тестировать и вызывать из CLI без сети и БД.
Цены и курсы — снимок на 06.10.2026, они меняются: см. `docs/РЕМНАВАВЕ-СВЯЗКА.md`,
раздел «Что сверить перед покупкой».

Ключевое правило расчёта: цену в рублях считаем по курсу из `RATES`, а не по
«примерно». Иначе модель врёт на 10–20 % просто из-за валюты.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------- курсы валют

#: Курсы ЦБ РФ на 06.10.2026. Меняются каждый день — при обновлении документа
#: пересчитать таблицы и прогнать тесты. За полгода евро ходил от 82,54 (23.05.2026)
#: до 100,83 (03.09.2026), то есть размах ~22 %: валютный риск здесь реальный,
#: а не теоретический.
RATES: dict[str, float] = {
    "RUB": 1.0,
    "EUR": 95.3349,
    "USD": 84.9309,
}


def rub(amount: float, currency: str = "RUB", rates: dict[str, float] | None = None) -> float:
    """Перевести цену в рубли. Неизвестная валюта — ошибка, а не молчаливый ноль."""
    table = rates or RATES
    if currency not in table:
        raise ValueError(f"нет курса для валюты {currency!r}")
    return amount * table[currency]


# ---------------------------------------------------------------- деньги

#: Ставка, которую теряем с каждого чека: 8 % СБП + 2 % конвертации в USDT
#: (`net_after_partner_payout` в app/services/finmodel.py). Одна константа на всю
#: модель, чтобы безубыточность в CLI и в документе не расходилась на десятую.
#: Считаем не «10 %», а точную эффективную ставку: проценты применяются
#: последовательно, 1 − 0,92 × 0,98 = 9,84 %. В таблицах это по-прежнему «10 %».
DEFAULT_CHANNEL_FEE_PERCENT: float = round(100 * (1 - 0.92 * 0.98), 2)

#: Средний чек с клиента в месяц (тарифы 120/299/539/959 ₽, микс 50/25/15/10
#: и поправка на звёзды +3 %: 106,3 × 1,03 ≈ 109,5). Держать в согласии с
#: ``finmodel.DEFAULT_PLANS`` — расхождение ловит tests/test_price_consistency.py.
DEFAULT_AVERAGE_CHECK_RUB: float = 109.5

#: Сколько доходит до нас с одного клиента в месяц.
DEFAULT_NET_PER_USER: float = DEFAULT_AVERAGE_CHECK_RUB * (1 - DEFAULT_CHANNEL_FEE_PERCENT / 100)


# ---------------------------------------------------------------- серверы

#: Запас по каналу на пиковые часы: ночью канал простаивает, вечером упирается.
#: 0.75 — «канал загружен на 75 % в самый пиковый час месяца».
DEFAULT_PEAK_UTILIZATION: float = 0.75

#: Сколько Мбит/с держит одно ядро под Xray с Reality/XHTTP (эстимейт по замерам,
#: НЕ из документации). Используется только когда у оффера не указана скорость порта.
MBPS_PER_VCPU: float = 450.0

#: Профиль нагрузки. Ёмкость ноды считается не от числа подписчиков, а от того,
#: сколько их одновременно онлайн в вечерний пик и сколько каждый тянет.
#:
#: - `DEFAULT_CONCURRENCY` — доля подписчиков, одновременно активных в час пик.
#:   0.25 значит: из 100 клиентов 25 смотрят/качают прямо сейчас.
#: - `DEFAULT_MBPS_PER_SESSION` — средняя скорость активной сессии, Мбит/с.
#:   8 — это 1080p-видео с запасом; 25 — 4K или торренты.
DEFAULT_CONCURRENCY: float = 0.25
DEFAULT_MBPS_PER_SESSION: float = 8.0

#: Практический потолок подписчиков на одну ноду, независимо от канала.
#: Считается по опыту: панель и Xray держат состояние каждого пользователя в памяти,
#: а на 2 ГБ ОЗУ (наш текущий сервер) запас кончается раньше, чем канал. Поэтому
#: «ёмкость по каналу» — верхняя оценка, а не обещание: эксплуатационный предел ниже.
#: В ФИНМОДЕЛЬ.md и ТЗ стоит более старая оценка 30–60 клиентов на ноду: она про
#: 1 vCPU и первые месяцы, когда каждый клиент тянул заметно больше трафика.
#: Проверяется на реальной ноде (память, load average, число сессий) и уточняется.
MAX_USERS_PER_NODE: int = 250

#: Профили для таблицы чувствительности: от «лёгкого» до «тяжёлого» использования.
LOAD_PROFILES: dict[str, tuple[float, float]] = {
    "лёгкий": (0.15, 5.0),
    "обычный": (DEFAULT_CONCURRENCY, DEFAULT_MBPS_PER_SESSION),
    "видео вечером": (0.25, 12.0),
    "торренты и 4K": (0.35, 25.0),
}


@dataclass(frozen=True, slots=True)
class ServerOffer:
    """Конкретный сервер: где, сколько стоит, какой порт и что с трафиком.

    :param key: короткий код для ссылок из документа.
    :param provider: как называть в таблицах.
    :param location: локация («Варшава», «Молдова»…).
    :param price: цена в месяц в валюте `currency`.
    :param currency: валюта цены (RUB/EUR/USD).
    :param vcpu: ядер.
    :param ram_gb: ОЗУ, ГБ.
    :param disk_gb: диск, ГБ.
    :param port_mbps: скорость порта, Мбит/с.
    :param traffic: «∞» / «unmetered» / лимит текстом.
    :param pays_ru_card: принимают ли карту РФ / СБП.
    :param pays_crypto: принимают ли крипту.
    :param vpn_ok: политика по VPN: «да» / «нет» / «не сказано».
    :param hourly: есть ли почасовая оплата — критично для теста подсети.
    :param in_russia: сервер стоит в РФ (юрисдикция РКН, а не «упоминание РФ в тексте»).
    :param note: главный подвох одной фразой.
    """

    key: str
    provider: str
    location: str
    price: float
    currency: str = "EUR"
    vcpu: int = 2
    ram_gb: int = 2
    disk_gb: int = 20
    port_mbps: int = 1000
    traffic: str = "∞"
    pays_ru_card: bool = False
    pays_crypto: bool = True
    vpn_ok: str = "не сказано"
    hourly: bool = False
    in_russia: bool = False
    note: str = ""

    @property
    def price_rub(self) -> float:
        """Цена в рублях по курсу из `RATES`."""
        return rub(self.price, self.currency)

    @property
    def throughput_mbps(self) -> float:
        """Сколько живого трафика нода отдаёт: порт, но не больше, чем тянет CPU."""
        cpu_cap = self.vcpu * MBPS_PER_VCPU
        return min(float(self.port_mbps), cpu_cap)


#: Каталог офферов, проверенных 06.10.2026. Порядок — от «под ноду» к «под панель».
OFFERS: tuple[ServerOffer, ...] = (
    # --- зарубежные, реальный безлимит, KVM
    ServerOffer(
        "aeza-ams", "Aeza", "Амстердам", 5.93, "EUR", 1, 2, 30, 1000,
        traffic="∞ (оферта п.10.7: при «аномальной нагрузке» могут ограничить)",
        pays_ru_card=True, pays_crypto=True, vpn_ok="нет на РФ-IP, зарубеж — да",
        hourly=True, note="оферта допускает ограничение при аномальной нагрузке",
    ),
    ServerOffer(
        "aeza-waw", "Aeza", "Варшава", 5.93, "EUR", 1, 2, 30, 1000,
        traffic="∞, порт до 10–25 Гбит/с по линейке",
        pays_ru_card=True, pays_crypto=True, vpn_ok="да (локация вне РФ)",
        hourly=True, note="лучший пинг из проверенных EU-локаций",
    ),
    ServerOffer(
        "zetservers-ro", "ZetServers", "Бухарест", 29.0, "EUR", 1, 4, 50, 25000,
        traffic="unmetered: 25 Гбит/с порт, без троттлинга и доплат",
        pays_ru_card=False, pays_crypto=False, vpn_ok="да (VPN — заявленный сценарий)",
        note="дорого, но это единственный оффер, который сам продаётся под ноды",
    ),
    ServerOffer(
        "buyvm-lu", "BuyVM (Frantech)", "Люксембург", 7.0, "USD", 1, 2, 40, 1000,
        traffic="настоящий unmetered, без fair use",
        pays_ru_card=False, pays_crypto=True, vpn_ok="да (AUP: Tor по тикету, VPN не запрещён)",
        note="скорость порта на странице слайсов не указана; AS 53667 в скане 10.02.2026",
    ),
    ServerOffer(
        "skrime-nl", "Skrime", "Эйгельсховен, NL", 5.59, "EUR", 2, 4, 20, 10000,
        traffic="unlimited (shared 10 Гбит/с)", pays_ru_card=False, pays_crypto=True,
        vpn_ok="да (VPN — целевой сценарий)", note="самый дешёвый гигабит, но shared-порт",
    ),
    ServerOffer(
        "netcup-panel", "netcup", "Вена, AT", 8.26, "EUR", 2, 4, 64, 2500,
        traffic="flatrate", pays_ru_card=False, pays_crypto=False, vpn_ok="не сказано",
        note="панели хватает с запасом, порт 2,5 Гбит/с",
    ),
    ServerOffer(
        "melbicom-sto", "Melbicom", "Стокгольм", 11.4, "EUR", 2, 4, 40, 1000,
        traffic="после 5 ТБ/мес — троттлинг (скорость после среза не опубликована)",
        pays_ru_card=True, pays_crypto=True, vpn_ok="лояльно",
        note="порог 5 ТБ/мес достижим при 50+ клиентах",
    ),
    ServerOffer(
        "alexhost-md", "AlexHost", "Кишинёв, MD", 6.0, "EUR", 1, 2, 10, 100,
        traffic="FUP: безлимит не заявлен, есть пункт о «чрезмерной нагрузке на канал»",
        pays_ru_card=False, pays_crypto=True,
        vpn_ok="не сказано; TOR exit запрещён", note="базовый порт 100 Мбит/с: под ноду мало",
    ),
    ServerOffer(
        "alexhost-nl", "AlexHost", "Амстердам / Стокгольм", 10.0, "EUR", 2, 4, 40, 1000,
        traffic="FUP: безлимит не заявлен", pays_ru_card=False, pays_crypto=True,
        vpn_ok="не сказано; TOR exit запрещён", note="это линейка U2: здесь порт действительно 1 Гбит/с",
    ),
    ServerOffer(
        "ava-md", "Ava.Hosting", "Кишинёв, MD", 5.0, "EUR", 1, 2, 25, 1000,
        traffic="заявлено ∞ Bandwidth", pays_ru_card=False, pays_crypto=True,
        vpn_ok="не сказано; запрещены open proxies", note="1 Гбит/с и ∞ за €5 — но AUP против открытых прокси",
    ),
    ServerOffer(
        "netcup-nl", "netcup", "Нюрнберг, DE", 7.03, "EUR", 2, 4, 64, 2500,
        traffic="flatrate: троттлинг до 200 Мбит/с только если средние 2 ТБ/сутки",
        pays_ru_card=False, pays_crypto=False, vpn_ok="не сказано",
        note="лучшая цена за ядра, но оплата картой другой юрисдикции",
    ),
    ServerOffer(
        "ovh-vps2", "OVHcloud", "Германия/Польша/Франция", 8.5, "USD", 4, 8, 75, 1000,
        traffic="unlimited в EU, Anti-DDoS включён", pays_ru_card=False, pays_crypto=False,
        vpn_ok="VPN назван use case, обязателен KYC", note="до 16 доп. IP — лучший запас на ротацию",
    ),
    ServerOffer(
        "servarica-ca", "Servarica", "Монреаль, CA", 15.0, "USD", 2, 8, 250, 1000,
        traffic="настоящий unmetered (опция unlimited)", pays_ru_card=False, pays_crypto=False,
        vpn_ok="personal/company VPN разрешён, public proxy — нет",
        note="+100 мс из РФ: только как «глубокий» резерв",
    ),
    # --- Россия: карта РФ и низкий пинг, но юрисдикция и риск блокировки
    ServerOffer(
        "zetservers-de", "ZetServers", "Франкфурт", 29.0, "EUR", 1, 4, 50, 25000,
        traffic="unmetered: 25 Гбит/с, без троттлинга",
        pays_ru_card=False, pays_crypto=False, vpn_ok="да (VPN — заявленный сценарий)",
        note="та же линейка, что и Бухарест: 8 стран Европы",
    ),
    ServerOffer(
        "alexhost-nl-vpn", "AlexHost", "Амстердам / Стокгольм", 10.0, "EUR", 2, 4, 40, 1000,
        traffic="FUP: безлимит не заявлен", pays_ru_card=False, pays_crypto=True,
        vpn_ok="не сказано; TOR exit запрещён", note="линейка U2: 1 Гбит/с, 2 vCPU",
    ),
    ServerOffer(
        "aeza-hel", "Aeza", "Хельсинки", 5.93, "EUR", 1, 2, 30, 1000,
        traffic="∞ (оферта п.10.7: ограничение при аномальной нагрузке)",
        pays_ru_card=True, pays_crypto=True, vpn_ok="нет на РФ-IP, зарубеж — да",
        note="только как эксперимент: с 03.12.2025 сворачивает VPN-услуги",
    ),
    ServerOffer(
        "buyvm-us", "BuyVM (Frantech)", "Нью-Йорк", 7.0, "USD", 1, 2, 40, 1000,
        traffic="unmetered", pays_ru_card=False, pays_crypto=True,
        vpn_ok="да (AUP: Tor по тикету)", note="США: для стриминга и как второй континент",
    ),
    ServerOffer(
        "hetzner-us", "Hetzner", "Ашберн, US", 7.13, "EUR", 2, 4, 40, 1000,
        traffic="20 ТБ egress включено, далее платно", pays_ru_card=False, pays_crypto=False,
        vpn_ok="не сказано", note="дешёвый US-вход, но метрированный трафик",
    ),
    ServerOffer(
        "servarica-ca2", "Servarica", "Торонто", 15.0, "USD", 2, 8, 250, 1000,
        traffic="настоящий unmetered", pays_ru_card=False, pays_crypto=False,
        vpn_ok="personal/company VPN разрешён, public proxy — нет", note="Канада: +100 мс из РФ",
    ),
    ServerOffer(
        "xorek-de", "xorek.cloud", "Германия", 459.0, "RUB", 1, 2, 30, 1000,
        traffic="∞ на тарифе DE-E-2; в оферте право снизить до 0,1–10 Мбит/с",
        pays_ru_card=True, pays_crypto=False, vpn_ok="VPN в оферте не назван",
        note="наш текущий сервер: тикет #77313, лежал сутки",
    ),
    ServerOffer(
        "justhost-ru", "JustHost.ru", "Москва / СПб", 351.0, "RUB", 2, 2, 20, 200,
        traffic="«неограниченный порт» с fair-share (~62 ТБ/мес на 200 Мбит/с)",
        pays_ru_card=True, pays_crypto=False, vpn_ok="да (запрета VPN в оферте нет)",
        in_russia=True, note="РФ-юрисдикция: блокируют по требованию регулятора",
    ),
    ServerOffer(
        "timeweb-ru", "Timeweb Cloud", "Москва / СПб", 900.0, "RUB", 2, 2, 40, 1000,
        traffic="«трафик бесплатный и безлимитный» (лимит в FAQ не оговорён)",
        pays_ru_card=True, pays_crypto=False, vpn_ok="VPN не назван",
        in_russia=True, note="РФ-юрисдикция + обсуждаемый «Антифрод 3.0»",
    ),
    ServerOffer(
        "vdsina-nl", "VDSina", "Амстердам", 285.0, "RUB", 1, 1, 10, 1000,
        traffic="32 ТБ/мес, далее $2.25/ТБ (не безлимит)",
        pays_ru_card=True, pays_crypto=False, vpn_ok="есть шаблоны VPN-ПО",
        note="карта РФ + EU-локация: редкая комбинация",
    ),
)

#: Быстрый доступ по коду.
OFFERS_BY_KEY: dict[str, ServerOffer] = {offer.key: offer for offer in OFFERS}


# ---------------------------------------------------------------- ёмкость ноды

def node_capacity_users(
    offer: ServerOffer,
    *,
    concurrency: float = DEFAULT_CONCURRENCY,
    mbps_per_session: float = DEFAULT_MBPS_PER_SESSION,
    utilization: float = DEFAULT_PEAK_UTILIZATION,
) -> int:
    """Сколько подписчиков тянет одна нода в вечерний пик.

    Считаем от канала, а не от числа аккаунтов: VPN продаёт мегабиты в час пик.
    Живая пропускная способность = порт, но не больше, чем переваривает CPU
    (`throughput_mbps`). Дальше делим её на «сколько мегабит нужно тем, кто онлайн».
    """
    if mbps_per_session <= 0:
        raise ValueError("скорость сессии должна быть больше нуля")
    if not 0 < concurrency <= 1:
        raise ValueError("доля онлайна должна быть в диапазоне (0, 1]")
    if not 0 < utilization <= 1:
        raise ValueError("загрузка канала должна быть в диапазоне (0, 1]")
    if offer.throughput_mbps <= 0:
        raise ValueError(f"у оффера {offer.key!r} нет пропускной способности")
    sessions = offer.throughput_mbps * utilization / mbps_per_session
    by_channel = max(1, int(sessions / concurrency))
    return min(by_channel, MAX_USERS_PER_NODE)


def peak_load(
    offer: ServerOffer,
    users: int,
    *,
    concurrency: float = DEFAULT_CONCURRENCY,
    mbps_per_session: float = DEFAULT_MBPS_PER_SESSION,
    utilization: float = DEFAULT_PEAK_UTILIZATION,
) -> float:
    """Какая доля канала ноды занята при таком числе подписчиков (0…1+).

    Больше 1 — нода не тянет: в час пик начнутся просадки и буферизация.
    """
    if users < 0:
        raise ValueError("клиентов не может быть меньше нуля")
    if mbps_per_session <= 0:
        raise ValueError("скорость сессии должна быть больше нуля")
    if not 0 < concurrency <= 1:
        raise ValueError("доля онлайна должна быть в диапазоне (0, 1]")
    if not 0 < utilization <= 1:
        raise ValueError("загрузка канала должна быть в диапазоне (0, 1]")
    if offer.throughput_mbps <= 0:
        raise ValueError(f"у оффера {offer.key!r} нет пропускной способности")
    return users * concurrency * mbps_per_session / (offer.throughput_mbps * utilization)


def monthly_traffic_tb(
    offer: ServerOffer,
    *,
    utilization: float = DEFAULT_PEAK_UTILIZATION,
    duty_cycle: float = 0.6,
) -> float:
    """Сколько терабайт в месяц прокачивает нода, если канал занят `duty_cycle` суток.

    Нужно, чтобы сверять ёмкость с тарифами, где «unlimited» кончается на 5 ТБ
    (Melbicom) или где троттлинг включается на 2 ТБ в сутки (netcup).
    """
    if not 0 < utilization <= 1:
        raise ValueError("загрузка канала должна быть в диапазоне (0, 1]")
    if not 0 < duty_cycle <= 1:
        raise ValueError("загрузка суток должна быть в диапазоне (0, 1]")
    if offer.throughput_mbps <= 0:
        raise ValueError(f"у оффера {offer.key!r} нет пропускной способности")
    mbps = offer.throughput_mbps * utilization * duty_cycle
    return mbps * 86400 * 30 / 8 / 1e6


def days_to_traffic_cap(
    offer: ServerOffer,
    cap_tb: float,
    *,
    utilization: float = DEFAULT_PEAK_UTILIZATION,
    duty_cycle: float = 0.6,
) -> float:
    """За сколько суток нода сожжёт месячный лимит трафика `cap_tb`.

    Это ответ на вопрос «а точно ли безлимит»: у Melbicom порог 5 ТБ/мес при
    живой ноде сгорает меньше чем за сутки, у Skrime порога нет вовсе.
    """
    if cap_tb <= 0:
        raise ValueError("лимит трафика должен быть больше нуля")
    per_day = monthly_traffic_tb(offer, utilization=utilization, duty_cycle=duty_cycle) / 30
    if per_day <= 0:  # pragma: no cover - защита от будущих правок
        raise ValueError("нода не прокачивает трафик: делить не на что")
    return cap_tb / per_day


# ---------------------------------------------------------------- фонд замены

#: Через сколько месяцев ноду приходится пересоздавать: адрес или подсеть
#: попадают под фильтр, либо хостер просит свернуть услугу. Оценка по волнам
#: 2026 года (391 AS под ограничением с 10.02.2026, 47 AS под динамической
#: блокировкой с 26.02.2026, волна после выборов в конце сентября 2026).
#:
#: Это не «плохие хостеры»: в списки попадают все, вопрос только в сроке.
NODE_REPLACEMENT_MONTHS: dict[str, float] = {
    "aeza": 6.0,  # по требованиям регулятора сворачивает VPN-услуги: в схемах не используем
    "skrime": 4.0,  # молодой AS, попадание в списки не проверено ни в одну сторону
    "melbicom": 3.0,  # AS 8849/56630 — в скане 10.02.2026 под полным ограничением
    "ovh": 3.0,  # AS 16276 — в том же скане
    "netcup": 3.0,  # AS 197540 — в списке динамической блокировки 26.02.2026
    "alexhost": 3.0,  # AS 200019 — там же
    "ava": 3.0,  # AS 48753 — там же
    "zetservers": 6.0,  # AS 25198 в списках не встречался, но и данных мало
    "justhost": 3.0,  # AS 26383 — там же, плюс юрисдикция РФ
    "timeweb": 4.0,
    "vdsina": 3.0,  # AS 216071 — там же
    "buyvm": 3.0,  # AS 53667 — в скане 10.02.2026 под полным ограничением
    "netcup": 3.0,  # AS 197540 — в списке динамической блокировки 26.02.2026
    "xorek": 3.0,  # наш текущий сервер: уже лежал сутки (тикет #77313)
    "servarica": 6.0,
    "hetzner": 3.0,  # AS 24940 — в скане 10.02.2026 под полным ограничением
}

#: По умолчанию: если про хостера ничего не известно, считаем как средний случай.
DEFAULT_REPLACEMENT_MONTHS: float = 4.0


def replacement_months(offer: ServerOffer) -> float:
    """Сколько месяцев в среднем нода живёт до замены адреса/сервера."""
    return NODE_REPLACEMENT_MONTHS.get(offer.key.split("-")[0], DEFAULT_REPLACEMENT_MONTHS)


def replacement_cost_rub(
    offer: ServerOffer,
    *,
    months: float | None = None,
) -> float:
    """Сколько в месяц стоит фонд замены одной ноды.

    Если нода живёт 2 месяца, её цена в месяц — это не 531 ₽, а 531 ₽ + 265 ₽
    на замену. Именно поэтому «цена сервера» — не главная метрика при выборе.
    """
    period = replacement_months(offer) if months is None else months
    if period <= 0:
        raise ValueError("срок жизни ноды должен быть больше нуля")
    return offer.price_rub / period


def nodes_for_users(
    users: int,
    offer: ServerOffer,
    *,
    min_nodes: int = 2,
    reserve_nodes: int = 0,
    **kwargs: float,
) -> int:
    """Сколько нод держать под такое число клиентов.

    :param min_nodes: минимум нод. `2` — «одна нода не должна быть единственной»,
        `0` — «считаем только дополнительную ёмкость» (нужно при масштабировании
        схемы, где часть нод уже куплена).
    :param reserve_nodes: запас «на выживание»: +1 нода к расчётной нагрузке,
        чтобы после падения одной ноды остальные не упирались в канал. Это самая
        дорогая часть схемы, поэтому в расчётах роста запас по умолчанию выключен:
        часовую просадку скорости клиенты переживают, недоступность сервиса — нет.
    """
    if min_nodes < 1:
        raise ValueError("минимум нод должен быть не меньше одной")
    per_node = node_capacity_users(offer, **kwargs)
    by_load = -(-max(users, 0) // per_node) if users > 0 else 0
    return max(min_nodes, by_load + reserve_nodes)


# ---------------------------------------------------------------- мультигео

#: Каталог «одна страна = один сервер»: что можно открыть и за сколько.
#: В списке ровно один оффер на страну — самый дешёвый из проверенных, чтобы
#: «добавить страну» стоило примерно одинаково и не тянуло премиум-цену.
#: `priority` — волна запуска: 1 = Запад (70–80 % аудитории), 2 = Юг и Урал,
#: 3 = второй континент и Восток, 4 = только под заказ.
COUNTRY_CATALOG: tuple[tuple[str, str, int], ...] = (
    ("🇲🇩 Молдова", "ava-md", 1),
    ("🇳🇱 Нидерланды", "skrime-nl", 1),
    ("🇫🇮 Финляндия", "aeza-hel", 1),
    ("🇩🇪 Германия", "netcup-nl", 1),
    ("🇦🇹 Австрия", "netcup-panel", 2),
    ("🇱🇺 Люксембург", "buyvm-lu", 2),
    ("🇸🇪 Швеция", "alexhost-nl-vpn", 2),
    ("🇺🇸 США", "buyvm-us", 3),
    ("🇨🇦 Канада", "servarica-ca2", 3),
    ("🇷🇴 Румыния (премиум, 25 Гбит/с)", "zetservers-ro", 4),
    ("🇷🇺 Россия (входной relay)", "justhost-ru", 4),
)

@dataclass(frozen=True, slots=True)
class CountryOption:
    """Страна, которую можно открыть: сколько стоит и что за это даёт."""

    country: str
    offer: ServerOffer
    priority: int

    @property
    def price_rub(self) -> float:
        return self.offer.price_rub

    @property
    def full_rub(self) -> float:
        """Цена с фондом замены: столько страна стоит в месяц на самом деле."""
        return self.offer.price_rub + replacement_cost_rub(self.offer)

    @property
    def capacity_users(self) -> int:
        return node_capacity_users(self.offer)

    @property
    def traffic_tb(self) -> float:
        return monthly_traffic_tb(self.offer)


#: Страны по волнам: 1 — Запад, 2 — Юг/Урал, 3 — Восток, 4 — экзотика.
COUNTRY_WAVES: tuple[tuple[str, str, str], ...] = (
    ("Запад", "Молдова, Нидерланды, Финляндия, Германия",
     "Калининград, СПб, Москва, Казань, Поволжье — 70–80 % платящих: **запускать сразу**"),
    ("Юг и Урал", "Австрия, Люксембург, Швеция",
     "Урал, Западная Сибирь, юг России: подключать при 30+ клиентах"),
    ("Второй континент", "США, Канада",
     "Стриминг, сервисы, которые не пускают европейские адреса: при 50+ клиентах. "
     "Пинг из РФ выше, зато другая юрисдикция и другие списки блокировок"),
    ("Восток и экзотика", "Токио, Сеул, Гонконг, Алматы, Ереван, Стамбул",
     "Дальний Восток и юг: провайдеров в модели нет — нужен отдельный поиск и проверка, "
     "данных за 2026 мало"),
)


def countries(priority: int | None = None) -> list[CountryOption]:
    """Страны каталога: все или только определённой волны."""
    result = [
        CountryOption(country, OFFERS_BY_KEY[key], wave)
        for country, key, wave in COUNTRY_CATALOG
        if key in OFFERS_BY_KEY
    ]
    if priority is not None:
        result = [option for option in result if option.priority == priority]
    return result


def multi_country_plan(
    count: int,
    *,
    panel: ServerOffer | None = None,
    min_countries: int = 1,
) -> VpsPlan:
    """Схема «одна страна = один сервер»: N стран плюс отдельная панель.

    Панель выносим отдельно: при 5+ странах она тем более не должна жить на одной
    из нод — иначе потеря этой ноды отнимает и управление всеми остальными.
    """
    if count < min_countries:
        raise ValueError(f"нужно минимум {min_countries} стран")
    options = countries()
    if count > len(options):
        raise ValueError(f"в каталоге только {len(options)} стран")
    # берём страны по волнам: сначала Запад, потом Юг, потом остальное
    ordered = sorted(options, key=lambda option: (option.priority, option.full_rub))
    chosen = ordered[:count]
    return VpsPlan(
        key=f"geo{count}",
        title=f"Мультигео: {count} стран, панель отдельно",
        panel=panel or PANEL_OFFER,
        nodes=tuple(option.offer for option in chosen),
        reserve_percent=8.0,
        purpose=f"Список из {count} локаций в одной ссылке: клиент выбирает страну тапом",
        min_users=0,
        min_nodes=count,
        notes=(
            "каждая страна — отдельный сервер: одного сервера на две страны не бывает",
            "панель отдельно от нод: она управляет всеми странами",
            "страны в разных ASN, значит блокировка одной не отключает сервис",
        ),
    )


def countries_for_budget(budget_rub: float, **kwargs: float) -> int:
    """Сколько стран помещается в месячный бюджет (включая панель и фонд замены)."""
    if budget_rub <= 0:
        raise ValueError("бюджет должен быть больше нуля")
    for count in range(len(countries()), 0, -1):
        if multi_country_plan(count, **kwargs).monthly_with_replacement_rub <= budget_rub:
            return count
    return 0


def country_break_even(count: int, *, net_per_user: float = DEFAULT_NET_PER_USER,
                       **kwargs: float) -> float:
    """Сколько клиентов окупают N стран."""
    return breakeven_users(multi_country_plan(count, **kwargs), net_per_user=net_per_user)


def next_country_price(count: int) -> float:
    """Сколько добавляет N+1-я страна в месяц (с фондом замены).

    Это ставка «плюс ещё одна локация»: примерно 650–800 ₽, если брать страны
    по каталогу, а не премиум-ноды.
    """
    current = multi_country_plan(count).monthly_with_replacement_rub
    nxt = multi_country_plan(count + 1).monthly_with_replacement_rub
    return nxt - current


# ---------------------------------------------------------------- сценарии

@dataclass(frozen=True, slots=True)
class VpsPlan:
    """Схема развёртывания: панель + ноды + резерв.

    :param reserve_percent: доля сверху на разовые мелочи (тестовые IP,
        доплаты, запас на «второй переезд в один месяц»). **Замена нод сюда не
        входит** — она считается отдельно (`replacement_rub`), чтобы не удваивать
        один и тот же расход.
    """

    key: str
    title: str
    panel: ServerOffer
    nodes: tuple[ServerOffer, ...]
    reserve_percent: float = 8.0
    purpose: str = ""
    min_users: int = 0
    min_nodes: int = 2
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Проверки на входе: отрицательный резерв или отрицательная ёмкость — ошибка."""
        if not 0 <= self.reserve_percent <= 100:
            raise ValueError("резерв должен быть в диапазоне [0, 100] %")
        # Панель тоже сервер: если она стоит на ноде, её проверяем как ноду.
        servers = self.nodes or (self.panel,)
        if any(server.throughput_mbps <= 0 for server in servers):
            raise ValueError("у сервера нет пропускной способности — схема бессмысленна")

    @property
    def panel_rub(self) -> float:
        return self.panel.price_rub

    @property
    def nodes_rub(self) -> float:
        return sum(node.price_rub for node in self.nodes)

    @property
    def fixed_rub(self) -> float:
        """Постоянные расходы в месяц без запаса."""
        return self.panel_rub + self.nodes_rub

    @property
    def reserve_rub(self) -> float:
        """Резерв на разовые мелочи: тестовые IP, доплаты, второй переезд."""
        return self.fixed_rub * self.reserve_percent / 100

    @property
    def monthly_rub(self) -> float:
        """Что списывается в месяц по счетам: панель, ноды и небольшой резерв."""
        return self.fixed_rub + self.reserve_rub

    @property
    def yearly_rub(self) -> float:
        return self.monthly_rub * 12

    @property
    def replacement_rub(self) -> float:
        """Фонд замены нод: сколько в месяц откладывать на пересоздание адресов.

        Считается по сроку жизни каждой ноды (`NODE_REPLACEMENT_MONTHS`):
        хостер, чей AS уже в списках, меняется чаще и стоит в месяц дороже,
        даже если его прайс ниже.
        """
        servers = self.nodes or (self.panel,)
        return sum(replacement_cost_rub(server) for server in servers)

    @property
    def monthly_with_replacement_rub(self) -> float:
        """Полная стоимость владения: счета + фонд замены нод.

        Именно эта цифра — «сколько схема стоит на самом деле»: ноды придётся
        пересоздавать, и деньги на это нужно откладывать каждый месяц, а не
        искать в момент блокировки.
        """
        return self.monthly_rub + self.replacement_rub

    @property
    def capacity_users(self) -> int:
        """Ёмкость всех нод под обычный профиль (онлайн 25 %, 8 Мбит/с на сессию).

        Для схемы «один сервер» нод в списке нет — там сервер и панель это одно
        и то же, поэтому ёмкость считаем по самому серверу.
        """
        return self.capacity()

    def capacity(self, **kwargs: float) -> int:
        """Ёмкость всех нод под заданный профиль нагрузки.

        Профиль важен: те же три ноды под торренты держат вчетверо меньше
        клиентов, чем под обычный вечерний просмотр.
        """
        servers = self.nodes or (self.panel,)
        return sum(node_capacity_users(server, **kwargs) for server in servers)

    def capacity_after_one_loss(self, **kwargs: float) -> int:
        """Ёмкость после потери САМОЙ КРУПНОЙ ноды под заданный профиль: то,
        что осталось бы у клиентов, если бы её выключили и все переехали."""
        if len(self.nodes) < 2:
            return 0
        return max(0, self.capacity(**kwargs) - max(node_capacity_users(n, **kwargs) for n in self.nodes))

    @property
    def capacity_users_after_one_loss(self) -> int:
        """Ёмкость, если выпала САМАЯ КРУПНАЯ нода и её клиенты переехали на остальные.

        Это консервативная оценка: считаем, что теряем лучшую ноду, а не худшую.
        Иначе метрика могла превысить полную ёмкость схемы — например, набор
        «337 + 168 + 168» давал «после потери» 674 при полной ёмкости 673.
        Для схемы «один сервер» это ноль: переезжать некуда.
        """
        return self.capacity_after_one_loss()


#: Схемы собраны из офферов, проверенных 06.10.2026. Панель везде одна и та же
#: (netcup 2 vCPU / 4 ГБ ≈ 785 ₽): дешевле — уже ниже рекомендованных 4 ГБ,
#: дороже — не нужно, панель не проксирует клиентский трафик.
PANEL_OFFER: ServerOffer = OFFERS_BY_KEY["netcup-panel"]

SCENARIOS: tuple[VpsPlan, ...] = (
    VpsPlan(
        key="now",
        title="Сейчас: 1 сервер (xorek DE) + своя панель на нём",
        panel=OFFERS_BY_KEY["xorek-de"],
        nodes=(),
        reserve_percent=8.0,
        purpose="Первый рубль, 1–30 клиентов. Одна точка отказа — принимаем осознанно.",
        min_users=0,
        min_nodes=1,
        notes=("3x-ui как панель, бот выдаёт ключи с этого же сервера",),
    ),
    VpsPlan(
        key="B",
        title="B. База: панель EU + 2 ноды в разных ASN (NL + MD)",
        panel=PANEL_OFFER,
        nodes=(OFFERS_BY_KEY["skrime-nl"], OFFERS_BY_KEY["ava-md"]),
        reserve_percent=8.0,
        purpose="Рабочая связка для 30–150 клиентов: две страны, две юрисдикции, дешёвый вход.",
        min_users=25,
        notes=(
            "Skrime NL — основной вход, Ava.Hosting MD — второй ASN и вторая юрисдикция",
            "Ava: €5 за 1 vCPU / 2 ГБ / 1 Гбит/с и ∞ — но AUP запрещает открытые прокси: проверить до покупки",
            "альтернатива второму входу: AlexHost U2 (€10, 2 vCPU / 1 Гбит/с) — дороже, зато тот же AUP-профиль",
            "если нужен именно молдавский вход с максимумом гарантий — Melbicom (€11.40, но лимит 5 ТБ/мес)",
        ),
    ),
    VpsPlan(
        key="A",
        title="A. Качество: панель EU + 3 ноды (RO + NL + LU)",
        panel=PANEL_OFFER,
        nodes=(
            OFFERS_BY_KEY["netcup-nl"],
            OFFERS_BY_KEY["skrime-nl"],
            OFFERS_BY_KEY["buyvm-lu"],
        ),
        reserve_percent=8.0,
        purpose="Когда клиентов 100+ и нужен запас канала: три страны, три ASN, разные юрисдикции.",
        min_users=100,
        notes=(
            "netcup 4 vCPU / 2,5 Гбит/с — основной объём, Skrime — дешёвый гигабит, BuyVM — unmetered-резерв",
            "порог netcup «2 ТБ за сутки» для ноды недостижим: это ~60 ТБ/мес",
            "премиум-вариант вместо netcup: ZetServers за €29/мес (25 Гбит/с, гарантированный 1 Гбит/с)",
        ),
    ),
    VpsPlan(
        key="C",
        title="C. Минимальные деньги: панель EU + 2 ноды (NL + LU)",
        panel=PANEL_OFFER,
        nodes=(OFFERS_BY_KEY["skrime-nl"], OFFERS_BY_KEY["buyvm-lu"]),
        reserve_percent=8.0,
        purpose="Самый дешёвый вариант с настоящим безлимитом: меньше всего денег в месяц.",
        min_users=25,
        notes=(
            "BuyVM от $7/мес (1 vCPU / 2 ГБ): слабее по CPU, зато unmetered и крипта",
            "обе ноды — «молодые» AS: замены будут чаще, фонд замены это учитывает",
        ),
    ),
    VpsPlan(
        key="D",
        title="D. Тяжёлый трафик: панель EU + премиум-нода + резерв",
        panel=PANEL_OFFER,
        nodes=(OFFERS_BY_KEY["zetservers-ro"], OFFERS_BY_KEY["skrime-nl"]),
        reserve_percent=8.0,
        purpose="Клиенты качают и смотрят 4K: канал не упирается ни в лимит, ни в CPU.",
        min_users=150,
        notes=(
            "ZetServers 1 vCPU / 4 ГБ / 25 Гбит/с — под 4K и торренты без троттлинга",
            "Skrime — второй вход, чтобы блокировка одной ноды не отключала всех",
        ),
    ),
)

SCENARIOS_BY_KEY: dict[str, VpsPlan] = {plan.key: plan for plan in SCENARIOS}

#: Схема, которая у нас работает сейчас: точка отсчёта для «сколько добавит связка».
BASELINE_KEY: str = "now"


def switching_delta(plan: VpsPlan, *, baseline: VpsPlan | None = None) -> float:
    """Насколько дороже текущей схемы обходится эта (полная стоимость, ₽/мес).

    Нужно, чтобы отвечать на главный вопрос «сколько это добавит» без калькулятора:
    связка из двух нод — это +1 600 ₽/мес, а не «в два раза дороже».
    """
    base = baseline or SCENARIOS_BY_KEY[BASELINE_KEY]
    return plan.monthly_with_replacement_rub - base.monthly_with_replacement_rub


def switching_payback_users(plan: VpsPlan, *, net_per_user: float = DEFAULT_NET_PER_USER,
                            baseline: VpsPlan | None = None) -> float:
    """Сколько платящих клиентов нужно, чтобы связка просто не была убыточной."""
    return breakeven_users(plan, net_per_user=net_per_user)


#: Лучший оффер «под ноду» по версии документа: цена мегабита + ресурсы.
#: Aeza сюда не берём: с 03.12.2025 она блокирует услуги за VPN по уведомлению
#: регулятора — включая серверы с Xray в «чистых» сетях.
DEFAULT_NODE_KEY: str = "skrime-nl"

#: Шкала, по которой считаем «сколько стоит сервис при N клиентах».
DEFAULT_SCALE: tuple[int, ...] = (10, 30, 50, 100, 300, 550)


@dataclass(frozen=True, slots=True)
class ScaleRow:
    """Строка таблицы «сколько стоит сервис при таком числе клиентов»."""

    users: int
    providers: int
    nodes: int
    monthly_rub: float
    per_user_rub: float
    breakeven_users: float
    margin_percent: float
    fits: bool
    fits_after_one_loss: bool

    @property
    def yearly_rub(self) -> float:
        return self.monthly_rub * 12


def free_panel(offer: ServerOffer) -> ServerOffer:
    """Панель, которая физически стоит на одной из нод: отдельной строкой не платится.

    Нужна, чтобы сервер не попал в счёт дважды (как нода и как панель), но при
    этом в схеме было видно, где живёт управление.
    """
    return ServerOffer(
        key=f"{offer.key}-panel", provider=offer.provider, location=offer.location,
        price=0.0, currency=offer.currency, vcpu=offer.vcpu, ram_gb=offer.ram_gb,
        disk_gb=offer.disk_gb, port_mbps=offer.port_mbps,
        traffic="панель на этом же сервере", pays_ru_card=offer.pays_ru_card,
        pays_crypto=offer.pays_crypto, vpn_ok=offer.vpn_ok, in_russia=offer.in_russia,
        note="счёт за сервер уже учтён в нодах",
    )


def scale_plan(plan: VpsPlan, users: int, **kwargs: float) -> VpsPlan:
    """Развернуть схему под число клиентов: добавить нод столько, сколько нужно.

    Правило масштабирования простое и честное по деньгам: **уже купленные ноды
    остаются как есть** (они спроектированы под задачу — премиум-нода остаётся
    премиум-нодой), а дополнительная ёмкость докупается самым дешёвым способом
    из каталога. Иначе таблица показывала бы, что на 10 клиентах нужно три
    дорогих сервера, а на 550 — те же три.

    Для схемы «один сервер» (нод нет) нодой считается сам сервер: на 550
    клиентах рядом появится второй, даже если сейчас всё живёт на одном.
    """
    if users <= 0:
        return plan
    # Схема «один сервер»: сервер одновременно панель и нода. Он уже посчитан
    # как нода, поэтому отдельной строкой за панель платить нельзя — ни до, ни
    # после докупки второй ноды (иначе один и тот же сервер идёт в счёте дважды).
    panel_is_node = not plan.nodes
    existing = [plan.panel] if panel_is_node else list(plan.nodes)
    have = sum(node_capacity_users(node, **kwargs) for node in existing)
    need = users - have
    extra: list[ServerOffer] = []
    if need > 0:
        purchase = plan_node_purchase(need, min_nodes=0, **kwargs)
        extra = [NODE_CLASSES[key] for key, count in purchase.counts.items() for _ in range(count)]
    nodes = tuple(existing + extra)
    panel = free_panel(plan.panel) if panel_is_node else plan.panel
    return VpsPlan(
        key=plan.key,
        title=plan.title,
        panel=panel,
        nodes=nodes,
        reserve_percent=plan.reserve_percent,
        purpose=plan.purpose,
        min_users=plan.min_users,
        min_nodes=plan.min_nodes,
        notes=plan.notes,
    )


def scale_row(
    plan: VpsPlan,
    users: int,
    *,
    net_per_user: float = DEFAULT_NET_PER_USER,
    channel_fee_percent: float = DEFAULT_CHANNEL_FEE_PERCENT,
    **kwargs: float,
) -> ScaleRow:
    """Посчитать расходы, себестоимость клиента и запас прочности при N клиентах.

    Две разные проверки «хватает ли нод»:
      * `fits` — хватает канала всем клиентам;
      * `fits_after_one_loss` — хватает ли канала, если одна нода выпала и её
        клиенты перешли на остальные. Второе дороже: это и есть цена «без просадки».
    """
    if users <= 0:
        raise ValueError("клиентов должно быть больше нуля")
    scaled = scale_plan(plan, users, **kwargs)
    monthly = scaled.monthly_with_replacement_rub
    revenue = users * net_per_user
    capacity_after_loss = scaled.capacity_after_one_loss(**kwargs)
    return ScaleRow(
        users=users,
        providers=len({offer.provider for offer in scaled.nodes}) + 1,
        nodes=len(scaled.nodes),
        monthly_rub=monthly,
        per_user_rub=cost_per_user_month(
            scaled, users, channel_fee_percent=channel_fee_percent,
            average_check_rub=DEFAULT_AVERAGE_CHECK_RUB
        ),
        breakeven_users=breakeven_users(scaled, net_per_user=net_per_user),
        margin_percent=(revenue - monthly) / revenue * 100 if revenue else 0.0,
        fits=users <= scaled.capacity(**kwargs),
        # «Хватает после аварии» — только если после потери крупнейшей ноды
        # действительно остаётся ёмкость: у схемы «один сервер» её нет вовсе.
        fits_after_one_loss=capacity_after_loss > 0 and users <= capacity_after_loss,
    )


def scale_table(
    plan: VpsPlan,
    users_scale: tuple[int, ...] = DEFAULT_SCALE,
    **kwargs: float,
) -> list[ScaleRow]:
    """Таблица по шкале клиентов — основа раздела «затраты в месяц»."""
    return [scale_row(plan, users, **kwargs) for users in users_scale]


# ---------------------------------------------------------------- закупка нод

#: Каталог конфигураций «под ноду»: чем дороже сервер, тем дешевле мегабит.
#: Цены — из `OFFERS`, здесь они превращены в удобные варианты закупки.
NODE_CLASSES: dict[str, ServerOffer] = {
    "s": OFFERS_BY_KEY["buyvm-lu"],  # 1 vCPU / 2 ГБ, unmetered — 450 Мбит/с живых, 595 ₽
    "m": OFFERS_BY_KEY["skrime-nl"],  # 2 vCPU / 4 ГБ — 900 Мбит/с живых, 531 ₽
    "l": OFFERS_BY_KEY["netcup-nl"],  # 2 vCPU / 4 ГБ, порт 2,5 Гбит/с, CPU-потолок 900 Мбит/с
}

#: Только зарубежные безлимитные ноды: меньше точек политического риска.
#: Именно из этого набора собирается «схема A» документа.
NODE_CLASSES_EU: dict[str, ServerOffer] = {key: NODE_CLASSES[key] for key in ("m", "l")}


@dataclass(frozen=True, slots=True)
class NodePurchase:
    """Сколько каких нод держать: ответ на вопрос «что конкретно покупать»."""

    subscribers_per_node: int
    sessions_per_node: int
    counts: dict[str, int]
    cost_rub: float

    @property
    def total_nodes(self) -> int:
        return sum(self.counts.values())

    @property
    def capacity_subscribers(self) -> int:
        return self.total_nodes * self.subscribers_per_node

    @property
    def capacity_sessions(self) -> int:
        return self.total_nodes * self.sessions_per_node

    @property
    def cost_per_subscriber_rub(self) -> float:
        if not self.capacity_subscribers:
            raise ValueError("нет ёмкости — делить не на что")
        return self.cost_rub / self.capacity_subscribers


def max_sessions_per_node(
    offer: ServerOffer,
    *,
    mbps_per_session: float = DEFAULT_MBPS_PER_SESSION,
    utilization: float = DEFAULT_PEAK_UTILIZATION,
) -> int:
    """Сколько одновременных сессий держит нода в пик (без учёта доли онлайна)."""
    if mbps_per_session <= 0:
        raise ValueError("скорость сессии должна быть больше нуля")
    if not 0 < utilization <= 1:
        raise ValueError("загрузка канала должна быть в диапазоне (0, 1]")
    return max(1, int(offer.throughput_mbps * utilization / mbps_per_session))


def plan_node_purchase(
    users: int,
    *,
    classes: dict[str, ServerOffer] | None = None,
    concurrency: float = DEFAULT_CONCURRENCY,
    mbps_per_session: float = DEFAULT_MBPS_PER_SESSION,
    utilization: float = DEFAULT_PEAK_UTILIZATION,
    min_nodes: int = 2,
) -> NodePurchase:
    """Подобрать самый дешёвый набор нод под число клиентов.

    Ограничений два, и оба должны выполняться:
      * одновременные сессии в пик (`users × concurrency`) — это про канал;
      * число подписчиков на ноду — это про RAM/CPU и лимиты самой панели.

    Поэтому «в лоб поделить» нельзя: одна большая нода иногда дешевле двух
    маленьких, и наоборот. Перебираем наборы и берём минимальный по цене.
    """
    if users < 0:
        raise ValueError("клиентов не может быть меньше нуля")
    if not 0 < concurrency <= 1:
        raise ValueError("доля онлайна должна быть в диапазоне (0, 1]")
    if min_nodes < 0:
        raise ValueError("минимум нод не может быть отрицательным")
    catalog = NODE_CLASSES if classes is None else classes
    if not catalog:
        raise ValueError("каталог нод пуст")

    per_node_subs = {
        key: node_capacity_users(
            offer, concurrency=concurrency, mbps_per_session=mbps_per_session, utilization=utilization
        )
        for key, offer in catalog.items()
    }
    per_node_sessions = {
        key: max_sessions_per_node(offer, mbps_per_session=mbps_per_session, utilization=utilization)
        for key, offer in catalog.items()
    }
    sessions_needed = users * concurrency

    # Верхняя граница перебора: сколько нод нужно, если бы все они были «мелкими».
    # Берём именно минимум по каталогу — иначе перебор не увидит дешёвые наборы
    # из нескольких маленьких нод (например, три по 450 Мбит/с вместо одной большой).
    smallest_subs = min(per_node_subs.values())
    smallest_sessions = min(per_node_sessions.values())
    limit = max(
        min_nodes,
        -(-users // smallest_subs) if users else 0,
        -(-int(sessions_needed) // smallest_sessions) if sessions_needed else 0,
    )

    best: tuple[float, dict[str, int]] | None = None
    for counts in _node_combinations(catalog, limit):
        total_nodes = sum(counts.values())
        if total_nodes < min_nodes:
            continue
        subs_capacity = sum(counts[key] * per_node_subs[key] for key in counts)
        sess_capacity = sum(counts[key] * per_node_sessions[key] for key in counts)
        if users and subs_capacity < users:
            continue
        if sessions_needed and sess_capacity < sessions_needed:
            continue
        cost = sum(counts[key] * catalog[key].price_rub for key in counts)
        if best is None or cost < best[0]:
            best = (cost, dict(counts))
    if best is None:  # pragma: no cover - перебор с запасом по limit всегда находит вариант
        raise RuntimeError("не удалось подобрать набор нод")

    cost, counts = best
    total_nodes = sum(counts.values())
    return NodePurchase(
        subscribers_per_node=total_nodes and sum(
            counts[key] * per_node_subs[key] for key in counts
        ) // total_nodes,
        sessions_per_node=total_nodes and sum(
            counts[key] * per_node_sessions[key] for key in counts
        ) // total_nodes,
        counts={key: value for key, value in counts.items() if value},
        cost_rub=cost,
    )


def _node_combinations(catalog: dict[str, ServerOffer], limit: int):
    """Перебрать наборы нод: (s, m, l) с суммой не больше `limit` (мелкие первыми)."""
    keys = list(catalog)
    counts = {key: 0 for key in keys}

    def walk(index: int, remaining: int):
        if index == len(keys):
            yield dict(counts)
            return
        for value in range(remaining + 1):
            counts[keys[index]] = value
            yield from walk(index + 1, remaining - value)
        counts[keys[index]] = 0

    yield from walk(0, limit)


def cost_per_user_month(
    plan: VpsPlan,
    users: int,
    *,
    channel_fee_percent: float = DEFAULT_CHANNEL_FEE_PERCENT,
    average_check_rub: float = DEFAULT_AVERAGE_CHECK_RUB,
) -> float:
    """Себестоимость одного клиента в месяц при таком числе клиентов.

    Включает две части: постоянную (серверы + резерв + фонд замены нод)
    и переменную — комиссию канала оплаты с оборота. Комиссия не «расход на инфраструктуру», но из
    чека уходит именно она, поэтому в честной себестоимости она есть.
    """
    if users <= 0:
        raise ValueError("клиентов должно быть больше нуля")
    fixed_per_user = plan.monthly_with_replacement_rub / users
    channel_per_user = average_check_rub * channel_fee_percent / 100
    return fixed_per_user + channel_per_user


def breakeven_users(
    plan: VpsPlan,
    *,
    net_per_user: float = DEFAULT_NET_PER_USER,
) -> float:
    """Сколько платящих нужно, чтобы схема окупала себя.

    `net_per_user` по умолчанию — средний чек 109,5 ₽ минус 8 % СБП
    (см. `app.services.finmodel`), то есть деньги, которые реально доходят.
    """
    if net_per_user <= 0:
        raise ValueError("выручка с клиента должна быть больше нуля")
    return plan.monthly_with_replacement_rub / net_per_user


# ---------------------------------------------------------------- один раз

@dataclass(frozen=True, slots=True)
class StartupItem:
    """Разовый расход при запуске/переезде."""

    title: str
    rub: float
    required: bool = True
    comment: str = ""


#: Разовые расходы при переходе на связку «панель + ноды».
STARTUP_ITEMS: tuple[StartupItem, ...] = (
    StartupItem("Домен для панели (.ru/.com, 1 год)", 1200, True, "нужен отдельный от бота"),
    StartupItem("Домен для страницы подписки (можно поддомен)", 0, False, "поддомен панельного домена — бесплатно"),
    StartupItem("Первые 2–3 ноды: оплата 3 месяца вперёд", 5400, True, "3 ноды × ~600 ₽ × 3 мес"),
    StartupItem("Резерв на замену IP/переезд ноды", 3000, True, "одна блокировка = один переезд"),
    StartupItem("Тестовые SIM/трафик всех 5 операторов", 1500, False, "проверять только с мобильных сетей"),
)
#: Плюс полный бюджет из ТЗ: 10 000 ₽ разово — он остаётся ориентиром.


def startup_cost(items: tuple[StartupItem, ...] = STARTUP_ITEMS, *, required_only: bool = False) -> float:
    """Сколько нужно на старте схемы."""
    chosen = [item for item in items if item.required or not required_only]
    return sum(item.rub for item in chosen)


# ---------------------------------------------------------------- надёжность

@dataclass(frozen=True, slots=True)
class FailureMode:
    """Режим отказа: вероятность в месяц, длительность и кого он накрывает.

    :param probability: доля месяцев, в которых отказ случается (0.12 = раз в 8 мес).
    :param hours: сколько длится недоступность одного попавшего клиента.
    :param affects: `node` (клиенты одной ноды), `provider` (все ноды хостера),
        `all` (все клиенты сервиса).
    :param share: какую долю сервиса накрывает отказ в схеме с 1 нодой:
        для одной ноды всё, для трёх — примерно треть.
    """

    title: str
    probability: float
    hours: float
    affects: str = "node"
    share: float = 1.0
    comment: str = ""

    @property
    def availability(self) -> float:
        """Ожидаемая доступность сервиса при таком режиме (без учёта `share`)."""
        hours_in_month = 24 * 30
        return 1 - self.probability * self.hours / hours_in_month


#: Эстимейт, а не данные провайдера. Источники: SLA хостеров, трекеры блокировок
#: (ntc.rkn.quest) и наш собственный кейс xorek от 06.10.2026. Калибруется по факту:
#: после 3–6 месяцев работы цифры надо заменить на фактические.
DEFAULT_FAILURES: tuple[FailureMode, ...] = (
    FailureMode("Авария ноды у хостера (перезагрузка, диск, сеть)", 0.20, 2.0, "node",
                "SLA 99,9 % — это ~43 минуты в месяц на сервер"),
    FailureMode("Авария всей локации/аккаунта (выключили стойку или аккаунт)", 0.05, 8.0, "provider",
                "кейс xorek 06.10.2026: сервер лежал сутки, поддержка молчала"),
    FailureMode("Фильтрация подсети/адреса: «соединение есть, скорости нет»", 0.12, 6.0, "node",
                "профиль 16–20 КБ, заморозка TLS-сессий — лечится сменой адреса"),
    FailureMode("Блокировка РФ-ноды по требованию регулятора", 0.05, 12.0, "provider",
                "применимо, только если в схеме есть российская нода"),
)


#: Сколько инцидентов в месяц случается «где-то в пуле»: отказ ноды, авария
#: локации, фильтрация подсети. Клиент сидит на одной ноде, поэтому его личный
#: простой тем меньше, чем больше нод в пуле — но не делится ровно на их число:
#: крупный инцидент может задеть несколько нод сразу.
POOL_INCIDENTS_PER_MONTH: float = 1.5


def scope_share(affects: str, nodes: int, *, pool_incidents: float = POOL_INCIDENTS_PER_MONTH) -> float:
    """Какую долю клиентов накрывает отказ такого типа.

    Один сервер — весь сервис. Чем больше нод, тем меньше шанс, что инцидент
    месяца попал именно в твою ноду. Считаем как «доля месяцев, в которые
    клиента задело»: `pool_incidents / nodes`, но не больше единицы.
    Именно поэтому доступность растёт от числа независимых входов.
    """
    if affects == "all" or nodes <= 1:
        return 1.0
    if affects in ("node", "provider"):
        return min(1.0, pool_incidents / nodes)
    raise ValueError(f"неизвестный охват отказа: {affects!r}")


def failure_minutes(mode: FailureMode, nodes: int) -> float:
    """Ожидаемый простой на одного клиента в месяц от такого режима отказа, минут."""
    return mode.probability * mode.hours * 60 * scope_share(mode.affects, nodes)


def expected_downtime_minutes(
    failures: tuple[FailureMode, ...] = DEFAULT_FAILURES,
    *,
    nodes: int = 0,
    has_ru_node: bool = False,
) -> float:
    """Сколько минут в месяц в среднем не может подключиться один клиент.

    Считаем суммой ожидаемых простоев по режимам отказа, а не «1 − произведение
    доступностей»: с точки зрения клиента важно, накрывает отказ весь сервис или
    только одну ноду из трёх. Режим блокировки РФ-ноды учитывается только если
    такая нода в схеме есть (`has_ru_node`).
    """
    total = 0.0
    for mode in failures:
        if "регулятора" in mode.title and not has_ru_node:
            continue
        total += failure_minutes(mode, nodes)
    return total


def availability(
    failures: tuple[FailureMode, ...] = DEFAULT_FAILURES,
    *,
    nodes: int = 0,
    has_ru_node: bool = False,
) -> float:
    """Ожидаемая доступность схемы: одна мера с `schema_reliability`, без расхождений.

    По умолчанию — схема «один сервер без РФ-ноды»; для другой схемы передай
    `nodes` и `has_ru_node`. Это ожидаемая доступность «в среднем по месяцу»,
    а не гарантия: 99,8 % допускает и полный простой на сутки, и десять обрывов.
    """
    return 1 - expected_downtime_minutes(failures, nodes=nodes, has_ru_node=has_ru_node) / (24 * 60 * 30)


def downtime_minutes_per_month(availability_value: float) -> float:
    """Сколько минут в месяц клиент в среднем не может подключиться."""
    if not 0 < availability_value <= 1:
        raise ValueError("доступность должна быть в диапазоне (0, 1]")
    return (1 - availability_value) * 24 * 60 * 30


@dataclass(frozen=True, slots=True)
class SchemaReliability:
    """Надёжность конкретной схемы: сколько точек отказа и что это значит."""

    plan_key: str
    nodes: int
    regions: int
    has_ru_node: bool
    expected_availability: float
    downtime_minutes: float
    bad_month_minutes: float
    worst_case_minutes: float
    notes: tuple[str, ...] = field(default_factory=tuple)


#: Вероятность, что за месяц РФ-нода получит требование РКН и её выключат.
RU_NODE_BLOCK_PROBABILITY: float = 0.05
#: Сколько это длится: пока не перевезём клиентов на зарубежную ноду, часов.
RU_NODE_BLOCK_HOURS: float = 12.0


def schema_reliability(plan: VpsPlan) -> SchemaReliability:
    """Оценить надёжность схемы: чем больше независимых нод, тем меньше радиус аварии.

    Логика — из таблицы режимов отказа (`DEFAULT_FAILURES`): одна нода в схеме
    накрывается отказом целиком, три ноды — примерно на треть. Поэтому надёжность
    растёт от ЧИСЛА независимых входов, а не от качества одного сервера.
    РФ-нода блокируется отдельным режимом отказа (её клиенты тоже доля сервиса).

    Худший случай — не «средний месяц», а одна авария уровня «нода/локация умерла»,
    которую админ разруливает руками: переезд делается за часы, не за минуты.
    """
    nodes = len(plan.nodes)
    regions = len({node.location for node in plan.nodes})
    # Панель, которая стоит на ноде, — тоже точка размещения: если она в РФ,
    # риск блокировки по требованию регулятора относится и к ней.
    servers = plan.nodes or (plan.panel,)
    has_ru = any(server.in_russia for server in servers)

    downtime = expected_downtime_minutes(nodes=nodes, has_ru_node=has_ru)
    expected = availability(nodes=nodes, has_ru_node=has_ru)

    # Плохой месяц — не «средний», а один реальный инцидент из практики 2026:
    # авария локации или блокировка подсети, которую разруливают руками.
    # У одного сервера это сутки простоя для ВСЕХ (наш кейс xorek), у трёх нод —
    # 4–8 часов для трети клиентов.
    bad_month = 24 * 60 if nodes == 0 else (8 * 60 if nodes == 2 else 4 * 60)

    # Худший случай на уровне сервиса: админ руками переезжает ноду целиком.
    worst = 120.0 if nodes >= 2 else 360.0

    notes: list[str] = []
    if nodes == 0:
        notes.append("одна точка отказа: авария сервера = сервис недоступен целиком")
    if nodes >= 2 and regions >= 2:
        notes.append("локации в разных странах: клиент меняет выход в приложении, без нового ключа")
    if has_ru:
        notes.append("РФ-нода: пинг лучше, но доступность зависит от требований регулятора")
    return SchemaReliability(
        plan_key=plan.key,
        nodes=nodes,
        regions=regions,
        has_ru_node=has_ru,
        expected_availability=expected,
        downtime_minutes=downtime,
        bad_month_minutes=bad_month,
        worst_case_minutes=worst,
        notes=tuple(notes),
    )
