# Клиентская сторона и эксплуатация: выживаемость на LTE в РФ, 2026

**Дата сбора:** 6 октября 2026 (Europe/Amsterdam). **Статус:** внутренний исследовательский отчёт.
**Контекст проекта:** панель 3x-ui, вход VLESS+Reality 443/tcp, второй протокол AmneziaWG 51820/udp, три ноды в трёх странах, подписка отдаётся одной ссылкой.

**Легенда достоверности:**

- **[П]** — проверено по первичному источнику в день сбора (официальная документация, код, релиз-ноты, страница магазина приложений).
- **[ИЗМ]** — есть измерение с методикой (публикация/отчёт с датой).
- **[З]** — заявлено одним автором или вендором, независимого подтверждения нет.
- **[ВЫВ]** — вывод этого отчёта, вытекающий из [П]-данных (не отдельный источник).
- **[НН]** — искал, публичного подтверждения не нашёл. Цифра не выдумана.

**Смежный контекст, на который опирается отчёт** (пересекается с `.research/whitelist-mechanics.md` и `.research/entry-points-ru.md`, здесь не перепроверялся):

- Основной режим на мобильных сетях РФ 2026 — **белый список по IP назначения** (гранулярность `/24`), TCP — инъекция RST, UDP — фильтрация по порту, SNI белым списком не является, но есть отдельный слой SNI-фильтрации и «16 КБ блок» на подсети зарубежных CDN/хостеров.
- **FOCI 2026, «On Russia's Early Introduction of QUIC SNI Censorship»** (Heitmann, Niere, Lange, Somorovsky; Paderborn University; [petsymposium.org/foci/2026/foci-2026-0010.php](https://petsymposium.org/foci/2026/foci-2026-0010.php), 2026) **[П]**: ТСПУ перешли на SNI-зависимую фильтрацию QUIC в период **май 2022 — июль 2023**, то есть минимум на 9 месяцев раньше, чем это задокументировано для GFW (апрель 2024). **Сегодня:** блокировка работает дропом пакетов, **affected all UDP ports** (а не только 443), блокирующий пакет включает **остаточную цензуру на 420 секунд для того же 4-tuple**, повторный Initial с блокированным SNI перезапускает таймер, затронут только трафик, **выходящий из страны**, и только **QUIC v1**, не QUIC v2.

Практический вывод для нас **[ВЫВ]**: на исходящем мобильном трафике РФ любой UDP-протокол (AmneziaWG, Hysteria2, TUIC) делит канал с QUIC-эвристикой. Даже если конкретный оператор не режет наш UDP-порт, 420-секундная остаточная цензура по 4-tuple — реальный риск для UDP-транспорта при смене SNI-паттерна в первых пакетах. Это делает **TCP-плечо (VLESS+Reality) обязательным**, а UDP — только вторым, не единственным.

---

## 1. Клиенты 2026: матрица поддержки

### 1.1. Версии на 06.10.2026 [П]

