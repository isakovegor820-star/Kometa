# Российские банки, VPN и белые списки: факты на 07.10.2026

**Зачем:** ответить на вопрос «как сделать, чтобы у клиента работали банки, когда мы включаем
свой канал». Итог ревью статьи: `docs/РЕВЬЮ-ХАБР-985674.md`; дизайн: `docs/ОБХОД-БС-ЛЮБОЙ-ГОРОД-LTE.md`, §5.

Обозначения: **[П]** подтверждено источником (официальное заявление/новость), **[Ж]** жалобы/мнения,
**[К]** коммерческое утверждение продавца (конфликт интересов), **[—]** данных не найдено.

---

## 1. Блокируется не «зарубежный IP», а факт включённого VPN

* **[П]** К 15.04.2026 Сбер, Т-Банк, Альфа-Банк и ВТБ перестали пускать в приложения при
  включённом VPN — по требованию Минцифры (совещание в конце марта; угроза исключения из «белых
  списков» и потери IT-аккредитации): [Meduza, 16.04.2026](https://plugin-frontend.meduza.io/feature/2026/04/16/krupneyshie-rossiyskie-servisy-zakryvayut-dostup-polzovatelyam-s-vklyuchennym-vpn),
  [Коммерсантъ, 30.03.2026](https://www.kommersant.ru/doc/8551281),
  [Anti-Malware, 14.04.2026](https://www.anti-malware.ru/news/2026-04-14-111332/49695).
* **[П]** 27.04.2026 Минцифры: российские сервисы, «в том числе банки, маркетплейсы», доступны
  из-за рубежа, режутся именно VPN-сессии — [Meduza](https://plugin-frontend.meduza.io/news/2026/04/27/mintsifry-zayavilo-chto-rossiyskie-servisy-perestali-otkryvatsya-s-vpn-iz-soobrazheniy-bezopasnosti),
  [URA.RU](https://ura.news/news/1053088764).
* **[П]** Банки сами рекомендуют выключить VPN: Райффайзенбанк — в официальной базе знаний
  ([raiffeisen.ru](https://www.raiffeisen.ru/retail/remote_service/information-centr/knowledgebase/konsultaciya_po_onlajn-banku/));
  Т-Банк, Райффайзен, ВТБ — в рекомендациях при проблемах со входом; смена страны даёт
  доп. подтверждение или паузу перевода до 2 суток ([РИА/news.mail.ru, 01.03.2026](https://news.mail.ru/society/69986213/)).
* **[П]** ЦБ (Р. Мухлынов): «когда вы пользуетесь финансовыми услугами, отключайте VPN» —
  [Парламентская газета, 14.11.2023](https://www.pnp.ru/economics/cb-rekomendoval-otklyuchat-vpn-pri-polzovanii-finansovymi-uslugami.html).
* **[П]** Отказы бывают и не из-за VPN: с 03–04.08.2026 сайты ВТБ, Сбера, Альфы, РСХБ, ПСБ,
  «Уралсиба» не открывались в зарубежных Chrome/Safari/Edge из-за SSL-сертификата НУЦ Минцифры
  ([3DNews](https://3dnews.ru/1146202/sayti-rossiyskih-bankov-perestali-otkrivatsya-v-zarubegnih-brauzerah-izza-otziva-sslsertifikatov/),
  [BFM](https://www.bfm.ru/news/614109)).
* **[—]** Официальных заявлений Сбера/ВТБ/Альфы о блокировке именно иностранных IP не найдено.

## 2. IP дата-центра — документированный признак

* **[П]** «IP принадлежит дата-центру, а не мобильной/домашней сети» прямо назван косвенным
  признаком VPN, по которому платформы и банки ограничивают доступ —
  [Anti-Malware, 14.04.2026](https://www.anti-malware.ru/news/2026-04-14-111332/49695),
  [НДН.Инфо, 15.04.2026](https://ndn.info/novosti/558271-pochemu-ozon-banki-i-prilozheniya-perestayut-rabotat-s-vpn/).
* **[Ж]** «Диапазоны VPN-хостеров известны, палятся банками, списки обновляются» —
  [форум «Храни Деньги», 17.07.2025](https://hranidengi.com/threads/bankovskie-prilozhenija-za-rubezhom-polzovatsja-zaxodit-ili-mogut-zablochit.894/page-3).
  Там же: «если шёл через VPN с российским сервером — банк никогда не писал, а напрямую заходить не давали»
  (то есть РФ-сервер помогал) — **[Ж]**, единичный отзыв.
* **[К]** «Sber has long blacklisted data center IPs», «без российского IP доступ ограничен» —
  маркетинг продавцов РФ-прокси: [proxycove](https://proxycove.com/en/blog/proxy-dlya-rossiyskikh-bankov-iz-rubezha),
  [bessy.my](https://bessy.my/en/blog/security/sberbank-onlajn-za-granicej). Независимо не подтверждено.
* **[—]** Подтверждённых банком кейсов блокировки DC-IP нет; про серверные сценарии (боты, парсеры
  выписок) данных нет — у Т-Банка есть токенные API без публичных IP-ограничений
  ([developer.tbank.ru](https://developer.tbank.ru/docs/intro/manuals/self-service-auth)).

## 3. Практика «РФ-IP/РФ-VPS для банков»

* **[П]** Задокументирован обратный сценарий: те, кто за границей, маршрутизируют РФ-сайты и банки
  **напрямую**, а туннель держат для заблокированного (VPS в РФ/OpenWrt + ipset «Russia outside») —
  [Habr/RUVDS, 04.03.2026](https://habr.com/ru/companies/ruvds/articles/1004546/).
* **[П]** Схема relay через Yandex Cloud для обхода белых списков мобильных операторов —
  [Habr, 09.04.2026](https://habr.com/ru/articles/1021160/); там же автор прямо пишет, что
  «Яндекс, Госуслуги, банки — напрямую, без VPN».
* **[—]** Отчётов «вход в банк из РФ через РФ VPS/DC-IP работает, а с зарубежного нет» за 2025–2026
  не найдено; сравнения РФ-DC-IP и зарубежных DC-IP — данных нет.

## 4. Детект VPN самими приложениями

* **[П]** Сигналы: системный флаг VPN, имена интерфейсов (`tun0`, `wg0`), нестандартный MTU,
  серверная проверка GeoIP и баз VPN/прокси — методика Минцифры (анализ «Теплицы», изложение
  [bessy.my, 06.10.2026](https://bessy.my/en/blog/security/sberbank-onlajn-za-granicej));
  QA-инструмент под эти сигналы — [s1mb1o/vpn-detector-android](https://github.com/s1mb1o/vpn-detector-android).
* **[П]** RKS Global (апрель 2026): из 30 популярных российских Android-приложений 22 определяли VPN,
  позднее — все 30, включая приложения Сбера; часть передаёт статус на сервер (APK-анализ, не
  динамический тест) — [НДН.Инфо, 06.05.2026](https://ndn.info/novosti/561015-rossijskie-prilozheniya-proveryayut-smartfony-na-vpn-chto-oni-mogut-uznat/).
* **[П]** Play Integrity используется банковскими приложениями (Альфа-Банк 12.43.03) — это
  root/целостность, не VPN — [отчёт, 20.12.2025](https://github.com/PrivSec-dev/banking-apps-compat-report/issues/835).
* **[Ж]** Заглушки «Похоже, вы используете VPN. Отключите его», Access Denied, 403 —
  [iphones.ru, 22.04.2026](https://www.iphones.ru/iNotes/pochemu-rossiyskie-prilozheniya-perestali-otkryvatsya-za-granicey-i-pri-chyom-tut-blokirovka-vpn-razbiraemsya).

## 5. Готовые решения и списки

* **[П/сообщество]** 1900+ российских доменов для раздельного туннелирования под банки, Госуслуги
  и маркетплейсы, автообновление, форматы Amnezia — [lib4u/amnezia-tunneling-ru](https://github.com/lib4u/amnezia-tunneling-ru).
* **[П]** Per-app и IP-сплиты доступны в AmneziaWG/WireGuard (`AllowedIPs`), v2rayNG, Shadowrocket,
  Hiddify. На iOS per-app для сторонних VPN нет — только правила в конфиге.
* **[К]** Продавцы «РФ-прокси» под Сбер/Т-Банк/ВТБ/СБП: proxycove, bessy.my, vpn-russia.app.

## 6. Состав белых списков по банкам

* **[П]** В «белых списках» были ВТБ, Альфа-Банк, ПСБ, МТС Банк, Газпромбанк; Сбера и Т-Банка там
  не было ([БанкИнформСервис, 08.04.2026](https://bankinform.ru/news/141250)); Госдума требовала
  расширить список ([Политсовет, 08.05.2026](https://politsovet.ru/print:page,1,87406-v-gosdume-trebuyut-rasshirit-belye-spiski-bankov-pri-sboyah-interneta.html)).
* **[П]** Сбер фактически стал доступен при отключениях с 01.06.2026
  ([vbr.ru](https://www.vbr.ru/novosti/tehnologii/2026/06/01/sberbank-voshel-v-belii-spisok-bankov/)).
  Список — переменная, а не константа.

## 7. Регуляторный риск для нас

* **[П]** «Антифрод 3.0» (Минцифры, 30.09.2026) вводит реестр РКН для клиентов российских хостеров,
  размещающих VPN/прокси: попадание = год без новых контрактов у всех хостеров —
  [The Moscow Times, 01.10.2026](https://ru.themoscowtimes.com/2026/10/01/v-rossii-nachnut-nakazivat-vladeltsev-serverov-za-razmeschenie-vpn-a207531),
  [Meduza, 04.10.2026](https://meduza.io/slides/vlasti-hotyat-vydavit-vpn-s-rossiyskih-serverov-privyazat-esim-k-trubke-i-polozhit-esche-bolshe-dannyh-v-paket-yarovoy).

## 8. Выводы для проекта

1. **Банк — это не задача маршрутизации, а задача детекта.** Российский IP на выходе не спасает,
   если приложение видит сам факт туннеля. Обещать клиенту «банк будет работать через наш канал» нельзя.
2. **Правильная политика:** банки и другие ru-сервисы — DIRECT (per-app на Android, правила
   в конфиге на iOS), плюс выключение туннеля в одно действие и понятная инструкция.
3. **РФ-плечо** (туннель → РФ-выход) — гипотеза с контрсигналом: IP дата-центра назван признаком
   VPN. Пилотировать только под небанковские сервисы и только с замером.
4. **Не ссылаться на продавцов РФ-прокси** как на источник: их тезисы — коммерческие.
