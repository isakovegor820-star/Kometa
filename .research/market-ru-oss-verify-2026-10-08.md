# РФ Telegram-боты + OSS-каркасы: внешняя верификация

**08.10.2026.** Метод: собственные `web_fetch`/`curl` + `web_search`. Внутренние `vpn-tg-market-report-2026.md`, `vpn-telegram-market-2026.md`, `.research/market-remnawave.md` — только заглядка для URL, не верификация.
**Легенда:** ✅открыто и прочитано мной 08.10.2026 · 🟡только из внутреннего документа · ❌НЕ ПРОВЕРЕНО.

## A. РФ-сервисы продажи VPN через Telegram

Каталог [vpnstatus.site](https://vpnstatus.site) ✅ (08.10.2026: 234 рабочих из 339). «БС» — обход белых списков, самоотчёт пользователей.

| Сервис | URL ✅ | Цена/мес | Триал | Устр-в | Кабинет | Оплата | Локации | БС |
|---|---|---|---|---|---|---|---|---|
| DOZOR VPN | [dozor-vpn.net](https://dozor-vpn.net/) · [@dozor_vpn](https://t.me/dozor_vpn) | 199/299; 1 699 ₽/год | 3 дня, без карты | до 10 | ✅ lk.dozor-vpn.net; «Статус серверов» | карты РФ, СБП, Stars, крипта = **4** | 8 | Частично |
| Connecta VPN | [connecta.su](https://connecta.su) | 180 Standart / 250 Premium | 7 дней без карты | сайт «до 10», каталог «3» ⚠️ | ✅ ЛК; возврат 14 дней | карта (1 подтв.) | 5 | Работает |
| XConnect VPN | [web.xconnect-vpn.xyz](https://web.xconnect-vpn.xyz) · [@XConnectVPN](https://t.me/XConnectVPN) | 149/189/399; LTE 49–189; роутер 350; частный 450 (8 тарифов) | нет | до 20 | web-app | ❌ | 20 | Работает |
| StealthSurf | [console.stealthsurf.net](https://console.stealthsurf.net/) | 199/399/449/599/1 699 | нет | безлимит (честн. исп.) | ✅ console | ❌ | 21 | Работает |
| FS VPN | [@fsocietyvpnproject](https://t.me/fsocietyvpnproject); сайта нет | 299; 849/3 мес; 1 599/6 мес | 2 дня | 1–10 | ❌ | ❌ | ~20 серверов/8 | Работает |
| Step VPN | [step-vpn.com](https://step-vpn.com/) | 349; 1 590/6 мес; 2 490/год (=208) | 3 дня | безлимит | ✅ ЛК; возврат 30 дней | карты РФ, СБП, Stars, USDT = **4**; **автосписаний нет** | ❌ | Работает |
| MatadoraVPN | [matadorabot.ru](https://matadorabot.ru/) | 159 (каталог; −30% за год) | 2 дня (каталог: 3) ⚠️ | 2, докупаются (каталог: 1) ⚠️ | ✅ ЛК; поддержка 24/7 | ❌ | 6 | Работает |
| WPN.ME | [wpn.me](https://wpn.me/) · @wpn_me_bot | Free «Начальный» навсегда / Premium 350 | freemium | 10 (free — 1) | ✅ сайт + бот | карта, СБП, Stars = **3** | 30+ | Работает |
| TrubaVPN | [@Truba_VPN](https://t.me/Truba_VPN) | 99 / 199 (VPN+LTE 20 ГБ) | 1 день | безлимит | ❌ | ❌ | 11 | Работает |

**KryptoLet.VPN:** `kryptolet.xyz` → **HTTP 404** 08.10.2026 ❌ (в каталоге 149 ₽).

### Ориентир «99–400 ₽/мес, медиана ~199 ₽» — подтверждается
21 платный «цена от» с главной каталога (✅08.10.2026): 30, 50, 99, 99, 149, 149, 150, 159, 159, 180, 199, 199, 199, 199, 200, 208, 245, 250, 299, 299, 350. **Медиана = 199 ₽** (точно), среднее 184 ₽, диапазон 30–350 ₽; в 99–400 ₽ — **19 из 21**. Оговорка: «цена от» у части — годовой эквивалент (Step VPN 208 при реальных 349 ₽/мес).

## B. OSS-каркасы (README/доки ✅прочитаны)

| Каркас | URL | Факты из первоисточника |
|---|---|---|
| **Remnawave** | [remnawave/backend](https://github.com/remnawave/backend) · [docs.rw](https://docs.rw/features/hwid-device-limit/) | HWID-лимит (опционален, **выключен по умолчанию**); Internal Squads = ACL по inbound'ам; External Squads = override шаблонов и настроек подписки (с v2.2.0); Config Profiles; Templates; **Server-Side Routing** (bridge; офиц. «Demonstration Only \| Not Production-Ready»); Response Rules; webhooks; Telegram OAuth; Rescue CLI; TypeScript SDK; REST API; миграция с Marzban. **Биллинга, промокодов и рефералки нет** — это слой бота |
| **remnashop** | [snoups/remnashop](https://github.com/snoups/remnashop) · [docs](https://remnashop.mintlify.app/docs/en/overview/introduction) | Конфигуратор тарифов (лимиты: трафик/устройства/оба/без; мультивалютность; бесплатный тариф; import/export); промокоды **5 типов награды** (дни, трафик, активация подписки, личная скидка, скидка на след. покупку); рассылки по 6 сегментам (все, по тарифу, с подпиской, без, истёкшие, триал) + медиа/HTML/превью/стоп/удаление; уведомления (истечение, трафик, статус узла, первое подключение, add/remove устройства); триалы (несколько, **платный триал**, авто-отключение при выходе из канала); рефералка **2 уровня** (points или дни); **14 шлюзов** (Stars, YooKassa, YooMoney, Cryptomus, Heleket, CryptoPay, FreeKassa, MulenPay, PayMaster, Platega, RoboKassa, UrlPay, WATA, Valutix); управление устройствами с cooldown и сбросом ссылки; 5 режимов доступа |
| **Marzban** | [Gozargah/Marzban](https://github.com/Gozargah/Marzban) (AGPL-3.0) | Web UI, REST API, multi-node, VMess/VLESS/Trojan/Shadowsocks, мультипротокол на юзера, мульти-inbound на одном порту (fallbacks), лимиты трафика и срока + периодические, подписка для V2ray/Clash/ClashMeta, share-link и **QR**, статистика, TLS/REALITY, админский TG-бот, CLI, вебхуки, бэкап в Telegram. **Нет HWID/лимита устройств и биллинга** |
| **3x-ui** | [MHSanaei/3x-ui](https://github.com/MHSanaei/3x-ui) | Inbound'ы: VLESS, VMess, Trojan, Shadowsocks, WireGuard, **AmneziaWG**, **TUIC v5** (нативный Go QUIC, BBR/NewReno), **Hysteria2**, MTProto (FakeTLS), HTTP, SOCKS, TUN; транспорты TCP/Raw, mKCP, WS, gRPC, HTTPUpgrade, XHTTP + TLS/XTLS/REALITY; fallbacks; per-client: квоты, срок, **IP-лимиты с исключениями доверенных адресов, HWID-лимит**, циклы продления, online, QR/шара/подписки; routing (WARP, NordVPN, PIA, свои правила, балансировщики, chaining; встроенные geosite/geoip + **Russia v2ray rules**); подписочный сервер (raw/JSON/Clash по User-Agent); TG и Discord боты; REST API со scoped-токенами; PWA; SQLite/PostgreSQL; 13 языков; Fail2ban |
| **Solo_bot** | [Vladless/Solo_bot](https://github.com/Vladless/Solo_bot) (CC BY-NC 4.0, перепродажа запрещена) | Мультипанель 3x-ui/Remnawave; подписки (любой срок, триал, продление по тарифу, Happ/Hiddify/v2RayTun, кастом заголовков); лимиты устройств/трафика/серверов; **роутеры** (VLESS-подписка за плату); рефералы (уникальные ссылки, **% или фикс**); **UTM-аналитика** (трекинг, привязка к рефералам/купонам/триалам, конверсии); **воронка продаж** (доп. дни к триалу → оффер; «горячие лиды»); бэкапы, смена домена, проверка доступности, уведомления об аптайме; рассылки по группам с медиа; автобалансировка серверов; админка (бан-лист, купоны, UTM, статистика, импорт из панели); платежи YooKassa/YooMoney/Robokassa/Stars/Heleket/Wata/Kassai |

**Вывод:** стандарт = Remnawave (панель/HWID/сквады) + 3x-ui (протоколы/роутинг) + remnashop **или** Solo_bot (продажи, промокоды, рефералка). Промокоды, рефералка, подарки и UTM — **не в панелях**, а в бот-слое.

## C. Ответы на 4 вопроса

### C1. HWID/лимит устройств: почему «не работает»
✅[docs.rw](https://docs.rw/features/hwid-device-limit/): фича **по умолчанию выключена**, при включении «strictly enforced». **Фолбэка «пропустить без HWID» нет:** если лимит включён и для юзера не отключён, получить подписку **невозможно** без заголовка `x-hwid` — Remnawave вернёт **`404`** (дословно). Фолбэк есть только на *число* устройств (`HWID_FALLBACK_DEVICE_LIMIT`) и отключение лимита **для конкретного юзера**. С Panel **v3.0.0** `x-hwid` валидируется regex `/^[a-zA-Z0-9=-]{10,64}$/`; не подошёл — **заголовок игнорируется целиком** → снова `404`. С v2.7.5 панель отдаёт `x-hwid-active`, `x-hwid-not-supported`, `x-hwid-max-devices-reached`, `x-hwid-limit` для внятной ошибки в клиенте.
**Шлют HWID:** Happ, v2RayTun, Koala Clash, FlClashX, Prizrak-Box, Throne, Shadowrocket, Passwall-OpenWRT, Clash Mi, Karing, Incy, RenoarX, DeskBox. **Не шлют:** Hiddify, Streisand, NekoBox, v2rayNG, SagerNet, Outline, ванильный Clash Verge/Mihomo. У Throne, Shadowrocket, Clash Mi, Karing HWID **выключен по умолчанию** — включается руками.
**Итог:** «не работает» = клиент без HWID при включённом лимите. Фолбэк: per-user off, глобально off либо IP/connection-лимиты (в 3x-ui — IP-лимиты с исключениями доверенных адресов). У remnashop свой фолбэк HWID в доках **не описан**.

### C2. Кто из клиентов НЕ умеет Hysteria2/TUIC
Два независимых источника совпали: ✅[vpnstatus.site/clients](https://vpnstatus.site/clients) (08.10.2026) и ✅[unstore.io](https://unstore.io/discover/best-v2raytun-alternatives) (07.05.2026).

| Клиент | Hysteria2 | TUIC |
|---|---|---|
| **v2RayTun** | ❌ нет | ❌ нет |
| **v2rayNG** | ❌ нет (нативно) | ❌ нет |
| Happ | ✅ | ❌ |
| INCY | ✅ | ❌ |
| Hiddify Next | ✅ | ✅ |
| NekoBox | ✅ | ✅ |
| Karing | ✅ | ✅ |
| Streisand / Shadowrocket | заявлено ✅ | заявлено ✅ ⚠️ |

⚠️ По Streisand/Shadowrocket каталог заявляет поддержку, но есть жалоба пользователя в [issue sing-box-yg#35](https://github.com/yonggekkk/sing-box-yg/issues/35) («Hysteria-2, Tuic-v5 в Shadowrocket не работают») — противоречие, тело issue прочитать не удалось. **Практика:** узкое место — **TUIC**; VLESS/Reality + Hysteria2 покрывают почти всю аудиторию.

### C3. Банки/госуслуги и VPN — подтверждено
✅[НТВ/РИА, 27.04.2026](https://www.ntv.ru/novosti/2979058): Минцифры заявило, что большинство крупных РФ-платформ — **от маркетплейсов и банков до госсервисов** — проверяют подключение и **отказывают при активном VPN**; причина — защита ПДн. В конце марта 2026 Шадаев совещался с Яндекс, VK, Ozon, Wildberries, Сбер и просил ограничить доступ **к середине апреля**; отказ грозил исключением из «белых списков». Подтверждают: ✅[Meduza, 16.04.2026](https://plugin-frontend.meduza.io/feature/2026/04/16/krupneyshie-rossiyskie-servisy-zakryvayut-dostup-polzovatelyam-s-vklyuchennym-vpn), ✅[Коммерсантъ](https://www.kommersant.ru/doc/8551281). Само использование VPN не запрещено.
**Как решают:** FS VPN — «Умный routing: российские сайты (VK, банки, маркетплейсы) идут напрямую» ✅[каталог](https://vpnstatus.site/vpn/fs-vpn); Remnawave **Server-Side Routing** = bridge «`.ru` напрямую через RU-ноду, остальное через DE» ✅[docs.rw](https://docs.rw/learn-en/server-routing) (оговорка «Demonstration Only \| Not Production-Ready»); 3x-ui — свои правила + встроенные geosite/geoip и [Russia v2ray rules](https://github.com/runetfreedom/russia-v2ray-rules-dat). Проверка идёт **на стороне банка по IP/ASN**, поэтому лечится bypass'ом для .ru-доменов, а не сменой протокола.

### C4. Реферальные программы
| Сервис | Условия | Источник |
|---|---|---|
| Connecta | «50% пожизненно от суммы пополнения приглашённого»; сайт: «процент с каждого пополнения… без ограничения по времени» | ✅[каталог](https://vpnstatus.site/vpn/connecta-vpn), connecta.su |
| DOZOR | **25%** с первой оплаты + **25% с каждого продления**; ссылка в ЛК/боте; **вывод от 500 ₽** на карту или СБП | ✅dozor-vpn.net |
| FS VPN | **30%** в рублях в реферальный баланс (гасится подписка) + партнёрка **40%** от дохода по оплатам Stars | ✅каталог (JSON акций) |
| remnashop (каркас) | points или дни, **2 уровня**, статистика | ✅docs |
| Solo_bot (каркас) | уникальные ссылки, **% или фикс**, инлайн/обычные сообщения | ✅README |
| XConnect, VPNHub, Step VPN | «щедрая», без цифр (🟡 Step VPN «месяц мне и ему» — только внутр. документ) | ✅каталог |

**Типовое:** 25–50% от платежей, часто **пожизненно/с продлений**; минимальный вывод 500 ₽ (подтв. только DOZOR). **Срок выплаты не подтверждён ни у одного сервиса.**

## НЕ ПРОВЕРЕНО
- **Подарочные подписки:** нет ни в доках remnashop, ни в README Solo_bot, ни в README 3x-ui/Marzban — стандартом рынка называть нельзя.
- **UTM:** только у Solo_bot; у remnashop не найдена.
- **Автопродление:** подтверждено лишь как **отсутствующее** у Step VPN и Connecta. У DOZOR, Matadora, WPN.ME, XConnect, TrubaVPN — нет данных.
- **Промокоды** у DOZOR, Connecta, Matadora, WPN.ME, TrubaVPN, XConnect — нет данных.
- **Платёжные способы** у Connecta (подтв. только карта), XConnect, Matadora, TrubaVPN, KryptoLet — нет данных.
- **Статус-страницы:** подтверждена только у DOZOR («Статус серверов»); `status.shadowvpn.io` не проверял.
- **KryptoLet.VPN:** `kryptolet.xyz` → 404, сервис по этому URL недоступен.
- **Медиана** — по «цена от» каталога (смесь месячных и годовых эквивалентов), не по прайсам вендоров.
- **Расхождения каталог↔вендор** (Connecta 3 vs 10 устройств; Matadora триал 3 vs 2 дня, устройства 1 vs 2) не разрешены — приоритет сайту вендора.
- Всё, что помечено 🟡, не проверялось: внутренние документы проекта — не внешняя верификация.