| Клиент | Платформы | Версия | Дата релиза/обновления | Ядро | Источник |
|---|---|---|---|---|---|
| **Happ — Proxy Utility** | iOS/macOS (+desktop), Android, Windows, Android TV | iOS **6.0.0**, Android **4.7.1** | 05.10.2026 / 05.10.2026 | Xray-core (+sing-box для туннеля части платформ) | [App Store id6504287215](https://apps.apple.com/us/app/happ-proxy-utility/id6504287215), [GitHub Happ-proxy/happ-android releases](https://github.com/Happ-proxy/happ-android/releases) |
| **v2rayNG** | Android | **2.3.10** | 01.10.2026 | Xray-core **v26.9.30** | [GitHub releases](https://github.com/2dust/v2rayNG/releases) |
| **Streisand** | iOS/iPadOS (macOS) | **1.6.76** | 10.09.2026 | Xray-core **v26.09.09** | [App Store id6450534064](https://apps.apple.com/ua/app/streisand/id6450534064) |
| **Karing** | iOS/Android/Windows/macOS/Android TV | iOS **1.2.25.2802**, Android **1.2.26.2906** | 11.09.2026 / 30.09.2026 | sing-box-совместимое ядро (Clash-совместимое) | [GitHub releases](https://github.com/KaringX/karing/releases), [App Store id6472431552](https://apps.apple.com/us/app/karing/id6472431552) |
| **sing-box SFA** | Android | **1.14.2** | сентябрь 2026 | sing-box 1.14.2 | [Google Play io.nekohasekai.sfa](https://play.google.com/store/apps/details?id=io.nekohasekai.sfa), [SagerNet/sing-box-for-android](https://github.com/SagerNet/sing-box-for-android) |
| **sing-box SFI** | iOS/macOS/tvOS | сборка от 02.10.2026 | 02.10.2026 | sing-box 1.14.x | [SagerNet/sing-box-for-apple](https://github.com/SagerNet/sing-box-for-apple) |
| **AmneziaVPN** | все | iOS **5.0.3** (min iOS 16), тег **5.0.3.0** | 21.09.2026 | собственный (AWG+VLESS/Shadowsocks) | [App Store id1600529900](https://apps.apple.com/us/app/amneziavpn/id1600529900), [GitHub tags](https://github.com/amnezia-vpn/amnezia-client/tags) |
| **AmneziaWG (лёгкий)** | Android / iOS | Android **v3.1.20260814**, iOS **3.1.4** | 14.08.2026 / 24.08.2026 | amneziawg-go | релизы проекта, [App Store id6478942365](https://apps.apple.com/app/amneziawg/id6478942365) |
| **Hiddify (Next)** | Android/iOS/Windows/macOS/Linux | **4.1.1** (Android/iOS 4.0/4.1.x) | 05.03.2026 (iOS 4.0 — 19.02.2026) | sing-box | [GitHub releases](https://github.com/hiddify/hiddify-app/releases), [App Store id6596777532](https://apps.apple.com/us/app/hiddify-proxy-vpn/id6596777532) |
| **NekoBox for Android** | Android | **1.4.2** | 09.02.2026 | sing-box **1.12.19-neko-1** (форк) | [GitHub releases](https://github.com/MatsuriDayo/NekoBoxForAndroid/releases) |

**Важное предупреждение по NekoBox [П]:** автор прямо пишет в релиз-нотах 1.4.2 — «本软件较少维护，**不接受功能请求**» (проект слабо поддерживается, функциональные запросы не принимаются) и **сам рекомендует другие клиенты**: для Xray-ядра — v2rayNG, для sing-box — sing-box for Android, для Clash-подписок — Clash Meta For Android / FlClash. Плюс: **версия в Google Play с мая 2024 контролируется третьей стороной и не является open-source** — использовать только APK с GitHub. **[ВЫВ]**: NekoBox не годится как рекомендованный клиент для клиентов сервиса, только как инструмент отладки.

### 1.2. Матрица «клиент → что поддерживает»

Обозначения: ✅ поддержка есть · ⚠️ частично/с оговоркой · ❌ нет · **?** не найдено.
«fragment» = дробление TLS ClientHello; «urltest» = авто-выбор по задержке; «fallback» = автопереключение при обрыве.

| Клиент | XHTTP | AnyTLS | mieru | Hysteria2 | AmneziaWG | VLESS+Reality | urltest | fallback | fragment | mux/XUDP | Подписки |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **Happ** | ✅ (Xray xhttp) | ❌ | ❌ | ✅ (`hy2://`) | ❌ | ✅ | ⚠️ **есть 3 вида Ping (ICMP/TCP/via Proxy), авто-переключения в доке нет** | ⚠️ **?** | ✅ `fragment=` + `noises=` | ✅ (Xray mux) | base64-список, **JSON-массив Xray**, encrypted-sub |
| **v2rayNG 2.3.10** | ✅ (режим + raw JSON `extra`) | ❌ (Xray не поддерживает) | ❌ | ✅ | ⚠️ только через Xray WireGuard-outbound (не AWG) | ✅ | ✅ **Observatory** (leastPing / leastLoad) | ✅ balancer+burstObservatory | ✅ **UI: Fragment Settings** | ✅ mux + XUDP (concurrency, xudpConcurrency, QUIC policy) | base64, v2rayN-совместимые |
| **Streisand 1.6.76** | ❌ (в описании нет; ядро Xray — **?**, UI-поля не подтверждены) | ❌ | ❌ | ✅ Hysteria v2 | ❌ | ✅ | **?** | **?** | **?** | **?** | base64/список |
| **Karing** | ✅ (Clash-совместимый транспорт) | ✅ | ✅ | ✅ (порт-хоппинг с 1.0.29.390) | ❌ | ✅ | ✅ «自动选择» (url-test) | ❌ **«自动回退» не поддерживается** | ⚠️ через sing-box-настройки | ✅ sing-mux | Clash YAML, sing-box, base64 |
| **SFA / SFI (sing-box)** | ✅ xhttp | ✅ AnyTLS (outbound) | ⚠️ только через форк/mbox, не в апстриме sing-box | ✅ | ❌ (нет AWG в апстриме) | ✅ | ✅ `urltest` | ✅ `urltest` + `interrupt_exist_connections` | ✅ `tls.fragment` / `record_fragment` | ✅ `multiplex` | sing-box JSON, base64, Clash |
| **AmneziaVPN** | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ (VLESS/Reality как второй протокол) | ⚠️ ручной выбор сервера | ❌ | ❌ | н/д | собственный `vpn://` |
| **AmneziaWG (лёгкий)** | ❌ | ❌ | ❌ | ❌ | ✅ **3.1** | ❌ | ❌ | ❌ | ❌ | н/д | `.conf` / QR |
| **Hiddify 4.1.1** | ✅ (добавлен в iOS 4.0) | ⚠️ **?** | ✅ (добавлен в iOS 4.0) | ✅ | ❌ | ✅ | ✅ «Automatically select LowestPing / LoadBalance» | ✅ | ⚠️ через sing-box-профиль | ✅ | sing-box, V2ray/Xray, Clash, Clash Meta |
| **NekoBox 1.4.2** | ⚠️ (ядро sing-box 1.12.x — xhttp есть, UI — **?**) | ✅ | ✅ (плагин `mieru-plugin`) | ✅ Hysteria 1/2 | ❌ (WireGuard да, AWG нет) | ✅ | ✅ (sing-box urltest) | ✅ | ⚠️ | ✅ | sing-box outbounds, ClashMeta, v2rayN, Shadowsocks |

**Первичные подтверждения по строкам:**

- **AnyTLS не поддерживается Xray-core вообще** — это отдельный протокол из семейства sing-box. Явное подтверждение: [«AnyTLS — not supported by Xray-core»](https://core-tutorial.argsment.com/xray/anytls) **[П]**. Отсюда автоматически: **Happ, v2rayNG, Streisand (Xray-ядро) AnyTLS не умеют**.
- **NekoBox — список протоколов** из README проекта [П]: SOCKS(4/4a/5), HTTP(S), SSH, Shadowsocks, VMess, Trojan, VLESS, **AnyTLS**, ShadowTLS, TUIC, Hysteria 1/2, WireGuard, Trojan-Go (плагин), NaïveProxy (плагин), **Mieru (mieru-plugin)**.
- **Karing — Clash-совместимость** [П]: официальная страница [karing.app/clash](https://karing.app/clash) отмечает ✅ AnyTLS, ✅ Mieru, ✅ Tailscale, ✅ TUIC, ✅ Hysteria2 (порт-хоппинг), ✅ sing-mux, ✅ dialer-proxy; и **❌ «自动回退» (automatic fallback)**, **❌ «负载均衡» (load balance)**, **❌ «链式代理» (chained proxy)**.
- **Happ — поддерживаемые протоколы** [П]: официальная документация перечисляет VLESS, VMess, Shadowsocks, Socks5, Trojan, Hysteria2 (в т.ч. схема `hy2://`) — [happ.su: Examples of links and parameters](https://www.happ.su/main/dev-docs/examples-of-links-and-parameters). AnyTLS/mieru/AmneziaWG в списке отсутствуют.
- **v2rayNG 2.3.10** [П]: в `strings.xml` есть `server_lab_xhttp_mode`, `server_lab_xhttp_extra` («XHTTP Extra raw JSON»), `title_fragment_settings` (Packets / Length / Interval / Max Split / Enable), `title_observatory_settings` (leastPing/leastLoad interval, method, sampling, timeout), `title_mux_settings` (concurrency, xudpConcurrency, xudp-quic: reject/allow/skip), `menu_item_import_config_manually_hysteria2`, `menu_item_import_config_manually_wireguard`, `routing_preset_russia_whitelist`. Источник: [2dust/v2rayNG master strings.xml](https://github.com/2dust/v2rayNG/blob/master/V2rayNG/app/src/main/res/values/strings.xml) и [AppConfig.kt](https://github.com/2dust/v2rayNG/blob/master/V2rayNG/app/src/main/java/com/v2ray/ang/AppConfig.kt).
- **v2rayNG дефолты Observatory** [П] (AppConfig.kt): `leastPingInterval = 3m`, `leastLoadInterval = 5m`, `leastLoadMethod = HEAD`, `leastLoadSampling = 2`, `leastLoadTimeout = 30s`. Есть тег balancer `balancer-main` — то есть нативный Xray-балансировщик, а не только «пинг всех серверов».
- **Streisand** [П]: описание в App Store перечисляет VLESS(Reality), VMess, Trojan, Shadowsocks, Socks, SSH, Hysteria(V2), TUIC, Wireguard. Release notes 1.6.76: «Updated core to v26.09.09».
- **Осторожно: в App Store есть клоны Happ.** Официальное приложение — **`Happ - Proxy Utility`** от **Flyfrog LLC** (trackId **6504287215**, ver 6.0.0 от 05.10.2026, iOS 15+) [П]. В поиске App Store рядом висят сторонние «Happ VPN» (PAUL PARVINDER KAI SIA E.E., SPK TECH LTD, INTRADEI TOV, Coconut Pools LLC) с версиями 1.0.x — **это не Happ**. В инструкции для клиентов надо давать прямую ссылку, а не название.
- **Hiddify iOS 4.0 release notes** [П]: «Added support for **XHTTP, Psiphon, Naive and Mieru** protocols».
- **Happ: Ping — три метода, но авто-переключения нет** [П] ([happ.su: Ping](https://www.happ.su/main/dev-docs/ping)): **ICMP Ping** (требует временно отключить тоннель), **TCP Ping** (прямой TCP до сервера, тоннель отключать не нужно, но «при использовании CDN не отражает реальную задержку до конечного сервера — пинг доходит только до ближайшего CDN-узла») и **via Proxy Ping** («самый точный и показательный метод, включает все этапы соединения, включая TLS-рукопожатие»). Выбор сервера — **ручной**: в документации Happ **нет** описания автоматического выбора по задержке или авто-переключения при обрыве. **Это ключевое отличие Happ от v2rayNG/sing-box** — если нужна автоматика, её надо закладывать в подписку (например, через штатные Sub Balancers 3x-ui), а не ждать от приложения.
- **Happ: подписка может управлять приложением** [П] — [happ.su: App management](https://www.happ.su/main/dev-docs/app-management). Через HTTP-заголовки или тело подписки: `profile-update-interval`, `profile-title`, `subscription-userinfo: upload=…;download=…;total=…;expire=…`, `announce`, `support-url`, `profile-web-page-url`, отключение маршрутизации, SOCKS/HTTP-inbound auth, ссылка на routing-профиль (`routing` header). Часть параметров требует **Provider ID**.
- **Happ и JSON-подписка** [П], [happ.su: Routing](https://www.happ.su/main/dev-docs/routing): при импорте JSON приложение отдаёт его ядру Xray **строго 1:1** — «стандартные правила маршрутизации и настройки интерфейса Happ к JSON-файлу не применяются». Для JSON-подписки разрешён **ровно 0 или 1 routing-профиль**, его нельзя ни создать вручную, ни скопировать, ни отключить — он приходит только от провайдера. **Это ключевое ограничение для нас:** если мы отдаём Happ JSON, мы обязаны сами положить туда routing и DNS, иначе клиент останется без раздельного туннелирования.

---

## 2. Fragment и noise: дробление TLS ClientHello

### 2.1. Механика

Классический TLS: первый же сегмент потока — ClientHello, в нём открытым текстом `server_name` (SNI). DPI, который умеет только «увидеть SNI в первых байтах потока», режет или инжектирует RST.

Fragment режет ClientHello на несколько TCP-сегментов с паузами. Тогда SNI оказывается либо во втором/третьем сегменте, либо разрезан между ними, и наивный парсер не собирает строку. Два режима, по документации Xray [П] ([xtls.github.io/config/outbounds/freedom](https://xtls.github.io/en/config/outbounds/freedom.html)):

- `"tlshello"` — **фрагментация TLS-handshake пакета** (то есть ClientHello);
- `"1-3"` — **нарезка TCP-потока**, применяется к 1-й…3-й записи клиента в сокет.

**Важное ограничение [П]:** дробление не работает для UDP — в Xray fragment описан как «outgoing **TCP** fragmentation». Все UDP-протоколы (AmneziaWG, Hysteria2, mux/XUDP) остаются без этой защиты.

### 2.2. Xray: два разных места конфигурации (важно!)

Есть **два несовместимых синтаксиса**, и это главная ловушка 2026 года.

**(а) Классический — freedom outbound** (работает на любой версии, включая клиентские сборки Xray до введения FinalMask) [П]:

```json
{
  "outbounds": [
    {
      "protocol": "freedom",
      "tag": "fragment",
      "settings": {
        "fragment": {
          "packets": "tlshello",
          "length": "100-200",
          "interval": "10-20"
        },
        "noises": [
          {
            "type": "rand",
            "packet": "50-150",
            "delay": "10-16"
          }
        ]
      }
    }
  ]
}
```

- `packets`: `"tlshello"` (фрагментация TLS-handshake) либо `"1-3"` (нарезка TCP-потока по 1-й…3-й записи).
- `length`: длина фрагмента в байтах, диапазон (`Int32Range`).
- `interval`: **интервал между фрагментами в миллисекундах**.
- `interval: 0` + `tlshello` — ClientHello уйдёт одним TCP-пакетом (если влезает в MSS/MTU).
- `noises[]`: **UDP-шум**, три типа: `"rand"` (случайные данные, `packet` = длина или диапазон), `"str"` (заданная строка), `"base64"` (base64-бинарник). Ключ называется **`noises`** (множественное), в отличие от `fragment`. В документации явное предупреждение: «might deceive sniffers, or it might **disrupt normal connections**. Use at your own risk» и оговорка, что noise **не применяется к порту 53**, иначе ломает DNS.

**(б) Новый — FinalMask** (Xray-core 26.x; в 3x-ui отображается как отдельный слой) [П]:

```json
{
  "streamSettings": {
    "finalmask": {
      "tcp": [
        {
          "type": "fragment",
          "settings": {
            "packets": "tlshello",
            "lengths": ["3-5", "6-8", "10-20"],
            "delays": ["10-20"],
            "maxSplit": "3-6"
          }
        }
      ]
    }
  }
}
```

Отличия от (а), которые важно знать **[П]** ([Xray-docs-next finalmask.md](https://raw.githubusercontent.com/XTLS/Xray-docs-next/main/docs/en/config/transports/finalmask.md)):

- `lengths` и `delays` — **массивы**: n-й элемент задаёт длину/задержку n-го фрагмента; последний элемент повторяется для всех последующих.
- `delays` — задержка **после** отправки n-го фрагмента (в (а) `interval` — между фрагментами).
- **`maxSplit`** — максимальное число фрагментов, `0` = не ограничено. Только в FinalMask.
- Элементы `lengths`, кроме последнего, **могут быть `0`**, но это даёт «RFC-violating empty TLS record», который терпят не все реализации («tolerated by some implementations, but not Golang»).
- FinalMask умеет ещё TCP-маски `sudoku`, `header-custom`, `xmc` (маскировка под Minecraft) и UDP-маски `salamander`, `mkcp-legacy`, `header-custom`, `xdns`, `xicmp`, `noise`, `sudoku`, `realm`.

**Откуда взялся `maxSplit` в UI v2rayNG:** в `strings.xml` есть `title_pref_fragment_maxsplit` [П] — то есть v2rayNG 2.3.10 уже отдаёт этот параметр. Совместимость с ядром v26.9.30 (та же версия, что в релиз-нотах) — **[ВЫВ]**.

**Лимиты значений** (по документации BPB Panel, поддерживающей генерацию fragment-конфигов для Xray-клиентов) [З]: «Length cannot exceed **500**, and the Delay cannot exceed **30 ms**» ([bia-pain-bache.github.io/BPB-Worker-Panel/configuration/fragment](https://bia-pain-bache.github.io/BPB-Worker-Panel/configuration/fragment/)). Дефолт BPB: **Length 100-200, Delay 1-1, Packets tlshello**. Независимого подтверждения лимитов из кода Xray я не нашёл — **[НН]**.

### 2.3. sing-box: fragment — это `bool`, а не структура (частая ошибка)

Документация sing-box 1.14.2 [П] ([shared/tls.md](https://github.com/SagerNet/sing-box/blob/v1.14.2/docs/configuration/shared/tls.md)):

```json
{
  "type": "vless",
  "tag": "proxy-a",
  "server": "1.2.3.4",
  "server_port": 443,
  "uuid": "00000000-0000-0000-0000-000000000000",
  "flow": "xtls-rprx-vision",
  "tls": {
    "enabled": true,
    "server_name": "www.example.com",
    "utls": { "enabled": true, "fingerprint": "chrome" },
    "reality": {
      "enabled": true,
      "public_key": "REPLACE_WITH_PUBLIC_KEY",
      "short_id": "0123456789abcdef"
    },
    "fragment": true,
    "fragment_fallback_delay": "500ms",
    "record_fragment": false
  }
}
```

- **`fragment` — булево** (`true`/`false`), добавлено в **sing-box 1.12.0**. Никаких `size`/`sleep` — старый синтаксис `"fragment": {"size": …, "sleep": …}` в актуальных версиях **недействителен**. Проверка: в документации v1.11.15 раздела про fragment **нет вовсе** (grep по `fragment` в `docs/configuration/shared/tls.md` тега v1.11.15 пуст) **[П]**.
- `record_fragment` (тоже с 1.12.0) — режет handshake на **несколько TLS-записей** вместо TCP-сегментов.
- `fragment_fallback_delay` (по умолчанию `500ms`) — используется, когда sing-box не может сам определить время ожидания. На Linux/Apple/Windows (с правами администратора) задержка определяется автоматически; **если реальная задержка < 20 мс, тоже падает на фиксированное значение**, потому что цель считается локальной или за прозрачным прокси.
- **Прямая рекомендация разработчика [П]:** «This feature is intended to circumvent simple firewalls based on **plaintext packet matching**, and should not be used to circumvent real censorship. Due to poor performance, try **`record_fragment` first**, and only apply to server names known to be blocked».

Это самое важное предложение всего раздела, и оно противоречит массовой практике «включить fragment всем». Правильный порядок — `record_fragment` → потом `fragment` → и только на конкретные заблокированные SNI.

### 2.4. Как это включается в клиентах

| Клиент | Где | Синтаксис / поля | Примечание |
|---|---|---|---|
| **v2rayNG 2.3.10** | Настройки → **Fragment Settings** | Enable Fragment, Fragment Packets, Fragment Length (min-max), Fragment Interval (min-max), Fragment Max Split | Глобальные настройки ядра (Xray freedom outbound) **[П]** |
| **Happ** | В URI-параметрах ссылки либо в глобальных настройках приложения | `fragment=length,interval,packets[,maxSplit]`, пример из документации: `fragment=1-10,5-20,tlshello`; `noises=type,packet,delay[,applyTo]`, пример: `noises=rand,50-150,10-50,ip` | **«Local server settings take priority only if the app's global settings are disabled»** — то есть параметр из ссылки сильнее глобального **[П]** ([happ.su](https://www.happ.su/main/dev-docs/examples-of-links-and-parameters)) |
| **sing-box SFA/SFI** | В JSON-профиле, `tls.fragment: true` | bool | Глобального UI-тумблера в SFA/SFI не подтверждено — **[НН]** |
| **Karing** | Секция Fragment в профиле (Clash-совместимый) | — | «Fragment можно включить для sing-box, но sing-box **не использует fragment-настройки и берёт свои дефолты**» — предупреждение BPB **[З]** |
| **Streisand** | — | — | Отдельного UI для fragment в описании приложения нет **[НН]** |
| **Hiddify / NekoBox** | — | — | Через sing-box JSON-профиль **[ВЫВ]** |

### 2.5. Помогает ли против SNI-фильтрации на LTE в РФ

**Ответ: против SNI-фильтрации — да, но именно той, что работает по «открытому тексту в первых байтах». Против белого списка по IP — нет вообще.** Разбор по слоям:

| Слой фильтрации (по `.research/whitelist-mechanics.md`) | Помогает ли fragment | Почему |
|---|---|---|
| **Белый список по IP (`/24`)** | ❌ **Нет** | Фильтрация идёт по IP назначения до всякого TLS. SNI не важен; фрагментировать нечего. Пакеты просто дропаются на L3 **[ВЫВ]** |
| **SNI-фильтрация с RST (L7 по ClientHello)** | ✅ **Да, часто** | SNI перестаёт попадать целиком в один сегмент. Это ровно тот сценарий, для которого fragment и создан **[П]** |
| **«16 КБ блок» на подсети CDN/хостеров** | ❌ **Нет** | Блокировка по объёму/подсети, а не по содержимому первых байт **[ВЫВ]** |
| **QUIC SNI-фильтрация (UDP)** | ❌ **Нет** | fragment в Xray — только TCP. QUIC Initial шифруется ключом из публичных данных, и ТСПУ его расшифровывает (FOCI 2026) **[П]** |
| **Активное зондирование (active probing)** | ❌ **Нет, и может вредить** | Зондирование — это отдельный механизм; fragment его не адресует. Для этого у AmneziaWG есть CPS/I1, у Reality — свой механизм **[ВЫВ]** |

**Практический тест, чтобы не гадать** **[ВЫВ]**: если нода на LTE не открывается вообще (TCP-handshake до 443 не проходит, `rc=28`/timeout), fragment бесполезен — это L3/белый список. Если TCP-handshake проходит, TLS начинается, но соединение обрывается на первом же ответе сервера или сразу после ClientHello — вот это и есть SNI-RST, и fragment/`record_fragment` стоит попробовать.

### 2.6. Побочные эффекты fragment (чек-лист «когда включать / когда выключать»)

**[П]** по документации sing-box и Xray:

- **Производительность.** «Due to poor performance, try `record_fragment` first» — прямое указание, что fragment дорогой. Он режет рукопожатие на N сегментов с паузами; +1 RTT-эквивалент на каждое новое соединение.
- **`interval`/`delays` — это задержка в мс.** При `10-20` мс и 6 фрагментах вы добавляете 60-100 мс к каждому handshake. На LTE с RTT 40-80 мс это заметно.
- **`length: 0`** даёт пустую TLS-запись; некоторые реализации её не терпят.
- **DNS.** Xray `noises` намеренно обходит порт 53, иначе ломает DNS — то есть авторы уже знают, что UDP-шум ломает протоколы.
- **Reality.** Fragment работает поверх Reality (это уровень TCP/stream), но Reality и так подменяет TLS-отпечаток и синхронизируется с реальным сайтом — **двойная защита не нужна и стоит задержки** **[ВЫВ]**.
- **mux.** При включённом mux ClientHello происходит один раз на mux-соединение, а не на каждое приложение; fragment и mux не конфликтуют, но выигрыш от fragment с mux меньше по числу рукопожатий **[ВЫВ]**.

**Правило [ВЫВ]:**

1. По умолчанию fragment **выключен**.
2. Включаем **только** если измеренно подтверждён SNI-RST (см. §7) и **только** для конкретных SNI.
3. Первым пробуем `record_fragment: true` (sing-box) / режим с малой задержкой (Xray `interval: 0-1`), и лишь потом `fragment: true` / `tlshello`.
4. Стартовые значения: `packets: "tlshello"`, `length: "100-200"`, `interval: "1-1"` (это дефолт BPB Panel) → если не помогло, повышаем до `length: "10-20"`, `interval: "10-20"`.
5. `noises` не включаем без отдельного повода: документация сама называет их рискованными.

---

## 3. Мультиплексирование (mux.cool / XUDP) и padding

### 3.1. Что говорит официальная документация Xray

**[П]** ([xtls.github.io/config/outbound](https://xtls.github.io/en/config/outbound.html)) — цитата, которую стоит запомнить дословно:

> «The Mux function distributes data from multiple TCP connections over a single TCP connection… **Mux is designed to reduce TCP handshake latency, not to increase connection throughput. Using Mux for watching videos, downloading, or speed testing usually has a negative effect.** Mux only needs to be enabled on the client side; the server side adapts automatically. The second use of Mux is to distribute multiple UDP connections, i.e., XUDP.»

Параметры `MuxObject` **[П]**:

```json
{
  "enabled": true,
  "concurrency": 8,
  "xudpConcurrency": 16,
  "xudpProxyUDP443": "reject"
}
```

| Поле | Смысл | Диапазон / дефолт |
|---|---|---|
| `concurrency` | Максимум под-соединений на одном TCP | 1…128, при `0`/пусто = **8**; >128 обрезается до 128 (после 128 переиспользований соединение больше не получает под-соединений). **Отрицательное значение (напр. `-1`) полностью отключает mux для TCP.** |
| `xudpConcurrency` | Отдельный XUDP-агрегатный туннель (ещё одно mux-соединение) для UDP | 1…1024. **`0`/пусто = UDP идёт тем же путём, что TCP** (традиционное поведение). Отрицательное = не использовать mux для UDP, взять родной UDP протокола (Shadowsocks — native UDP, VLESS — UoT) |
| `xudpProxyUDP443` | Что делать с проксируемым UDP/443 (QUIC) | `reject` (дефолт — браузер сам откатится на TCP HTTP/2), `allow`, `skip` |

### 3.2. XHTTP: главное предупреждение и взаимодействие с mux

**[П]** ([lcuwx2016.github.io/xtls/config/transports/xhttp](https://lcuwx2016.github.io/xtls/config/transports/xhttp.html), раздел с DANGER-блоком):

> «使用 XHTTP 时**不要启用 mux.cool**，新版 Xray 服务端已有检查，**只接受纯 XUDP**»
> (При использовании XHTTP **не включайте mux.cool** — новый серверный Xray это проверяет и принимает **только чистый XUDP**.)

И там же:

> «XHTTP 默认有多路复用，延迟比 Vision 低但多线程测速不如它，除非测速前设了 `"maxConcurrency": 1`»
> (У XHTTP мультиплексирование включено по умолчанию; задержка ниже, чем у Vision, но многопоточный speedtest хуже — если перед тестом не поставить `maxConcurrency: 1`.)

**[ВЫВ]** Это значит: **XHTTP и mux — взаимоисключающие.** Если мы добавляем XHTTP-инбаунд как резервный, в клиенте нужно либо отключить mux (`concurrency: -1`), либо оставить mux только для XUDP (`xudpConcurrency` > 0, `concurrency: -1`). Иначе сервер откажет.

### 3.3. XHTTP: ключевые настройки для мобильных сетей

**[П]** по XHTTP-документации:

| Параметр | Дефолт | Что делает | Почему важно на LTE |
|---|---|---|---|
| `mode` | `auto` | `auto` / `packet-up` / `stream-up` / `stream-one`. В `auto` **на клиенте**: TLS H2 → `stream-up`; **REALITY → `stream-one`**; иначе → `packet-up` | `stream-one` — один POST с двусторонним стримом: минимум запросов. `packet-up` — максимум совместимости с кривыми посредниками |
| `xPaddingBytes` | `100-1000` | Случайный padding HTTP-заголовков; запрос — `Referer: ...?x_padding=XXX...`, ответ — `X-Padding: XXX...` | Стирает фиксированную длину заголовков |
| `scMaxEachPostBytes` | `1000000` (1 МБ) | Сколько данных несёт один POST | При обрывах LTE меньшее значение = меньше потерь на реконнект |
| `scMinPostsIntervalMs` | `30` | Минимальный интервал POST, мс | На нестабильном LTE больший интервал снижает риск «пачки ретрансмиссий» |
| `scMaxBufferedPosts` | `30` | Сколько POST сервер буферизует | При потерях LTE больше = устойчивее |
| `scStreamUpServerSecs` | `"20-80"` | Сервер шлёт padding каждые N сек, чтобы CDN/посредник не рвал «молчащее» соединение; `-1` отключает | Прямо адресует обрывы долгих соединений на мобильных |
| `xmux.maxConcurrency` | `"16-32"` | Сколько прокси-запросов одновременно в одном TCP/QUIC | Конфликтует с `maxConnections` (только одно из двух!) |
| `xmux.hMaxRequestTimes` | `"600-900"` | Лимит HTTP-запросов на соединение (Nginx по умолчанию 1000) | Не ставить в упор к лимиту посредника |
| `xmux.hMaxReusableSecs` | `"1800-3000"` | Сколько секунд живёт соединение (Nginx по умолчанию 1 час) | Для LTE: меньший срок = чаще свежий путь |
| `xmux.hKeepAlivePeriod` | `0` (=45 с для Chrome H2 / 10 с для H3) | Keepalive-пинг на простаивающем H2/H3 | На LTE NAT-таймаут оператора часто 30-60 с **[ВЫВ]** — держать keepalive включённым |

**Ловушка [П]:** «когда в XMUX заполнено хоть одно поле, остальные **больше не берут дефолты автоматически** — заполнять нужно все».

**Мобильный профиль для XHTTP [ВЫВ]** (наш вариант «устойчивость важнее скорости»):

```
mode: "stream-one"          # при Reality в auto и так stream-one
xPaddingBytes: "100-1000"   # дефолт, не трогаем
scMaxEachPostBytes: "300000-600000"   # меньше 1 МБ — короче «хвост» при обрыве
scMinPostsIntervalMs: "30-60"
scMaxBufferedPosts: 60                # вверх от дефолта 30
scStreamUpServerSecs: "20-40"         # чаще padding-keepalive
xmux: { maxConcurrency: "8-16", maxConnections: 0, cMaxReuseTimes: 0,
        hMaxRequestTimes: "400-600", hMaxReusableSecs: "900-1500", hKeepAlivePeriod: 0 }
```

Оговорка: эти значения — **[ВЫВ]** из документации, а не измеренный оптимум. Их надо проверять A/B на живых LTE-симках.

### 3.4. sing-box: multiplex

**[П]** ([sing-box 1.14.2 shared/multiplex.md](https://github.com/SagerNet/sing-box/blob/v1.14.2/docs/configuration/shared/multiplex.md)):

```json
{
  "multiplex": {
    "enabled": true,
    "protocol": "h2mux",
    "max_connections": 4,
    "min_streams": 4,
    "padding": true
  }
}
```

| Поле | Смысл |
|---|---|
| `protocol` | `smux` (xtaci/smux), `yamux` (hashicorp/yamux), **`h2mux` — дефолт** |
| `max_connections` | Максимум соединений. **Конфликтует с `max_streams`** |
| `min_streams` | Минимум потоков в соединении до открытия нового. **Конфликтует с `max_streams`** |
| `max_streams` | Максимум потоков на соединение. Конфликтует с `max_connections` и `min_streams` |
| `padding` | Включить padding. **Требует сервер sing-box ≥ 1.3-beta9.** На входящем (сервер) `padding: true` означает «отклонять соединения без padding» |

**[ВЫВ]** `h2mux` по умолчанию — не случайно: он даёт настоящий HTTP/2-фрейминг, что само по себе выглядит как обычный HTTPS. Для мобильных сетей `max_connections: 4` + `min_streams: 4` — разумный компромисс: не агрегирует всё в один поток (иначе одна потеря сегмента тормозит все приложения — head-of-line blocking), но сокращает число рукопожатий.

### 3.5. Padding: где он есть и что реально даёт

| Место | Что это | Ценность на LTE |
|---|---|---|
| **XHTTP `xPaddingBytes`** | Padding HTTP-заголовков запроса/ответа | **Высокая** — убирает фиксированную длину заголовков, самое дешёвое и безопасное улучшение **[П]** |
| **sing-box `multiplex.padding`** | Padding внутри mux-фреймов | Средняя; требует серверной поддержки **[П]** |
| **AmneziaWG `ContentPaddingAddition`** | Случайная добавка к транспортным пакетам | **Высокая для UDP**: снимает «все пакеты кратны 16» — характерный признак WireGuard **[П]** |
| **AmneziaWG `S1-S4`** | Префиксы к Init/Response/Cookie/Data | **Высокая**: убирает точные размеры 148/92/64 байта **[П]** |
| **Hysteria2 salamander** | Обфускация QUIC | Есть в FinalMask как UDP-маска `salamander` (`password`, `packetSize: "512-1200"`) **[П]** |
| **AnyTLS padding scheme** | Встроенная схема padding | Не проверил деталей — **[НН]** |
| **VLESS padding (Xray)** | Padding в VLESS | Не подтверждено отдельной опцией в актуальной документации — **[НН]** |

### 3.6. Таблица «сценарий → настройка» [ВЫВ]

| Сценарий | mux.cool | XUDP | padding | Что ожидать |
|---|---|---|---|---|
| **Браузинг с телефона** | ✅ вкл, `concurrency: 4-8` | `xudpConcurrency: 8-16`, `xudpProxyUDP443: "reject"` | XHTTP padding вкл | Меньше рукопожатий → отзывчивее открытие сайтов. Дефолт v2rayNG: XUDP 8 |
| **YouTube 1080p / стриминг** | ❌ **выкл** (`concurrency: -1`) | `xudpConcurrency: -1` (родной UDP) | XHTTP padding вкл | Документация Xray прямо: mux для видео «usually has a negative effect» |
| **Торренты / большие загрузки** | ❌ выкл | `-1` | — | Многопоточная загрузка в один mux-поток = узкое место |
| **Игры / видеозвонки (UDP)** | ⚠️ TCP не трогаем | `xudpConcurrency: -1` **обязательно** | — | Иначе весь UDP-игровой трафик идёт через TCP-туннель: лаг и потери |
| **Нестабильный LTE, частая смена IP** | ⚠️ вкл с малым `concurrency: 2-4` | `xudpConcurrency: 4-8` | вкл | Mux даёт **быстрое восстановление**: при обрыве одного TCP теряются только активные под-соединения, а не вся сессия. Но HOL-blocking реален |
| **XHTTP-транспорт** | ❌ **обязательно выкл**, `concurrency: -1` | Только `xudpConcurrency > 0` | вкл | Иначе сервер Xray **откажет** (принимает только чистый XUDP) **[П]** |

---

## 4. Готовые fallback-цепочки

### 4.1. sing-box: параметры `urltest` (полностью)

**[П]** ([sing-box 1.14.2 outbound/urltest.md](https://github.com/SagerNet/sing-box/blob/v1.14.2/docs/configuration/outbound/urltest.md)):

```json
{
  "type": "urltest",
  "tag": "auto",
  "outbounds": ["proxy-a", "proxy-b", "proxy-c"],
  "url": "",
  "interval": "",
  "tolerance": 0,
  "idle_timeout": "",
  "interrupt_exist_connections": false
}
```

| Поле | Дефолт | Значение |
|---|---|---|
| `outbounds` | **обязательно** | Список тегируемых outbound'ов |
| `url` | `https://www.gstatic.com/generate_204` | URL проверки |
| `interval` | **`3m`** | Интервал проверки |
| `tolerance` | **`50`** мс | Допуск: outbound считается «лучше» только если быстрее текущего более чем на tolerance. Защита от «дёргания» между нодами |
| `idle_timeout` | `30m` | Через сколько простоя перестать проверять группу |
| `interrupt_exist_connections` | `false` | **Рвать ли активные соединения при смене выбранного outbound.** Влияет только на входящие соединения; внутренние рвутся всегда |

**Как ведёт себя при обрыве [ВЫВ]** (из семантики параметров, документация прямо этого не пишет — **[НН]** в части «мгновенности»): `urltest` — это активный пробник по расписанию. Мгновенного переключения при обрыве нет; переключение происходит на следующей проверке. Поэтому `interval: 3m` означает «до трёх минут клиент может сидеть на мёртвой ноде». Для нашего кейса это главный аргумент за уменьшение интервала и за `interrupt_exist_connections: true` в мобильном профиле.

**`selector` [П]:** управляется **только через Clash API**, поэтому в подписке его смысл ограничен — но он нужен, чтобы дать пользователю ручной выбор поверх автоматики:

```json
{
  "type": "selector",
  "tag": "select",
  "outbounds": ["auto", "proxy-a", "proxy-b", "proxy-c"],
  "default": "auto",
  "interrupt_exist_connections": false
}
```

Лучшая практика **[ВЫВ]**: маршрутизацию направлять не на `urltest`, а на `selector`, внутри которого первым и дефолтным элементом стоит `urltest`-группа. Тогда автоматика работает, но у пользователя есть ручной override в UI.

### 4.2. Какой URL проверки использовать в РФ 2026

**[П]** Xray использует по умолчанию `https://connectivitycheck.gstatic.com/generate_204` (для BurstObservatory) и `https://www.google.com/generate_204` (в примере ObservatoryObject); sing-box — `https://www.gstatic.com/generate_204`.

**[ВЫВ], важно:** проверка идёт **через сам проксируемый outbound**, то есть запрос уходит через ноду. Значит, домен проверки должен быть доступен **из страны ноды**, а не из РФ. `gstatic.com`/`google.com` надёжны в большинстве стран, но в Китае/Иране нет. Более нейтральные варианты:

- `http://cp.cloudflare.com/generate_204` — Cloudflare, доступен почти везде;
- `http://connectivitycheck.gstatic.com/generate_204` — дефолт Xray;
- `https://www.gstatic.com/generate_204` — дефолт sing-box.

**Чего избегать [ВЫВ]:** российских доменов (они пойдут в обход или дадут ложный «зелёный» при живом операторе) и доменов, которые сами блокируются в стране ноды. Если нода в Китае — ни один из gstatic-вариантов не подойдёт.

**Расход трафика и батарея [ВЫВ]:** каждый прогон — один HTTP-запрос на каждый outbound. При 3 нодах и `interval: 30s` это 8 640 запросов в сутки с телефона. Это заметно и по трафику, и по батарее. Разумный мобильный диапазон — **60-180 с**, не 5-30 с.

### 4.3. Полные JSON-конфиги sing-box (валидны по структуре; проверять под свою версию)

> Все конфиги ниже — **заготовки**. Подставьте свои `server`, `uuid`, `public_key`, `short_id`, `server_name`. JSON проверен на синтаксическую валидность.

#### (а) Три ноды VLESS+Reality+tcp, urltest, без fragment

```json
{
  "log": { "level": "warn", "timestamp": true },
  "dns": {
    "servers": [
      { "tag": "remote", "address": "https://1.1.1.1/dns-query", "detour": "select" },
      { "tag": "local", "address": "77.88.8.8", "detour": "direct" }
    ],
    "rules": [
      { "outbound": "any", "server": "local" },
      { "rule_set": "ru-domains", "server": "local" }
    ],
    "final": "remote",
    "strategy": "prefer_ipv4"
  },
  "inbounds": [
    {
      "type": "tun",
      "tag": "tun-in",
      "address": ["172.19.0.1/30", "fdfe:dcba:9876::1/126"],
      "auto_route": true,
      "strict_route": true,
      "stack": "mixed",
      "mtu": 1400
    }
  ],
  "outbounds": [
    {
      "type": "vless",
      "tag": "proxy-a",
      "server": "NODE_A_IP",
      "server_port": 443,
      "uuid": "NODE_A_UUID",
      "flow": "xtls-rprx-vision",
      "tls": {
        "enabled": true,
        "server_name": "NODE_A_SNI",
        "utls": { "enabled": true, "fingerprint": "chrome" },
        "reality": {
          "enabled": true,
          "public_key": "NODE_A_PBK",
          "short_id": "NODE_A_SID"
        }
      }
    },
    {
      "type": "vless",
      "tag": "proxy-b",
      "server": "NODE_B_IP",
      "server_port": 443,
      "uuid": "NODE_B_UUID",
      "flow": "xtls-rprx-vision",
      "tls": {
        "enabled": true,
        "server_name": "NODE_B_SNI",
        "utls": { "enabled": true, "fingerprint": "chrome" },
        "reality": {
          "enabled": true,
          "public_key": "NODE_B_PBK",
          "short_id": "NODE_B_SID"
        }
      }
    },
    {
      "type": "vless",
      "tag": "proxy-c",
      "server": "NODE_C_IP",
      "server_port": 443,
      "uuid": "NODE_C_UUID",
      "flow": "xtls-rprx-vision",
      "tls": {
        "enabled": true,
        "server_name": "NODE_C_SNI",
        "utls": { "enabled": true, "fingerprint": "chrome" },
        "reality": {
          "enabled": true,
          "public_key": "NODE_C_PBK",
          "short_id": "NODE_C_SID"
        }
      }
    },
    {
      "type": "urltest",
      "tag": "auto",
      "outbounds": ["proxy-a", "proxy-b", "proxy-c"],
      "url": "http://cp.cloudflare.com/generate_204",
      "interval": "1m",
      "tolerance": 100,
      "idle_timeout": "10m",
      "interrupt_exist_connections": true
    },
    {
      "type": "selector",
      "tag": "select",
      "outbounds": ["auto", "proxy-a", "proxy-b", "proxy-c"],
      "default": "auto",
      "interrupt_exist_connections": false
    },
    { "type": "direct", "tag": "direct" }
  ],
  "route": {
    "rules": [
      { "action": "sniff" },
      { "protocol": "dns", "action": "hijack-dns" },
      { "ip_is_private": true, "outbound": "direct" },
      { "rule_set": "ru-domains", "outbound": "direct" }
    ],
    "rule_set": [
      {
        "type": "remote",
        "tag": "ru-domains",
        "format": "binary",
        "url": "https://raw.githubusercontent.com/REPLACE/ru-domains.srs",
        "download_detour": "select",
        "update_interval": "7d"
      }
    ],
    "final": "select",
    "auto_detect_interface": true
  },
  "experimental": {
    "cache_file": { "enabled": true, "path": "cache.db" },
    "clash_api": { "external_controller": "127.0.0.1:9090" }
  }
}
```

Ключевые решения в этом конфиге:

- `interval: "1m"` вместо дефолтных `3m`, `tolerance: 100` (мягче дефолтных 50 — на LTE задержки «прыгают», и tolerance 50 вызывает лишние переключения) **[ВЫВ]**;
- `interrupt_exist_connections: true` — при смене ноды старые соединения рвутся, приложения переподключаются сами. Без этого клиент остаётся на мёртвых сокетах **[ВЫВ]**;
- `idle_timeout: "10m"` — перестаём проверять, когда телефон спит, экономим батарею;
- маршрутизация идёт на `select`, а не на `auto` — пользователь может переключить руками из Clash API.

#### (б) Два протокола на одной ноде + резервный порт (VLESS 443 + VLESS 8443)

Тот же конфиг, но `proxy-a`/`proxy-a-alt` — один сервер, два порта, один UUID:

```json
{
  "type": "vless",
  "tag": "proxy-a",
  "server": "NODE_A_IP",
  "server_port": 443,
  "uuid": "NODE_A_UUID",
  "flow": "xtls-rprx-vision",
  "tls": {
    "enabled": true,
    "server_name": "NODE_A_SNI",
    "utls": { "enabled": true, "fingerprint": "chrome" },
    "reality": { "enabled": true, "public_key": "NODE_A_PBK", "short_id": "NODE_A_SID" }
  }
}
```

```json
{
  "type": "vless",
  "tag": "proxy-a-alt",
  "server": "NODE_A_IP",
  "server_port": 8443,
  "uuid": "NODE_A_UUID",
  "flow": "xtls-rprx-vision",
  "tls": {
    "enabled": true,
    "server_name": "NODE_A_SNI",
    "utls": { "enabled": true, "fingerprint": "chrome" },
    "reality": { "enabled": true, "public_key": "NODE_A_PBK", "short_id": "NODE_A_SID" }
  }
}
```

Группа:

```json
{
  "type": "urltest",
  "tag": "auto",
  "outbounds": ["proxy-a", "proxy-a-alt", "proxy-b", "proxy-c"],
  "url": "http://cp.cloudflare.com/generate_204",
  "interval": "1m",
  "tolerance": 100,
  "interrupt_exist_connections": true
}
```

**Зачем [ВЫВ]:** разные порты на одном IP чаще всего режутся независимо. Если оператор дропает 443 к этому IP (или именно 443 из-за «похоже на VPN»), резервный порт даёт второй шанс **без второй ноды**. Это дешёвая страховка, которую стоит поднять на всех трёх нодах — например 443/tcp (Reality+Vision) и 8443/tcp (Reality+XHTTP) + 2053/tcp (Reality+WS). **Важно:** AmneziaWG-порт 51820/udp — третья, независимая опора, но он же и самый уязвимый (см. §1 и FOCI-контекст).

#### (в) Смешанная цепочка TCP + UDP (Hysteria2) — с предупреждением

```json
{
  "type": "hysteria2",
  "tag": "proxy-udp",
  "server": "NODE_A_IP",
  "server_port": 443,
  "password": "HY2_PASSWORD",
  "obfs": { "type": "salamander", "password": "OBFS_PASSWORD" },
  "tls": { "enabled": true, "server_name": "NODE_A_SNI", "alpn": ["h3"] }
}
```

```json
{
  "type": "urltest",
  "tag": "auto",
  "outbounds": ["proxy-a", "proxy-b", "proxy-c", "proxy-udp"],
  "url": "http://cp.cloudflare.com/generate_204",
  "interval": "1m",
  "tolerance": 100,
  "interrupt_exist_connections": true
}
```

**🔴 Предупреждение [ВЫВ], критично:** `urltest` измеряет задержку **HTTP-запросом по TCP**. Если UDP-нода фактически мертва (оператор режет UDP), но её TCP-порт жив (или проверка идёт через другой транспорт), `urltest` будет считать её здоровой и может выбрать её как «быструю» — и пользователь получит полностью нерабочий интернет.

**Правильные обходы [ВЫВ]:**

1. **Не смешивать** TCP- и UDP-ноды в одной `urltest`-группе. Держать отдельный TCP-only `urltest` для автоматики и отдельный `selector` для UDP — ручной.
2. Либо заводить UDP-ноды вторыми в `selector` за TCP-группой, никогда не давая им стать `default`.
3. Либо проверять UDP-ноду через её же TCP-фолбэк-порт.

С учётом 420-секундной остаточной цензуры QUIC и SNI-фильтрации UDP (FOCI 2026) **[П]** — вариант 1 самый честный.

#### (г) Каскад: РФ-вход (мост) → зарубежный выход

В sing-box каскад делается через `detour` в Dial Fields:

```json
{
  "type": "vless",
  "tag": "bridge-ru",
  "server": "RU_BRIDGE_IP",
  "server_port": 443,
  "uuid": "BRIDGE_UUID",
  "flow": "xtls-rprx-vision",
  "tls": {
    "enabled": true,
    "server_name": "RU_BRIDGE_SNI",
    "utls": { "enabled": true, "fingerprint": "chrome" },
    "reality": { "enabled": true, "public_key": "BRIDGE_PBK", "short_id": "BRIDGE_SID" }
  }
}
```

```json
{
  "type": "vless",
  "tag": "exit-de",
  "server": "EXIT_IP",
  "server_port": 443,
  "uuid": "EXIT_UUID",
  "flow": "xtls-rprx-vision",
  "detour": "bridge-ru",
  "tls": {
    "enabled": true,
    "server_name": "EXIT_SNI",
    "utls": { "enabled": true, "fingerprint": "chrome" },
    "reality": { "enabled": true, "public_key": "EXIT_PBK", "short_id": "EXIT_SID" }
  }
}
```

Плюс fallback напрямую, если мост упал:

```json
{
  "type": "urltest",
  "tag": "auto",
  "outbounds": ["exit-de", "proxy-a", "proxy-b"],
  "url": "http://cp.cloudflare.com/generate_204",
  "interval": "1m",
  "tolerance": 100,
  "interrupt_exist_connections": true
}
```

**Важная асимметрия [ВЫВ]:** если мост (`bridge-ru`) упал, `exit-de` через него не работает, а `urltest` этого не поймёт — он проверит `exit-de` и получит ошибку, переключится на `proxy-a`, и через минуту снова попробует `exit-de`. Это нормально, но добавляет «мигание». Полезно поднять `interval` для каскадной группы или вынести каскад в отдельный `selector` с явным названием «через РФ-мост», чтобы пользователь понимал, что он выбрал.

### 4.4. Clash/Mihomo YAML

```yaml
proxy-groups:
  - name: "auto"
    type: url-test
    proxies: ["node-a", "node-b", "node-c"]
    url: "http://cp.cloudflare.com/generate_204"
    interval: 60
    tolerance: 100
    lazy: false
    expected-status: 204
  - name: "auto-fallback"
    type: fallback
    proxies: ["node-a", "node-b", "node-c"]
    url: "http://cp.cloudflare.com/generate_204"
    interval: 60
  - name: "select"
    type: select
    proxies: ["auto", "auto-fallback", "node-a", "node-b", "node-c", "DIRECT"]
  - name: "lb"
    type: load-balance
    proxies: ["node-a", "node-b", "node-c"]
    url: "http://cp.cloudflare.com/generate_204"
    interval: 60
    strategy: consistent-hashing

proxies:
  - name: "node-a"
    type: vless
    server: NODE_A_IP
    port: 443
    uuid: NODE_A_UUID
    network: tcp
    tls: true
    udp: true
    flow: xtls-rprx-vision
    servername: NODE_A_SNI
    reality-opts:
      public-key: NODE_A_PBK
      short-id: NODE_A_SID
    client-fingerprint: chrome
```

Пояснения к параметрам (по документации Mihomo) **[З — не проверял первоисточник в этой сессии]**:

- `type: url-test` — выбирает самый быстрый, переключается по `interval`;
- `type: fallback` — выбирает **первый живой** по порядку списка (не самый быстрый): предсказуемее для «мобильного» профиля;
- `type: load-balance` — распределяет по стратегии (`consistent-hashing`, `round-robin`);
- `lazy: true` — не проверять группу, пока через неё нет трафика (экономия батареи);
- `expected-status: 204`;
- `interval` — секунды;
- `health-check` — расширенный блок проверки (в новых версиях Mihomo).

**Про AmneziaWG в Mihomo [НН]:** поддержку AmneziaWG в Mihomo я **не подтвердил**. В 3x-ui есть Clash-выход для AmneziaWG (release notes v3.8.0: «Clash subscriptions can emit AmneziaWG proxies») **[П]** — но это описывает генерацию 3x-ui, а не то, что Mihomo это исполнит. **Не полагаться на этот путь без отдельной проверки.**

### 4.5. Xray-нативный fallback: balancer + burstObservatory

**[П]** ([xtls.github.io/config/routing](https://xtls.github.io/en/config/routing.html), [observatory](https://xtls.github.io/en/config/observatory.html)):

```json
{
  "observatory": {
    "subjectSelector": ["proxy-"],
    "probeUrl": "https://www.google.com/generate_204",
    "probeInterval": "10s",
    "enableConcurrency": true
  },
  "burstObservatory": {
    "subjectSelector": ["proxy-"],
    "pingConfig": {
      "destination": "https://connectivitycheck.gstatic.com/generate_204",
      "connectivity": "",
      "interval": "1m",
      "sampling": 10,
      "timeout": "5s",
      "httpMethod": "HEAD"
    }
  },
  "routing": {
    "balancers": [
      {
        "tag": "balancer-main",
        "selector": ["proxy-"],
        "fallbackTag": "direct",
        "strategy": { "type": "leastPing" }
      }
    ],
    "rules": [
      { "type": "field", "network": "tcp,udp", "balancerTag": "balancer-main" }
    ]
  }
}
```

Стратегии **[П]**: `random` (дефолт), `roundRobin`, **`leastPing`** (наименьшая задержка по данным обсерватории), **`leastLoad`** (наименьшая нагрузка).

Ключевые детали из документации **[П]**:

- `subjectSelector` — **префиксное совпадение** по тегам. `["proxy-"]` поймает `proxy-a`, `proxy-b`, `proxy-c`.
- `observatory.probeInterval` — фиксированный интервал, и документация **сама предупреждает**: «since the request interval is fixed, periodic fixed requests might lead to **behavioral fingerprinting**. Using protocols with multiplexing or enabling `mux` can alleviate this issue».
- `burstObservatory` — умнее: на каждом цикле `interval × sampling` задача планируется в **случайный** момент. Это снижает fingerprint. Документация: минимальный `interval` — **10 s** (меньше указанного обрезается до 10 s).
- Восстановление после сбоя: нода помечается мёртвой минимум за 1 цикл (максимум 2), оживает после **одной** успешной проверки.
- В 3x-ui есть UI для настройки: релиз **v3.4.2 (29.06.2026)** добавил «Balancer Observatory» — вкладки Observatory / Burst Observatory, burst observer для стратегий `random`/`roundRobin` с `fallbackTag` **[П]**.

**Поддерживает ли это v2rayNG [П — частично]:** да. В `strings.xml` есть `title_observatory_settings` с полями leastPing Interval / leastLoad Interval / leastLoad Method / Sampling / Timeout, а в `AppConfig.kt` — `TAG_BALANCER = "balancer-main"`, `TAG_BALANCER_PRE = "balancer"` и дефолты (3m / 5m / HEAD / 2 / 30s). Значит v2rayNG **умеет** и balancer, и обсерваторию. **Про Happ — [НН]**: подтверждения, что Happ генерирует `routing.balancers` из своей подписки, я не нашёл.

### 4.6. Рекомендованные интервалы и tolerance [ВЫВ]

| Профиль | interval | tolerance | idle_timeout | Почему |
|---|---|---|---|---|
| Телефон, LTE, экономия батареи | **60-120 с** | **100 мс** | 10 м | Компромисс: до 1-2 мин на мёртвой ноде, трафик/батарея терпимо |
| Телефон, «всегда онлайн» (работа/звонки) | **30 с** | 100 мс | 0 | Быстрее реакция, но ~2 880 проверок/сутки на ноду |
| Ноутбук / стационар | 180-600 с | 50 мс | 30 м | Дефолт sing-box (`3m`/`50`) разумен |
| Каскад через РФ-мост | 180 с | 200 мс | 30 м | Избегаем «мигания» из-за двойной задержки |

- **tolerance меньше 50 мс на LTE брать нельзя** — задержка «плавает» на 30-80 мс сама по себе, и группа будет переключаться каждую проверку **[ВЫВ]**.
- **Что происходит при переключении LTE→Wi-Fi [ВЫВ]:** смена интерфейса рвёт TCP-соединения. sing-box с `auto_detect_interface: true` перепривяжется; mux-соединения и Reality-сессии пересоздадутся. Пользователь увидит паузу на 1-3 с. Это нормально и не лечится настройками клиента.
- **DNS-утечки при проверках [ВЫВ]:** если `url` проверки задан доменом и `download_detour`/`detour` не указан, DNS-запрос на этот домен может уйти напрямую. В конфиге выше DNS поднят в `dns.servers` с `detour: "select"`, а правила `route.rules` начинаются со `sniff` — этого достаточно.

---

## 5. AmneziaWG 2.0/3.1: новые параметры обфускации и MTU под LTE

> **Терминологическая правка к исходной постановке задачи.** «3x-ui 3.7» — это версия **панели**, а не протокола. В 3x-ui **3.7.0 (24.08.2026)** появился **нативный AmneziaWG 3.1** [П]. До этого AmneziaWG в 3x-ui не было **вообще**: запрос «Add amneziawg 2.0 (awg2.0) support» (issue #5113, 09.06.2026) был закрыт как дубликат с ответом, что «3x-ui — панель над xray-core и может поддерживать только то, что реализует сам xray-core… AmneziaWG в xray-core нет» [П]. То есть панель **перешагнула** 2.0 и сразу пришла к 3.1. Ниже — хронология именно **протокола** AmneziaWG.

### 5.1. Хронология версий [П]

Официальная документация Amnezia ([docs.amnezia.org/documentation/amnezia-wg](https://docs.amnezia.org/documentation/amnezia-wg/)) описывает эволюцию так:

| Версия | Что добавила | Когда |
|---|---|---|
| **1.0 (AWG 1.x)** | Базовая обфускация: junk-пакеты (Jc/Jmin/Jmax), префиксы к пакетам (S1/S2), подмена magic-заголовков (H1-H4) | исторически |
| **1.5** | «Obfuscation to the next level»: трафик можно было маскировать под популярные UDP-протоколы (QUIC, DNS и др.) — то есть появились полноценные signature-пакеты | исторически |
| **2.0** | «Full **mimicry**»: динамические **диапазоны** заголовков вместо статических значений, случайные байты в специальных WireGuard-пакетах, расширенные CPS-пакеты перед рукопожатием | анонс [amnezia.org/blog/amneziawg-2-0-available-for-self-hosted](https://amnezia.org/blog/amneziawg-2-0-available-for-self-hosted) (дата на странице не указана — **[НН]**) |
| **3.0** | Линия третья: Header Protection, Content Padding, Random Trailers, DisableCookies, рандомизация таймингов | **конец июля 2026** (по данным стороннего установщика **[З]**) |
| **3.1** | «Разработана в ответ на массовые блокировки в России в июне и июле 2026»: вариация размеров пакетов, последовательностей, таймингов и метаданных; менее узнаваемое поведение cookie (active probing) | тег **`v3.1.20260812`**, 12 августа 2026 **[З]**, подтверждено: [amneziawg-linux-kernel-module tags](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module/tags) → `v3.1.20260906` (06.09.2026) **[П]** |

> **Прямая причина 3.1 [П]** (цитата документации): «Version 3.1 was developed in response to widespread blocking in Russia in **June and July 2026**, which showed that masking individual traffic characteristics was no longer sufficient».

**Ответ на вопрос «чем отличаются от AWG 1.0/1.5 в 3x-ui 3.7»:** в 3x-ui **версия 3.7.0 (24.08.2026)** — это первый релиз с **нативным AmneziaWG 3.1** [П]: «🛡️ **Native AmneziaWG 3.1** — AmneziaWG inbounds run in-process on an embedded userspace device with a reconcile manager, so DPI-resistant peers need **no kernel module, no Docker and no second panel**». До 3.7 в 3x-ui AmneziaWG **не было вообще**: issue #5113 («Add amneziawg 2.0 support», создан 09.06.2026, закрыт как дубликат) содержал ответ бота, что «3x-ui — панель над xray-core и может поддерживать только то, что реализует сам xray-core… AmneziaWG в xray-core нет» [П]. То есть вся обфускация AmneziaWG в 3x-ui появилась **сразу в версии 3.1**, минуя 2.0.

**Актуальная версия на 06.10.2026 — `v3.9.0` (03.10.2026)** [П], а не 3.7. Что добавили 3.8 и 3.9 по нашей теме:

- **v3.8.0 (14.09.2026)** [П]: «🛰️ **AmneziaWG everywhere** — AmneziaWG is now an **outbound** protocol too, **Clash subscriptions can emit AmneziaWG proxies**, multi-server NordLynx outbounds, and the embedded tunnel picked up a dozen relay, MTU, keying, locking and throughput fixes»; плюс «📡 Subscription overhaul — client-app integration headers and curated routing presets, routing profiles and DNS servers baked into JSON subscriptions, a legacy Clash endpoint, a dummy info/status node…».
- **v3.8.5 (16.09.2026)** [П]: «📡 **AmneziaWG relay ports fixed for good** — the loopback relay slot now wraps, survives disabled and peerless rows, and refuses a row its own port; **on databases with a high inbound-id counter the protocol simply never worked before**». Это критично: если панель обновлялась с большой базой, AWG до 3.8.5 мог не работать вовсе.
- **v3.9.0 (03.10.2026)** [П]: «🌐 **AmneziaWG, TUIC & MTProto on nodes** — these inbounds can now be created and cloned from the master straight onto a node»; Xray-core **v26.9.30**; «📦 Subscription controls — hide an inbound from subscriptions without disabling it».

**[ВЫВ] Вывод:** наша панель должна быть **минимум 3.8.5** (иначе AmneziaWG может быть нерабочим на большой базе), лучше **3.9.0** (AWG на нодах + скрытие инбаунда из подписки).

### 5.2. Новые параметры обфускации: полный список

**[П]** по [docs.amnezia.org/documentation/amnezia-wg](https://docs.amnezia.org/documentation/amnezia-wg/) и [3x-ui amneziawg.mdx](https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/config/amneziawg.mdx):

| Параметр | Тип | Смысл | Ограничения |
|---|---|---|---|
| **I1-I5** | `CPS-description` | **Обфускационные пакеты перед рукопожатием** (до пяти, отправляются каждые 120 с). I1 обычно содержит hex-снимок реального протокола (например QUIC Initial). Это основа защиты от **active probing** | Теги: `<b hex>` статические байты (любая длина), `<t>` unix-timestamp, `<r N>` N случайных байт (N ≤ 1000), `<rc N>` случайные буквы, `<rd N>` случайные цифры. Если ни один не задан — CPS не отправляются |
| **S1-S4** | `uint16` | Случайные префиксы к Init / Response / Cookie / **Data** | Итоговые размеры: `148+S1`, `92+S2`, `64+S3`, `payload+S4`. **Должны совпадать на обоих концах** |
| **Jc** | `uint16` | Количество junk-пакетов после I-цепочки | — |
| **Jmin / Jmax** | `uint16` | Размер junk-пакетов | Jmin ≤ Jmax |
| **H1-H4** | `range<uint32>` | Идентификаторы четырёх типов WireGuard-пакетов (Init/Response/Cookie/Data). Заменяют предсказуемые magic-значения, сдвигают смещения Version/Type, меняют reserved-биты | **Диапазоны H1-H4 не должны пересекаться** — иначе ядро/amneziawg-go откажет в устройстве целиком. `H1=1`, `H2=2`, `H3=3`, `H4=4` **отключают** механизм (это стандартные типы WG) |
| **HeaderProtectionKey** | 32-байтовый ключ | ChaCha20-обфускация незашифрованных служебных частей пакета (Init 148 б, Response 92 б, Cookie 64 б, Data 16 б). Nonce — первые 12 байт соответствующего S-префикса | Требует **S1-S4 ≥ 12**. Рекомендуется при этом оставить H1-H4 = 1/2/3/4: тогда кастомные заголовки отключены, тип сообщения скрыт Header Protection |
| **ContentPaddingAddition** | `range<uint16>` | Случайная добавка к транспортным пакетам (WG паддит до кратности 16 — это статистический признак) | Не превышает внутренний MTU |
| **RekeyAfterTime / RekeyTimeout / RejectAfterTime / KeepaliveTimeout / MaxHandshakeAttempts** | `range<uint16>` | Рандомизация таймингов: re-handshake, таймаут рукопожатия, интервал до нового handshake без данных, keepalive, число попыток | Каждое `RekeyAfterTime` < каждого `RejectAfterTime` |
| **RandomTrailers** | `on`/`off` | Случайный хвост к концу пакетов. Для handshake — случайные данные, для transport — нули в конец внутреннего IP-пакета. Размер подбирается с учётом текущего UDP-окна, чтобы не нарушать MTU | **Двусторонний**: приёмник без флага удлинённый пакет **не опознаёт**. Рекомендуется ставить одинаковые `S1-S4` |
| **DisableCookies** | `on`/`off` | Не отправлять Handshake Cookie Reply — убирает DPI-видимый тип сообщения | **Односторонний**, совпадения сторон не требует. Цена: теряется защита WireGuard от флуда рукопожатиями с подменённых адресов |

**Что из этого реально критично [П]:**

- **Совпадать на сервере и клиенте обязаны только `S1-S4` и `H1-H4`.** `Jc`, `Jmin`, `Jmax`, `I1-I5` совпадать **не обязаны** — это отдельные пакеты-обманки, вторая сторона их отбрасывает, а узел без `I1` просто не шлёт их вовсе (подтверждение: [bivlked/amneziawg-installer ADVANCED.md](https://raw.githubusercontent.com/bivlked/amneziawg-installer/main/ADVANCED.md)). **Это снимает большую часть боли совместимости** — можно менять junk-профиль, не перенастраивая клиентов **[ВЫВ]**.
- **`RandomTrailers` — двусторонний и опасный.** Включение только на сервере обрывает связь со **всеми** клиентами, у которых он не включён, «независимо от версии этого клиента» **[П]**. Если включаем — включаем и в подписке/конфигах, и проверяем, что клиент 3.1-совместим.
- **`H1-H4` лучше одним числом, а не диапазоном** — при `randomTrailers: on` широкий диапазон приводит к тому, что транспортные пакеты классифицируются как handshake (ссылка на `amnezia-vpn/amneziawg-go#183` в коде 3x-ui) **[П]**.
- **`S1 + 56 = S2` запрещено** — иначе Init и Response получают одинаковый размер на проводе. `S3 ≠ S2 + 28` — иначе совпадут Response и Cookie **[П]**.
- **`S3/S4` — проклятие старых прошивок.** Keenetic Speedster (5.0.6) выдаёт `invalid H1` на диапазоны и требует **обнулить `S3`/`S4`** и снять `I1`, если рукопожатие проходит, а трафика нет **[З]**.

### 5.3. Дефолты 3x-ui 3.9 (что реально в панели) [П]

Из `frontend/src/schemas/protocols/inbound/amneziawg.ts` и `internal/amneziawg/params.go`:

| Поле | Дефолт в схеме | Что генерирует `GenerateObfuscation31` |
|---|---|---|
| `mtu` | — (**`DefaultMTU = 1420`**, `MIN_MTU = 1280`) | — |
| `subnetIp` / `subnetCidr` | `10.8.1.0` / `24` | — |
| `primaryDns` / `secondaryDns` | `8.8.8.8` / `8.8.4.4` | — |
| `jc` | **5** | `randInt(3, 6)` |
| `jmin` | **10** | `randInt(40, 89)` |
| `jmax` | **50** | `jmin + randInt(50, 250)` |
| `s1` | **30** (0…1552) | `randInt(15, 150)` |
| `s2` | **45** (0…1608) | `randInt(15, 150)`, `S1+56 ≠ S2` |
| `s3` | **10** (0…1636) | `randInt(12, 55)` |
| `s4` | **5** (0…32) | `randInt(12, 27)` |
| `h1-h4` | пусто → 1/2/3/4 | 4 значения, по одному из своей полосы в диапазоне `5…2147483647`, **одним числом** |
| `i1` | пусто | `<r N>`, N = `randInt(32, 256)` |
| `i2-i5` | пусто | пусто (как в генераторе самой Amnezia) |
| `headerProtectionKey` | пусто | base64 32 случайных байта (**всегда генерируется**) |
| `contentPaddingAddition` | пусто | `${cpLo}-${cpLo+randInt(8,40)}`, `cpLo = randInt(8,24)` → суммарно ≤ 64 |
| `rekeyAfterTime` | пусто | `randInt(100,120)` … `+10..40` |
| `rekeyTimeout` | пусто | `randInt(3,6)` … `+1..4` |
| `rejectAfterTime` | пусто | `rekeyHi + randInt(30,60)` … `+30..90` (гарантированно ≥ +30 с от rekey) |
| `keepaliveTimeout` | пусто | `randInt(8,12)` … `+2..8` — **максимум 20 с**, меньше типичного `PersistentKeepalive` 25 с |
| `maxHandshakeAttempts` | пусто | `randInt(15,25)` … `+5..25` |
| `randomTrailers` | **false** | **true** |
| `disableCookies` | **false** | **true** |

**Важные следствия [ВЫВ]:**

- Генератор 3x-ui **всегда** включает `headerProtectionKey`, `randomTrailers` и `disableCookies`. Значит **клиент обязан быть 3.1-совместимым**. Строка из документации 3x-ui: «The 3.1 parameters need a **3.1-capable client**… blanking the 3.1 fields renders a config older clients still understand» **[П]**.
- **Дефолты схемы (`jc: 5`, `jmin: 10`, `jmax: 50`) — не то же самое, что генератор.** Если поле не заполнено, панель подставит 5/10/50; если нажать «Regenerate» — 3-6 / 40-89 / +50..250. Для мобильного профиля по данным стороннего замера меньше junk = не обязательно лучше (см. §5.5).
- **`s4 ≤ 32` — не косметика.** s4-префикс прибавляется к **каждому** транспортному пакету и **никогда не клампится к MTU**. При s4 > 20 тоннель с MTU 1420 начинает фрагментироваться: `EffectiveMTU = max(1420 − s4, 1280)` **[П]**.
- `S1-S4 ≥ 12` обязательно при включённом Header Protection — иначе `IpcSet` отвергнет устройство. Именно поэтому генератор задаёт `s3: randInt(12,55)`, а не от нуля **[П]**.

### 5.4. MTU под LTE: 1280 / 1380 / 1420 — почему

Здесь два лагеря, и оба правы в своих условиях.

**Лагерь «1280» — установщик bivlked (серверный, AWG 2.0/3.1)** [П], [ADVANCED.md, обновлено 24.09.2026](https://raw.githubusercontent.com/bivlked/amneziawg-installer/main/ADVANCED.md):

> «**Зачем:** Сотовые сети (4G/LTE) часто имеют эффективный MTU ниже стандартных 1420 — пакеты фрагментируются или отбрасываются. **iOS строго обрабатывает Path MTU Discovery** и может не установить соединение. **1280 — минимальный MTU для IPv6 (RFC 8200)**, проходит через любую сеть. На скорость влияет незначительно.»

Начиная с v5.7.4 установщик ставит `MTU = 1280` автоматически и в серверный, и в клиентские конфиги.

**MSS-clamp (с v5.17.0)** [П]: правило `TCPMSS --set-mss` в таблице `mangle`, цепочке `FORWARD`, только на SYN, в обе стороны (`-o %i` и `-i %i`). Значения из MTU: **MSS 1240 для IPv4 (MTU−40)** и **1220 для IPv6 (MTU−60)**.

> «**Зачем:** даже при MTU = 1280 крупные страницы и закачки иногда зависают на мобильных операторах, при двойном NAT и в каскаде из двух серверов. Причина — **PMTUD-блэкхол**: когда по пути фильтруется ICMP “Fragmentation needed” (для IPv6 — ICMPv6 “Packet Too Big”), крупные TCP-сегменты с флагом DF молча отбрасываются на туннеле, и соединение “висит” на больших объёмах данных (мелкие запросы при этом проходят).»

**Правило ручного изменения MTU [П]:** `--set-mss` в `PostUp`/`PostDown` менять **вместе** с MTU, останавливая тоннель до правки (иначе старые MSS-правила снимутся по старым значениям). При MTU 1200 → MSS 1160 (IPv4) и 1140 (IPv6).

> «Без этого крупные пакеты перестанут пролезать в туннель, и часть сайтов будет открываться через раз.»

**Ограничение MSS-clamp [П]:** работает **только для TCP**. Видео и QUIC/HTTP3 (UDP) он не затрагивает — если тормозит именно такой трафик, MSS-clamp не поможет.

**Лагерь «1420» — 3x-ui (панельный, `DefaultMTU = 1420`)** [П], [params.go](https://github.com/MHSanaei/3x-ui/blob/main/internal/amneziawg/params.go):

```go
// DefaultMTU is WireGuard/AmneziaWG's usual tunnel MTU on a 1500-byte host
// link, before AmneziaWG's own S4 transport junk is prepended.
const DefaultMTU = 1420

// EffectiveMTU is the admin's value when set, else DefaultMTU minus S4: s4 junk
// is prepended to every transport packet and never clamped against the MTU.
func EffectiveMTU(configuredMTU, s4 int) int {
	if configuredMTU > 0 { return configuredMTU }
	return max(DefaultMTU-max(s4, 0), 1280)
}
```

И документация 3x-ui: «`ContentPaddingAddition` — Kept `<= 64` by the generator **so a 1420-MTU tunnel doesn't fragment**» [П].

**Лагерь «1420 → 1380 при проблемах» — вендор NvoVPN** [З], [nvovpn.com/en/setup/vpn-slow, обновлено 23.09.2026](https://nvovpn.com/en/setup/vpn-slow):

> «In a manual configuration, add `MTU = 1420`… Did not help? Set it to **1380**.» Признак проблемы: «messenger messages go through, but heavy pages and downloads stall halfway».

**Арифметика, чтобы решать самому [ВЫВ]:**

- Хост-линк 1500 байт − IPv4 20 − UDP 8 − WireGuard-заголовок 32 = **1440**. Отсюда классические «1420» (запас на PPPoE 8 байт и на всякий случай).
- IPv6: 1500 − 40 − 8 − 32 = **1420**.
- **1280** — не «оптимум», а **пол**: минимальный MTU, обязанный проходить по IPv6 (RFC 8200) и почти по любому LTE.
- **Каждый байт s4/PersistentKeepalive-DATA съедает заголовок**: при s4 = 27 реальная полезная нагрузка на 27 байт меньше.

**Практическая процедура выбора MTU [ВЫВ] (то, что стоит сделать на каждой ноде):**

1. Начать с **1280** — гарантированно работает на LTE, iOS и через двойной NAT. Это «безопасный дефолт» для клиентов, которых мы не контролируем.
2. Если клиент жалуется на скорость — поднимать ступенями **1380 → 1420**, каждый раз проверяя: (а) `ping -M do -s <MTU−28>` до внешнего адреса через тоннель, (б) открывается ли «тяжёлая» страница целиком, (в) не рвётся ли large download.
3. Убедиться, что `--set-mss` соответствует MTU (MTU−40 / MTU−60). **Это не опционально.**
4. Для **iOS-клиентов** — держать 1280 или проверять особенно тщательно: iOS строго соблюдает PMTUD **[П]**.
5. Помнить: **`ContentPaddingAddition` + `s4` съедают headroom** — если MTU 1420 и хочется большой padding, надо либо уменьшить MTU, либо держать padding ≤ 64.

**Что именно рекомендовать в подписке [ВЫВ]:**

- **Мобильный профиль (по умолчанию для новых клиентов): MTU = 1280**, `PersistentKeepalive = 25`, `s4` из генератора (12-27), `ContentPaddingAddition` ≤ 64.
- **Домашний/Wi-Fi профиль: MTU = 1420** с MSS-clamp на сервере.
- Логика: **MTU — единственный параметр, который нельзя «угадать» со стороны сервера**; 1280 стоит 3-5 % скорости, а неправильный 1420 стоит полностью нерабочего тоннеля **[ВЫВ]**.

### 5.5. Эмпирика по LTE: что реально решает рукопожатие

Замер сентября 2026 на **МТС (Москва)**, зафиксированный в документации установщика bivlked **[З — один источник, методика описана, но данных в открытом виде мало]**:

> «По замеру сентября 2026 на МТС Москва рукопожатие решает **форма маскирующего пакета `I1`**, а количество и размер junk-пакетов (`Jc`, `Jmin`, `Jmax`) на него **не влияют**.»

Диагностический признак, разделяющий две причины, — цитата из того же источника [З]:

- **Причина «порт»:** на сервере **не видно вообще ничего** — `tcpdump` по вашему порту молчит, пира в `awg show` нет. Часть операторов глушит нестандартный UDP-порт, но стабильно пропускает **443/udp** («он выглядит как QUIC/HTTP3»). `--preset=mobile` тут **не помогает** — он меняет маскировку, а не порт.
- **Причина «форма I1»:** пакеты от клиента **приходят**, `endpoint` у пира обновляется, счётчик принятых байт растёт, а **`latest handshake` не обновляется никогда**.

**Рабочие варианты I1 из практики Keenetic** [З], [Discussion #45](https://github.com/bivlked/amneziawg-installer/discussions/45):

```ini
# Вариант 1: просто случайные байты
I1 = <r 64>

# Вариант 2: имитация DNS-ответа (42 байта постоянны, 2 байта — случайный ID транзакции)
I1 = <r 2><b 0x858000010001000000000669636c6f756403636f6d0000010001c00c000100010000105a00044d583737>
```

**🔴 Критично [З]:** префикс `<r 2>` **обязателен**. Без него: содержимое сдвигается на два байта, пакет перестаёт разбираться как DNS (поле ID получает `0x8580`, флаги — `0x0001`, то есть пакет объявляет себя **запросом** вместо ответа), и — вторая причина — все 42 байта становятся постоянными, то есть пакет байт-в-байт одинаков у всех, кто скопировал рецепт. При этом постоянный 42-байтовый блок внутри остаётся и по-прежнему годится как признак для поиска **[З]**.

**Профили junk [П]** ([ADVANCED.md](https://raw.githubusercontent.com/bivlked/amneziawg-installer/main/ADVANCED.md)):

| Preset | Jc | Jmin | Jmax | Когда |
|---|---|---|---|---|
| `default` | 3-6 (случайно) | 40-89 | Jmin + 50..250 | Домашний/проводной интернет, стандартные VPS |
| **`mobile`** | **3** (фиксировано) | 30-50 | Jmin + 20..80 | Мобильные операторы (Tele2, Yota, Мегафон, Таттелеком) |

Контрпример по Jc **[З]**: Discussion #38 (@elvaleto) — на **Таттелеком (Летай)** с `Jc = 4-8` подключение получалось «раза с третьего», после `Jc = 3` заработало сразу. То есть **пресет `mobile` имеет смысл, но это не универсальное лекарство**: по замеру на МТС junk вообще не влиял на рукопожатие.

---

## 6. Роутеры и «твёрдые» клиенты

### 6.1. Keenetic / Netcraze

**[П]** по официальной инструкции Amnezia ([docs.amnezia.org/documentation/instructions/keenetic-os-awg](https://docs.amnezia.org/documentation/instructions/keenetic-os-awg), актуальная на 06.10.2026):

- **AmneziaWG 3.1 нативно поддерживается в KeeneticOS только начиная с версии 5.2 Alpha 11**, доступной через **Канал разработчика (Dev channel)**.
- Инструкция описывает настройку на примере **Netcraze Giga (NC-1012)** с **KeeneticOS 5.1 Beta 0.1**.
- Опубликован список моделей, поддерживающих обновление до 5.2: Orbiter 6 (KAP-630/NAP-630), Stellar 6 (KAP-650/NAP-650), Giga/Hero (KN-1010/1011/1012, NC-1012), Start/Starter (KN-1112/1121, NC-1112/1121), 4G (KN-1213/NC-1213), Launcher (KN-1221/NC-1221), Air (KN-1613/NC-1613), Explorer (KN-1621/NC-1621), Extra (KN-1713/1714, NC-1714), Carrier (KN-1721/NC-1721), Ultra/Titan (KN-1810/1811/1812, NC-1812), Viva/Skipper (KN-1910/1912/1913, NC-1913), DSL (KN-2010), Omni DSL (KN-2011/2012), Duo (KN-2110), Extra DSL/Carrier DSL/Skipper DSL/Speedster DSL (KN-2111/2112/2113, NC-2113), Runner 4G (KN-2210/2211/2212/2213, NC-2212), Hero 4G (KN-2310/2311), **Hero 5G (KN-4110, NC-4110)**, Hopper 4G+ (KN-2312, NC-2312), Giga SE/Hero DSL (KN-2410), Ultra SE/Peak DSL (KN-2510), Giant (KN-2610), Peak (KN-2710), Orbiter Pro (KN-2810), **Skipper 4G (KN-2910)**, **Speedster 4G (KN-2911, NC-2911)**, Speedster (KN-3010/3012/3013, NC-3013), Buddy 4/5/5S/6/6 SE, Voyager Pro (KN-3510), Hopper DSL (KN-3610/3611, NC-3611), Sprinter (KN-3710/3711/NC-3711), Sprinter SE (KN-3712/NC-3712), Hopper (KN-3810/3811/NC-3811), Hopper SE (KN-3812/NC-3812), Challenger (KN-3910/NC-3910), Challenger SE (KN-3911/NC-3911), Racer (KN-4010/NC-4010), Explorer 4G (KN-4910/NC-4910), Giga III (Zyxel), Ultra II (Zyxel).

**Для «раздачи на LTE» (портативная схема с SIM) интересны именно 4G/5G-модели [ВЫВ]:** 4G (KN-1213), Runner 4G (KN-2210…2213), Hero 4G (KN-2310/2311), **Hero 5G (KN-4110)**, Hopper 4G+ (KN-2312), Skipper 4G (KN-2910), Speedster 4G (KN-2911), Explorer 4G (KN-4910). Все они в списке поддерживаемых до 5.2.

**Практические подводные камни [З]** (из практики установщика bivlked):

- **Keenetic 4.x нативно поддерживает AWG 2.0 без доп. пакетов**, но: «если туннель поднимается, но трафик не идёт — проблема в формате `I1`». Рабочие варианты — те же `<r 64>` и DNS-паттерн из §5.5.
- **Keenetic Speedster (прошивка 5.0.6):** старые прошивки **не парсят H1-H4 как диапазоны** (`нижняя-верхняя`) и выдают `invalid H1`. Решение: задать H1-H4 **конкретными числами** — остальные слои (Jc/Jmin/Jmax, I1, S1-S4) продолжают работать. Если handshake проходит, а трафика нет — снизить junk (`Jc=3`, `Jmin=10`, `Jmax=50`), при необходимости убрать строку `I1` и обнулить `S3`/`S4`.
- **Универсальный обход прошивочных ограничений** — userspace-решение **[AWG Manager](https://github.com/hoaxisr/awg-manager)** (Entware, веб-интерфейс; версия **v2.17.4** от 27.08.2026 **[З]**), не зависящее от версии KeeneticOS.

⚠️ **Важное замечание автора установщика [З]:** «Поколение у сторонних проектов указано по документации и релиз-нотам их авторов: их код я читал, но **поведением не проверял ни один**».

### 6.2. OpenWrt + sing-box

**[НН] — прямого первоисточника по «OpenWrt + sing-box + urltest для РФ 2026» я не нашёл за время сбора.** Что известно и подтверждено:

- **mieru официально поддерживает OpenWrt** — в README проекта есть отдельный документ [Client Installation & Configuration - OpenWrt](https://github.com/enfein/mieru/blob/main/docs/client-install-openwrt.md) **[П]**.
- mieru как протокол доступен на роутерах через **OpenWrt-пакет/плагин**, а также через **husi** (форк) + mieru-plugin **[П]**.
- **3x-ui** отдаёт Clash/Mihomo и sing-box JSON — то есть роутерный клиент можно кормить той же подпиской, что и телефон **[П]** (см. §8).

**[ВЫВ] рекомендация для OpenWrt:** sing-box (пакет `sing-box` + `luci-app-sing-box` или HomeProxy) с профилем в формате sing-box JSON из нашей подписки. Прошивки на 128 МБ RAM и слабом CPU (MT7621 и подобные) с Reality+Vision не тянут больше 30-60 Мбит/с; для гигабитных LTE/5G-модемов нужен ARM-роутер (Filogic 830/820, IPQ807x). **Это [ВЫВ], не измерение.**

### 6.3. «Раздача на LTE» и TTL

**Что подтверждено [П]:** существует специализированный инструмент именно под эту задачу — **[ArlenFuture/OpenWrt-TTL-Configurator](https://github.com/ArlenFuture/OpenWrt-TTL-Configurator)** (OpenWrt, конфигуратор TTL). Это фактическое подтверждение, что практика «править TTL при раздаче» в OpenWrt существует и востребована.

**Что известно из механики [ВЫВ, требует проверки на нашем операторе]:** операторы historically определяют раздачу по TTL: пакет, вышедший с телефона, приходит на шлюз с TTL, уменьшенным на число промежуточных хопов. Если телефон раздаёт по Wi-Fi/USB, его пакеты приходят к оператору с TTL, отличным от «нативного» для модема. Правка TTL в роутере (mangle, `TTL --ttl-set`) выравнивает значения. **Конкретных измерений по РФ-операторам 2026 года я не нашёл — [НН].**

**⚠️ Дополнительный риск для 2026 [ВЫВ]:** если «признак раздачи» действительно читается оператором, то в режиме белых списков это может приводить не к «доплате за раздачу», а к **более жёсткому профилю фильтрации**. Проверять индивидуально на каждой симке.

### 6.4. Портативная схема с SIM — что собирать [ВЫВ]

**Вариант «минимум»:** LTE/5G-модем (Huawei E3372 / Quectel) → OpenWrt-роутер (ARM) → sing-box с sing-box JSON из нашей подписки → раздача по Wi-Fi/Ethernet. Плюс: TTL-правка, если оператор её требует.

**Вариант «без прошивки»:** Keenetic 4G/5G (модели из списка 5.2) → нативный AmneziaWG 3.1 как основной, VLESS/Reality — только если прошивка/плагин умеет (см. oговорки) → клиенты в LAN получают интернет без своих VPN.

**Критично для обоих [ВЫВ]:** на роутере **нельзя полагаться на один протокол**. Учитывая, что на мобильных РФ UDP может резаться, роутер должен уметь переключиться на TCP-плечо. Нативно это умеет **только** sing-box (urltest/selector); нативный Keenetic-AWG — нет.

---

## 7. Диагностика на телефоне: «оператор режет» vs «нода упала»

### 7.1. Минимальный набор тестов (по шагам, 5-10 минут)

Принцип: **каждый следующий шаг отсекает один класс причин.** Если шаг N пройден — причина не в том, что проверяет шаг N. **[ВЫВ] на основе §5.5 [З] и физики фильтрации.**

| # | Тест | Команда / действие | Что означает результат |
|---|---|---|---|
| **0** | **Нода жива с другой сети?** | Открыть тот же конфиг с домашнего Wi-Fi другого оператора или через мобильное приложение-«внешний чекер» | ✅ работает → нода жива, проблема в мобильной сети. ❌ не работает нигде → **нода/сервер/конфиг** |
| **1** | **ICMP до IP ноды** | `ping NODE_IP` | ⚠️ ICMP часто дропается ТСПУ тихо, **«нет пинга» ≠ «нода упала»**. Отрицательный результат здесь ничего не доказывает |
| **2** | **TCP-handshake на 443** | Проверка TCP-порта с внешнего чекера (например check-host.net → **TCP port**) | ❌ таймаут → L3-фильтрация (белый список) или порт закрыт. ✅ открыт → идём дальше |
| **3** | **TLS до 443 из мобильной сети** | `curl -v https://NODE_SNI:443` или открыть `https://NODE_IP:443` в браузере | TCP есть, TLS обрывается → **SNI-RST**. Пробуем `record_fragment`/`fragment` (§2) |
| **4** | **Тот же сервер на резервном порту** | TCP-проверка 8443 / 2053 / 2087 | ✅ резервный порт открыт, а 443 нет → оператор режет **порт**, не IP. Переключаем порт в подписке |
| **5** | **UDP-порт AWG с внешнего чекера** | check-host.net → **UDP port** (`NODE_IP:51820`), либо `nc -u -v` / `nmap -sU -p 51820` | ❌ → UDP-порт закрыт для входящих (файрвол) либо оператор режет. Проверять **с сервера** (`tcpdump`), приходят ли пакеты вообще |
| **6** | **UDP/443 (QUIC-подобный)** | Сменить AWG `ListenPort` на **443/udp** и повторить | По практике §5.5: часть операторов глушит нестандартный UDP, но **стабильно пропускает 443/udp** [З] |
| **7** | **MTR до ноды** | MTR/traceroute (см. §7.3) | Обрыв на 2-4 хопе внутри сети оператора → **оператор**. Обрыв на последнем хопе → **сервер/ДЦ**. Обрыв у первого зарубежного хопа → **международный стык/ТСПУ** |
| **8** | **«16 КБ блок»** | Скачать файл >1 МБ по HTTPS напрямую с IP ноды (без VPN) | Открывается, но загрузка встаёт на первых КБ → похоже на «16 КБ блок» **[З, смежное исследование]**. Сервер доступен, но канал режется по объёму |
| **9** | **Серверная сторона** | На сервере: `tcpdump -ni any port 51820`, `awg show` (latest handshake, transfer) | **Это самый сильный тест** — см. §7.2 |
| **10** | **Замер задержки и потерь** | Сравнить `mtr` до ноды и до нейтрального IP (например `1.1.1.1`) | Разница в потерях на первых хопах → **радио/LTE**, а не фильтрация |

### 7.2. Главный разделяющий признак: смотрит ли сервер

Из практики AmneziaWG-установщика **[З]**, но это универсальная логика, применимая к любому протоколу **[ВЫВ]**:

**Сценарий А — «оператор режет» (порт/протокол):**
- на сервере `tcpdump` по порту **молчит**;
- пира в `awg show` нет / в логах Xray нет входящего соединения;
- вывод: до сервера вообще ничего не доехало → фильтрация в сети оператора (L3/L4).

**Сценарий Б — «пакеты доехали, но не собирается сессия» (форма пакетов / активное зондирование):**
- пакеты **приходят**, `endpoint` у пира обновляется, счётчик принятых байт **растёт**;
- но `latest handshake` **не обновляется никогда**;
- вывод: рукопожатие отвергается — либо фильтр режет по форме пакетов, либо сервер не принимает параметры (проверить совпадение `S1-S4`/`H1-H4`).

**Сценарий В — «нода упала»:**
- на сервере вообще нет трафика **ни с одной** сети;
- TCP-проверка порта провалена **всеми** внешними чекерами;
- вывод: сервис/сервер/ДЦ.

**Сценарий Г — «нода жива, но путь плохой»:**
- TCP-порт открывается, TLS проходит, скорость мизерная или соединение встаёт на больших объёмах;
- `mtr` показывает потери на промежуточных хопах;
- вывод: **PMTUD-блэкхол или шейпинг**. Лечится MSS-clamp + MTU (§5.4).

### 7.3. Приложения для диагностики

**Android [З — рекомендации, не проверял каждое приложение в этой сессии]:**

| Приложение | Что даёт |
|---|---|
| **PingTools Network Utilities** | ping, traceroute, MTR, whois, DNS, LAN-скан, порт-скан — «швейцарский нож», работает без root |
| **Termux** | `ping`, `mtr`, `nc -u`, `nmap`, `curl -v`, `openssl s_client` — полный набор, если уметь |
| **Check Host** ([Play](https://play.google.com/store/apps/details?id=com.onuraltun.checkhost)) | Обёртка над check-host.net: проверка доступности с **внешних** точек |
| **DR-NetTools** ([Play](https://play.google.com/store/apps/details?id=pl.dronline.nettools)) | ping/traceroute/DNS/port-scan |
| **NetMan: Network Tools & Utils** ([Play](https://play.google.com/store/apps/details?id=com.eakteam.networkmanager.pro)) | Сетевые утилиты + информация о сети |
| **networkmapper** ([Play](https://play.google.com/store/apps/details?id=biz.exdata.networkmapper)) | LAN/маршруты |
| **TraceMeister** | MTR-подобный трейс |

**iOS [З]:** iNetTools, Network Analyzer, HE.NET Network Tools (ping/traceroute/DNS/whois), Termius (для SSH к серверу). Отдельно: **Streisand/Happ/SFI не показывают MTR** — диагностику удобнее делать с ноутбука в той же точке, раздав интернет с телефона **[ВЫВ]**.

**Веб-инструмент, обязательный в арсенале [П]:** **[check-host.net](https://check-host.net/check-udp)** — умеет **UDP-проверку** (`UDP connect`, «Check UDP connection to any port of any IP or website from different places»), а также **Ping, HTTP, TCP port, DNS**. Это единственный публичный бесплатный способ проверить **UDP-порт** с внешних точек, не имея второй симки.

**Серверная сторона (обязательно для каждого инцидента) [ВЫВ]:**

```bash
# AmneziaWG: приходят ли пакеты и обновляется ли handshake
awg show
tcpdump -ni any udp port 51820 -c 50

# есть ли вообще входящие на порт
ss -lunp | grep 51820
ss -ltnp | grep -E ':(443|8443|2053)'

# Xray: входящие соединения и ошибки TLS/Reality
tail -f /var/log/xray/error.log

# MSS-правила на месте?
iptables -t mangle -S FORWARD | grep TCPMSS
ip6tables -t mangle -S FORWARD | grep TCPMSS
```

### 7.4. Чек-лист «что делать клиенту по телефону» (для инструкции в боте) [ВЫВ]

1. Переключиться между нодами в приложении — работает другая нода? → проблема не в телефоне.
2. Переключиться Wi-Fi ↔ LTE — работает на Wi-Fi? → проблема в мобильной сети.
3. Включить/выключить авиарежим на 10 с — сбросить 4-tuple (важно для 420-секундной остаточной цензуры QUIC).
4. Сменить APN/тип сети (LTE → 3G/5G) — иногда другой профиль фильтрации.
5. Проверить, не включён ли «Приватный DNS»/iCloud Private Relay/второй VPN — конфликты.
6. Если ничего — прислать нам: скриншот ошибки, оператор, город, тип сети, время (UTC), какую ноду и какой порт пробовал.

---

## 8. Подписки: один URL, несколько профилей

### 8.1. Что умеет 3x-ui 3.9 (практика 2026) [П]

**[П]** по официальной документации [3x-ui subscription.mdx](https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/config/subscription.mdx):

| Формат | Путь | Включается | Что отдаёт |
|---|---|---|---|
| **Raw links (base64)** | `subPath` | всегда (если сервер включён) | Список `vless://`, `vmess://`, … ссылок; base64-кодируется, если `subEncrypt` = true |
| **JSON (Xray-json)** | `subJsonPath` | `subJsonEnable` | Полный клиентский конфиг Xray (один объект для одного клиента, **массив для нескольких**) |
| **Clash / Mihomo** | `subClashPath` | `subClashEnable` | Полный Mihomo-совместимый YAML |
| **Mihomo (явный алиас)** | `/mihomo/` | `subClashEnable` | Алиас для полного `subClashPath` |
| **Clash for Windows (legacy)** | `/clash-legacy/` | `subClashEnable` | YAML, урезанный до типов/транспортов/шифров старого Clash-ядра; **исключает** VLESS, Hysteria2, Reality, XHTTP, HTTPUpgrade, Shadowsocks 2022. Если совместимых нод нет — отдаёт **422**, а не неимпортируемый YAML |

**Ключевое ограничение [П]:** в подписку попадают только включённые инбаунды с протоколами **VLESS, VMess, Trojan, Shadowsocks, WireGuard, AmneziaWG, MTProto, TUIC, Hysteria2**, отсортированные по sub-sort index. При этом: **«TUIC и AmneziaWG включаются в raw links и Clash/Mihomo-профили, но исключены из JSON-эндпоинтов; MTProto включается только в raw links»**.

**Контекст для нас [ВЫВ]:** AmneziaWG **нельзя** отдать через JSON-подписку 3x-ui. Значит, если мы хотим один URL с VLESS и AWG, нам нужен либо Clash/Mihomo-эндпоинт, либо raw-links. Первый упирается в непроверенную поддержку AWG в Mihomo (§4.4), второй не даёт автоматики (urltest живёт в профиле клиента, а не в списке ссылок).

**Практический вывод [ВЫВ]:** правильная архитектура — **разные эндпоинты для разных клиентов**:
- **sing-box JSON-подписка (свой шаблон, не штатный 3x-ui JSON)** — для SFA/SFI/Hiddify/Karing; там мы кладём `urltest` + `selector` + routing (§4.3). AmneziaWG в неё не попадёт, поэтому AWG отдаём отдельным `vpn://`/QR-каналом.
- **Mihomo YAML** (`/mihomo/`) — для Clash Meta / FlClash / Karing; там есть AWG от 3x-ui, но без гарантии исполнения.
- **base64 raw** (`subPath`) — универсальный fallback для всего остального; **в нём работает автоматика Happ** (встроенный Ping + автопереключение между серверами из списка).

### 8.2. Заголовки подписки [П]

3x-ui отдаёт стандартные заголовки, которые читают совместимые приложения:

- `Subscription-Userinfo` — `upload`, `download`, `total` (байты; `total=0` = безлимит), `expire` (Unix-секунды);
- `Profile-Update-Interval` — интервал обновления в часах (`subUpdates`, дефолт **12**);
- `Profile-Title`, `Support-Url`, `Profile-Web-Page-Url`, `Announce` — опциональный брендинг.

Настройки сервера подписки: `subEnable` (**по умолчанию включён**), `subPort` = **2096**, `subListen`, `subPath` (**случайный на панель**), `subDomain`, `subCertFile`/`subKeyFile` (если заданы — HTTPS), `subEncrypt` = **true**, `subUpdates` = **12**.

**Важное изменение в v3.8.5 [П]:** встроенная страница профиля **теперь выключена по умолчанию** (`subProfileMode = none`), и заголовок `profile-web-page-url` больше не отправляется, если URL не задан явно. Если клиенты полагались на эту страницу — надо включить режим **Built-in** в *Settings → Subscription*.

**Про Happ-специфику [П]:** Happ понимает те же заголовки (`profile-update-interval`, `profile-title`, `subscription-userinfo`, `support-url`, `profile-web-page-url`, `announce`), плюс умеет принимать их **в теле подписки** — строками вида `#profile-update-interval: 1` перед первой ссылкой. Ограничения: `profile-title` ≤ **25 символов**, `announce` ≤ **200 символов**, интервал в часах, кратен часу. Часть параметров (SOCKS/HTTP inbound auth, конфигурация туннеля, `Provider ID`) требует указания **Provider ID** в подписке.

### 8.3. Клиентские балансировщики прямо в JSON-подписке 3x-ui [П]

С версии **v3.7.0 (24.08.2026)**: «🔗 Subscription output — **client-side balancers in the JSON format**». Реализация: `internal/sub/sub_balancer.go`, `frontend/src/schemas/subBalancer.ts`.

**Стратегии [П]** (`SubBalancerStrategySchema`): `leastLoad`, `leastPing`, `random`, `roundRobin`.

**Что генерируется [П]:** «each enabled balancer is emitted as **one extra config document** whose members are the proxy outbounds of the selected inbounds (**`routing.balancers` + `burstObservatory`**)». То есть 3x-ui **сам** собирает Xray-JSON с балансировщиком и burst-обсерваторией — это ровно §4.5, но без ручной работы.

**Поля формы [П]:** `remark` (≤256), `strategy`, `inboundIds` (≥1), `memberWeights` (map inboundId → вес, отсутствующие = 1.0; используется в `leastLoad`), `sortOrder` (≥1), `enabled`. Управление — **Settings → Sub Balancers**, API `/panel/api/sub-balancers` (GET/POST/DELETE).

**Дефолты обсерватории при некорректных значениях [П]** (из `internal/sub/sub_json_observatory_test.go`): плохой JSON, не-URL `destination`, неверный `interval`/`timeout`, неверная схема `connectivity` — всё **не протекает** в итоговый `burstObservatory`, а заменяется встроенными дефолтами. Это важно: панель защищена от «отравления» клиентского конфига.

**[ВЫВ] Это лучший штатный инструмент 3x-ui для нашей задачи:** включаем Sub Balancer со стратегией `leastPing` на все VLESS-инбаунды всех трёх нод — и клиент, поддерживающий Xray-JSON (v2rayNG, Happ в JSON-режиме), получает автоматику бесплатно. Конфигурацию обсерватории (destination, interval, timeout) задаём через настройки панели.

### 8.4. Как это делает Remnawave (для сравнения) [П]

**[П]** ([remnawave/panel docs/learn-en/templates.md](https://raw.githubusercontent.com/remnawave/panel/refs/heads/main/docs/learn-en/templates.md)):

- Remnawave **определяет клиента по User-Agent** и отдаёт разный формат: браузеру — веб-страницу, клиенту — подписку в его формате.
- Четыре семейства форматов: **Mihomo**, **Base64**, **Xray-json**, **Sing-box**.
- **С версии 2.2.0 можно создавать несколько шаблонов на каждое ядро** и раздавать их разным пользователям/приложениям через **External Squads** или **Routing Rules**.
- Base64 — **fallback**, если клиент не совпал ни с одним специфичным форматом.
- У Mihomo есть «специальные ключи Remnawave» для расширенной подстановки.

**[ВЫВ] Что взять:** идея «несколько шаблонов на ядро + раздача по User-Agent/скваду» — правильная. У 3x-ui такого нет: формат выбирается **путём**, а не по UA. Значит, придётся либо писать свой тонкий слой подписки поверх нашей базы (у нас уже есть `GET /sub/<token>` в боте), либо раздавать клиентам разные URL. **Рекомендую первое** — у нас уже есть своя веб-точка подписки на `WEB_PORT=8090`, и шаблонизацию логичнее делать там, а 3x-ui оставить как источник ссылок.

---

## 9. Ограничения ОС

### 9.1. iOS

**[П]:**

- **Streisand:** минимальная iOS **14.0** (App Store), текущая версия 1.6.76 от 10.09.2026, ядро **Xray v26.09.09**.
- **Happ:** минимальная iOS **15.0**, версия 6.0.0 от 05.10.2026.
- **Karing:** минимальная iOS **15.0**, версия 1.2.25.2802 от 11.09.2026.
- **Hiddify:** минимальная iOS **15.0**, версия 4.0 от 19.02.2026.
- **AmneziaVPN:** минимальная iOS **16.0**, версия 5.0.3 от 21.09.2026. **AmneziaWG (лёгкий):** минимальная iOS **15.0**, версия 3.1.4 от 24.08.2026.
- **sing-box SFI** — экспериментальный iOS/macOS/tvOS-клиент, лицензия GPL-3.0 с оговоркой: «no derivative work may use the name or imply association with this application without prior consent» **[П]**.

**[ВЫВ] Что из этого следует для нас:**

- **Все популярные iOS-клиенты требуют iOS 15+** (AmneziaVPN — 16+). Это отсекает старые устройства; в инструкции для клиентов надо писать «iOS 15 и новее».
- **Fragment в iOS-клиентах не подтверждён** (кроме Happ, у которого fragment передаётся в URI). Если у пользователя iPhone и оператор режет по SNI — рабочий путь один: **параметр `fragment=` в ссылке, которую мы отдаём** (Happ его прочитает), либо **AmneziaWG с правильным `I1`**.
- **Раздельное туннелирование в iOS есть**, но в Happ при JSON-подписке оно задаётся **провайдером**: «JSON subscription may have strictly 0 or 1 routing profile… the routing profile cannot be added manually, copied from another subscription, or imported from the clipboard. It is provided exclusively together with the subscription itself from the provider» **[П]**. То есть **раздельное туннелирование для iOS-клиентов мы обязаны положить в подписку сами** — иначе его у них не будет.
- **Always-on VPN в iOS** реализуется через **On-Demand Rules** в конфигурации `NEVPNManager`/per-app VPN через MDM. Для обычного App Store-приложения стороннего вендора это ограничено; для MDM-управляемых устройств — доступно **[З — общий факт платформы, первоисточник Apple в этой сессии не открывался; Apple-документ упоминался в выдаче: [support.apple.com st_vid10851-agd.pdf](https://support.apple.com/library/APPLE/APPLECARE_ALLGEOS/HT202739/st_vid10851-agd.pdf)]**.

### 9.2. Android 15+ (и OEM-надстройки)

**[П] подтверждено в этой сессии:**

- **SFA (sing-box for Android)** распространяется через Google Play, версия **1.14.2**; репозиторий `SagerNet/sing-box-for-android` (1 345 звёзд, последний коммит 02.10.2026).
- **v2rayNG** требует **API 24+** (Android 7.0+), README проекта.
- **NekoBox** требует **API 21+**, но версия в Google Play с мая 2024 контролируется третьей стороной и **не является open-source** — использовать только APK с GitHub **[П]**.
- **[dontkillmyapp.com](https://dontkillmyapp.com/)** — актуальный рейтинг вендоров, которые «предпочитают батарею правильной работе приложений» **[П]**: **#1 Huawei**, **#2 Xiaomi**, **#3 OnePlus**, **#4 Samsung** (особенно после Android P), далее Meizu, Asus, Ulefone/RugOne, Oppo, Wiko, Lenovo, Vivo, realme, Motorola, Blackview, Tecno, Sony, Unihertz. Внизу — AOSP, Nokia, HTC с нулём. Сайт обновляется, есть публичный API и бенчмарк-приложение.

**[ВЫВ] Что делать нашим клиентам на Android (инструкция в боте):**

1. **Отключить оптимизацию батареи для VPN-приложения** — вручную, для Huawei/Xiaomi/OnePlus/Samsung это обязательно, иначе фоновый туннель будет умирать.
2. **Разрешить автозапуск** (Xiaomi/Huawei/Oppo/Vivo) — иначе после перезагрузки VPN не поднимется.
3. **Зафиксировать приложение в памяти** (не «смахивать» из списка задач) — на MIUI/HyperOS/EMUI это ключевой фактор.
4. **Включить системный «Always-on VPN»** — тогда система сама поднимает туннель и (опционально) блокирует трафик без VPN. Это единственный способ пережить агрессивный киллер процессов **[ВЫВ]**. В AOSP это *Настройки → Сеть → VPN → шестерёнка → Always-on VPN* + галочка «Блокировать соединения без VPN».
5. На HyperOS/MIUI: добавить приложение в исключения «Экономии энергии» **и** в «Автозапуск»; в некоторых сборках есть отдельный список «Разрешить фоновую активность».
6. Учитывать, что **Always-on VPN с блокировкой ломает локальную сеть и часть банковских приложений** — предупредить пользователей.

**[НН]:** конкретных ссылок на Android 15/16 changelog с изменениями для VPN-приложений я в этой сессии не нашёл (developer.android.com отдаёт только навигацию без JS-рендеринга). **Не утверждаю**, что Android 15 что-то поменял именно для VpnService. Известное и подтверждённое — это OEM-оптимизации батареи (dontkillmyapp) и то, что VpnService работает как foreground service с постоянным уведомлением.

### 9.3. Раздельное туннелирование (split tunneling)

**[П]/[ВЫВ] по клиентам:**

| Клиент | Раздельное туннелирование | Где настраивается |
|---|---|---|
| **Happ (JSON-подписка)** | ✅ **только от провайдера** — 0 или 1 routing-профиль, пользователь не может изменить | В подписке (header `routing` или routing-ссылка) **[П]** |
| **Happ (base64/список)** | ✅ пользователь может сам | В приложении, профили маршрутизации **[П]** |
| **v2rayNG** | ✅ правила маршрутизации (domain/ip/port/process/network) + пресеты, включая **Russia Whitelist** | Routing Settings, `routing_settings_*` в strings.xml **[П]** |
| **sing-box SFA/SFI** | ✅ полноценные `route.rules` + `rule_set` | JSON-профиль **[П]** |
| **Karing** | ✅ правило-совместимое (IP-CIDR, DOMAIN-SUFFIX, PROCESS-NAME) | Приложение **[П]** |
| **Hiddify** | ✅ | Приложение/профиль |
| **AmneziaVPN** | ✅ (официальная инструкция [vpn-split-tunneling](https://docs.amnezia.org/documentation/instructions/vpn-split-tunneling)) | Приложение **[П]** |
| **AmneziaWG (лёгкий)** | ⚠️ только `AllowedIPs` | `.conf` |

---

## 10. Топ-выводы и рекомендации

### 10.1. Топ-5 действий

1. **Обновить 3x-ui до 3.9.0 (минимум 3.8.5) и включить Sub Balancer со стратегией `leastPing`.**
   Причина: AmneziaWG в 3x-ui появился только в **3.7.0** (24.08.2026), а **в 3.8.5 (16.09.2026) починили фундаментальный баг relay-портов AwG** — «on databases with a high inbound-id counter the protocol simply never worked before». В 3.7.0 добавлен Sub Balancer в JSON-подписке (`routing.balancers` + `burstObservatory`), в 3.9.0 — AWG/TUIC/MTProto на нодах. Это бесплатная автоматика для v2rayNG и Happ.
   Источники: [release v3.9.0](https://github.com/MHSanaei/3x-ui/releases/tag/v3.9.0), [v3.8.5](https://github.com/MHSanaei/3x-ui/releases/tag/v3.8.5), [v3.7.0](https://github.com/MHSanaei/3x-ui/releases/tag/v3.7.0).

2. **Держать TCP-плечо обязательным и не строить архитектуру на UDP.**
   Причина: FOCI 2026 документирует, что ТСПУ фильтруют QUIC по SNI **на всех UDP-портах** с **остаточной цензурой 420 секунд на 4-tuple** ([petsymposium.org/foci/2026/foci-2026-0010.php](https://petsymposium.org/foci/2026/foci-2026-0010.php)). Плюс смежное исследование: под белыми списками UDP не поднимается ни с каким релеем. AmneziaWG/Hysteria2 — второй эшелон, не первый.

3. **Поднять на каждой ноде резервный TCP-порт (8443/2053) с тем же Reality, и завести его в urltest-группу как отдельный outbound.**
   Самая дешёвая страховка: разные порты на одном IP режутся независимо. Плюс в подписке для sing-box-клиентов поставить `interval: 60s`, `tolerance: 100`, `interrupt_exist_connections: true` (§4.6) вместо дефолтных `3m`/`50`/`false`.

4. **Поставить AmneziaWG `ListenPort = 443/udp` на всех нодах и выставить `I1` в форме DNS-ответа с обязательным префиксом `<r 2>`.**
   Причина: по замеру сентября 2026 (МТС Москва) именно **форма `I1`**, а не junk-параметры, решает, соберётся ли рукопожатие на мобильной сети; и часть операторов глушит нестандартный UDP-порт, но стабильно пропускает 443/udp. `Jc/Jmin/Jmax/I1-I5` **не обязаны** совпадать на клиенте и сервере — это снимает боль раздачи конфигов.
   Источники: [bivlked/amneziawg-installer ADVANCED.md](https://raw.githubusercontent.com/bivlked/amneziawg-installer/main/ADVANCED.md) (обновлён 24.09.2026), [commit 91443f8](https://github.com/bivlked/amneziawg-installer/commit/91443f8076eec1534e3e4c8cd8c6ca32ab92c3d4) (24.09.2026).

5. **Не включать fragment всем подряд; для iOS-клиентов заранее прописать `fragment=` в ссылке, а для Android — оставить тумблер в приложении.**
   Причина: разработчик sing-box прямо пишет, что fragment — против «simple firewalls based on plaintext packet matching» и «should not be used to circumvent real censorship», да ещё «due to poor performance, try `record_fragment` first». Против белого списка по IP fragment **бесполезен**. При этом Happ читает `fragment=` из URI и он приоритетнее глобальных настроек — это единственный реальный способ дать iOS-пользователю fragment без правки приложения.

### 10.2. Что рекомендовать клиентам (матрица «телефон → приложение»)

| Платформа | Первый выбор | Второй выбор | Почему |
|---|---|---|---|
| **Android, обычный пользователь** | **v2rayNG 2.3.10** | Happ 4.7.1 | Xray v26.9.30, **UI для fragment и Observatory**, mux+XUDP, TCP-ping, батч-обновление подписок с авто-тестом и авто-удалением мёртвых, пресет маршрутизации «Russia Whitelist» |
| **Android, нужен AnyTLS/mieru** | **Karing** или **Hiddify 4.1.1** | NekoBox (только как отладка) | Karing: AnyTLS ✅, mieru ✅, Hysteria2 с порт-хоппингом ✅, sing-mux ✅, но **нет автопереключения**. Hiddify: XHTTP + Mieru с iOS 4.0 |
| **iOS** | **Happ 6.0.0** (Flyfrog LLC, [trackId 6504287215](https://apps.apple.com/us/app/happ-proxy-utility/id6504287215) — брать только по прямой ссылке, в поиске есть клоны) | Streisand 1.6.76 / Karing | Happ: fragment из URI, управление подпиской через заголовки, JSON 1:1. **Но авто-переключения по задержке в Happ нет** — автоматику кладём в подписку. Streisand: Xray v26.09.09, минимум лишнего |
| **iOS, нужен AmneziaWG** | **AmneziaWG 3.1.4** (лёгкий) или AmneziaVPN 5.0.3 | — | AWG 3.1 (нужен для параметров 3.1-header) |
| **Роутер** | **Keenetic 4G/5G + нативный AWG 3.1** (KeeneticOS 5.2 Alpha 11+) | OpenWrt + sing-box | Нативный AWG не требует DKMS/Docker; но **не умеет urltest** — резервный TCP не переключится сам |
| **Windows/macOS** | v2rayN / Happ Desktop | Hiddify | — |

### 10.3. Чего мы НЕ знаем (честный список [НН])

1. **Mihomo и AmneziaWG** — поддержка не подтверждена. 3x-ui умеет **генерировать** AWG-прокси в Clash-подписке (v3.8.0), но исполняет ли это Mihomo — не проверено.
2. **Streisand: urltest / fragment / mux** — UI-возможности не подтверждены; из описания приложения видны только протоколы.
3. **SFA/SFI: глобальный UI-тумблер для fragment** — не подтверждён; работает через JSON-профиль.
4. **Happ: поддержка генерации `routing.balancers`** — не найдено. Косвенное подтверждение, что её нет: в документации Happ **[П]** описаны только три вида Ping (ICMP/TCP/via Proxy) и **ручной** выбор сервера, без авто-выбора по задержке и без авто-переключения при обрыве. Если Happ всё же умеет `balancer` через переданный провайдером JSON — это осталось непроверенным.
5. **Точные лимиты fragment (`length ≤ 500`, `delay ≤ 30 мс`)** — цифры только из документации BPB Panel, в коде Xray не проверял.
6. **Android 15/16: изменения именно для VpnService** — не найдено, developer.android.com не читается без JS.
7. **TTL-детект раздачи на РФ-операторах в 2026** — измерений не найдено; подтверждено только существование инструмента правки TTL под OpenWrt.
8. **OpenWrt + sing-box: готовые конфиги под РФ 2026** — первоисточников не найдено.
9. **AnyTLS: схема padding** — детали не проверены.
10. **«Мгновенность» переключения sing-box urltest при обрыве** — семантика параметров известна, но прямого утверждения «переключение только на следующем интервале» в документации нет; это мой вывод из конструкции.
11. **Дата анонса AmneziaWG 2.0** — страница блога Amnezia дату не содержит.

---

## Приложение: сводка версий на 06.10.2026 [П]

| Компонент | Версия | Дата |
|---|---|---|
| Xray-core | **v26.9.30** | 30.09.2026 |
| sing-box (stable) | **v1.14.2** | 24.09.2026 |
| sing-box (alpha) | v1.15.0-alpha.10 | 03.10.2026 |
| 3x-ui | **v3.9.0** | 03.10.2026 |
| AmneziaWG kernel module | v3.1.20260906 | 06.09.2026 |
| Amnezia VPN client | 5.0.3.0 | 21.09.2026 |
| v2rayNG | 2.3.10 | 01.10.2026 |
| Happ (iOS / Android) | 6.0.0 / 4.7.1 | 05.10.2026 |
| Karing (iOS / Android) | 1.2.25.2802 / 1.2.26.2906 | 11.09 / 30.09.2026 |
| Streisand | 1.6.76 | 10.09.2026 |
| Hiddify | 4.1.1 | 05.03.2026 |
| SFA | 1.14.2 | сентябрь 2026 |
| NekoBox for Android | 1.4.2 | 09.02.2026 |
| mieru | v3.38.0 | 24.09.2026 |
