# Категория B: криптоплатёжные посредники и приём крипты без посредника

**Проект:** Telegram-бот «Kometa» — подписки на VPN, аудитория РФ, чеки 199–1590 ₽/мес.
**Дата проверки:** 2026-10-05 (все данные проверялись в этот день; даты публикации источников указаны отдельно).
**Особенность заказчика:** основателю 15 лет → ИП/ООО/самозанятость на него недоступны; платёжную часть может оформить родитель как самозанятый (НПД 6%) или как физлицо. Бюджет минимальный.

**Методика.** Приоритет — первичные источники (официальные страницы тарифов, ToS, help-центры, PDF-договоры, официальная документация API). Где первичный источник недоступен (Cloudflare/JS/401) — использован вторичный и это явно помечено. Цифры, которые не удалось подтвердить загруженной страницей, помечены как **«не подтверждено»**. Расхождения источников отмечаются отдельно.

> ⚠️ **Главный юридический факт, который меняет всю картину.** С **1 сентября 2026 года** в РФ запрещено использовать цифровые валюты и цифровые права «в качестве средства платежа, встречного предоставления или иной формы оплаты товаров, работ, услуг» (Федеральные законы от 04.08.2026 № 282-ФЗ и № 283-ФЗ). Исключения — внешнеторговые договоры резидент↔нерезидент, оплата ценных бумаг и операции с другими цифровыми валютами. С **1 июля 2027 года** банки обязаны отказывать резидентам в переводах в пользу «неуполномоченных получателей» — лиц, подозреваемых в незаконном обмене криптовалюты без включения в госреестр. ([consultant-plus.ru](https://consultant-plus.ru/company/news/opublikovan-novyj-zakon-o-kriptovalyute-i-tsifrovyh-pravah/))
> Практический смысл для «Kometa»: **приём крипты за VPN-подписку у резидентов РФ с 01.09.2026 — это расчёт цифровой валютой за услугу, то есть прямо запрещённая конструкция.** Крипта остаётся рабочей только как (а) платёж нерезидента, (б) «зачисление на баланс» без оформления как средства платежа — серая зона, (в) способ хранения/конвертации, а не приёма оплаты. Это не отменяет технической части исследования, но должно быть решением юриста, а не разработчика.

---

## Сводная таблица-обзор

| Сервис | VPN-политика | Комиссия приёма | KYC / юрлицо | Рекуррент | Вывод | Итог для «Kometa» |
|---|---|---|---|---|---|---|
| **Crypto Pay (CryptoBot)** | нет запрета (VPN не в списке) | **3%** при обороте до $10k/30дн, до 2,5% при $100k | KYC может быть запрошен; физлицо ок | нет API-подписки; подписки 5% только для каналов/чатов | крипта на внешний кошелёк, комиссия сети | ✅ **основной** (уже подключён) |
| **CryptoCloud → Trybit** | нет запрета (VPN не в списке) | **1,9%**, от 0,4% индивидуально; +$1,40 в Tron | **KYC/KYB не требуется, физлицо**, стран без ограничений | не подтверждено | без холдов, мин. $10, 0% сервис + сеть | ✅ **лучший внешний шлюз** |
| **Plisio** | не запрещено (серая зона) | **0,5%** | «No proof of identity needed»; KYC через SumSub по риску | не подтверждено | массовые выплаты без лимита суммы | ✅ **дёшево, но проверять** |
| **Cryptomus** | не подтверждено | от **0,4%** (2% новым) | ⚠️ с янв. 2026 обязательный KYC, CIS в списке KYC отсутствует | не подтверждено | заявлено авто-вывод | ⚠️ **высокий риск, не рекомендуется** |
| **CoinGate** | **VPN и Proxy — отдельные категории в каталоге мерчантов** | **1%** | due diligence, документы по запросу | есть продукт Billing | стандартные сеттлменты | ⚠️ хорош по политике, нужен юрсубъект |
| **CoinPayments** | нет запрета (VPN не в списке) | **от 3%** (новая платформа) | AML/KYC-фреймворк, ISO 27001 | не подтверждено | батчинг выводов | ⚠️ дорого |
| **Confirmo** | не подтверждено | 0,8% + 0,5% вывод *(вторичный источник)* | по данным источников — только фирма | не подтверждено | фиат EUR/USD/CZK | ⚠️ средняя уверенность |
| **CoinsPaid** | не подтверждено | не подтверждено (продажи по запросу) | EU-компания, высокий комплаенс | не подтверждено | не подтверждено | ❌ вычеркиваем |
| **ALFAcoins** | не подтверждено | **0,99%** + сеть; payout +0,99% | **только юрлица** («all legal business entities») | не подтверждено | вывод только криптой | ❌ физлицу недоступен |
| **NOWPayments** | VPN не запрещён, но | **1%** | ⚠️ **РФ прямо в списке запрещённых юрисдикций** | есть продукт Subscriptions | — | ❌ **вычеркиваем** |
| **BitPay** | не подтверждено | не подтверждено | 18+, US-компания, CIP/KYC | не подтверждено | банк US | ❌ вычеркиваем |
| **OpenNode** | high-risk по запросу | конвертация 0%, вывод 1% on-chain | US-компания, Patriot Act CIP | не подтверждено | банк USD/EUR/GBP/…, **RUB нет** | ❌ вычеркиваем |
| **Trocador** | — (не шлюз) | 0% сверху (берёт долю fee биржи) | нет KYC у большинства партнёров | — | — | 🔧 инструмент покупателя |
| **BestChange / P2P** | — | наценка обменника | у обменников свой KYC | — | — | ⚠️ юридические риски |
| **Своя проверка: USDT TRC20 + TronGrid** | — | 0% сервису, только сеть | нет | пишем сами | на свой кошелёк | 🔧 технически реально |
| **Своя проверка: BTC xpub (Esplora/Blockchair)** | — | 0% сервису | нет | пишем сами | на свой кошелёк | ⚠️ xpub-эндпоинта нет |
| **Своя проверка: TON + TONAPI** | — | 0% сервису | нет | пишем сами | на свой кошелёк | 🔧 free tier 1 RPS |

---

## 1. Telegram Crypto Pay / CryptoBot (уже подключён)

Сервис — платёжная система на базе `@CryptoBot`, работает как кастодиальный кошелёк внутри Telegram. Документация переехала с `help.crypt.bot` на `help.cr.bot` / `help.send.tg`.

1. **VPN-политика — нет запрета (серая зона по формулировке).** В Crypto Pay Policy перечислены запрещённые категории: «narcotics, weapons, gambling, pornography, pirated content, phishing services, cash-out schemes, sale of personal data, etc.» — VPN/прокси/анонимайзеров в списке нет ([help.cr.bot/en/articles/12594929-crypto-pay-policy](https://help.cr.bot/en/articles/12594929-crypto-pay-policy), обновлено 16.10.2025). Но п. 8.1 даёт право «suspend the operation of a specific integration, as well as block the Partner's account or its related balance» по широкому набору оснований, а п. 8.3 — «may withhold or confiscate funds if it is determined that they were obtained through prohibited or fraudulent activity». То есть запрета нет, но дискреция максимально широкая.
2. **Комиссии.** Официальная страница «Fees and Limits Crypto Pay» (от 18.11.2024): комиссия зависит от оборота приложения за последние 30 дней — **0 USD → 3%**, 10 000 USD → 2,9%, 25 000 → 2,8%, 50 000 → 2,7%, 75 000 → 2,6%, 100 000 USD → 2,5% ([help.cr.bot/en/articles/9820010](https://help.cr.bot/en/articles/9820010-fees-and-limits-crypto-pay)). **Расхождение источников:** многие обзоры пишут «Crypto Pay бесплатен / 0%», но официальная страница тарифов даёт 3% на старте. Для «Kometa» с оборотом в первые десятки тысяч рублей корректно планировать **3%** (в TSV зафиксировано 3% как базовая ставка; «0%» встречается только в маркетинговых материалах третьих лиц и на официальной странице не подтверждается).
   - Депозит в свой кошелёк: «There are no commissions for deposit to your wallet. There are also no limits on the amounts you can keep in your wallet» ([help.cr.bot/en/articles/9717243](https://help.cr.bot/en/articles/9717243-fees-and-limits), 22.01.2025); возможен минимальный депозит по конкретной монете.
   - Вывод: «The amount available for withdrawal shown at the top of the form is displayed with the withdrawal fee included» — то есть списывается сетевая комиссия; переводы между пользователями Crypto Bot через чеки — без комиссии за вывод ([help.cr.bot/en/articles/9717238](https://help.cr.bot/en/articles/9717238-how-do-i-withdraw-coins-to-another-wallet)).
   - Подписки на приватный канал/чат (отдельный механизм, не API): **5% включены в цену подписки**, минимум **$1/мес**, максимум **$5 000/мес** ([help.cr.bot/en/articles/9820192](https://help.cr.bot/en/articles/9820192-fees-and-limits-subscriptions)).
   - P2P-маркет: по вторичному источнику комиссия ~0,4% с продавца — **официально не подтверждено** ([ton-adoption.xyz](https://ton-adoption.xyz/blog/crypto-bot-gayd-platezhi-v-telegram-2026/)).
3. **KYC/AML мерчанта.** В политике прямо: «The Payment System may require KYC verification of the Partner and/or its users» (п. 4.4). Юрлицо не требуется — приложение создаётся из Telegram-аккаунта. Депозит может быть заморожен по AML: «the transaction came from an address or wallet associated with high-risk sources»; при подтверждении источника, но высоком риске — «the deposit will be rejected and you will be given the option to withdraw the funds to an external wallet»; адрес после этого переиспользовать нельзя ([help.cr.bot/en/articles/12594783](https://help.cr.bot/en/articles/12594783-why-was-my-deposit-locked-for-aml-review), 22.07.2026).
4. **Монеты и автоконвертация.** Инвойсы: `USDT, TON, BTC, ETH, LTC, BNB, TRX, USDC`. **Можно выставлять счёт в фиате, включая RUB** (`currency_type: fiat`, `fiat: RUB`), с `accepted_assets` — набором монет, которыми покупатель может оплатить. Есть `swap_to` (попытка автоконвертации в USDT/TON/TRX/ETH/SOL/BTC/LTC, «the swap is not guaranteed») и поля `is_swapped`, `swapped_output` ([help.cr.bot/en/articles/10279948](https://help.cr.bot/en/articles/10279948-crypto-pay-api), обновлено 19.03.2026). Это удобно: цена в рублях, оплата любой монетой.
5. **Рекуррент/автопродление — нет.** В API есть только `createInvoice`, `deleteInvoice`, `createCheck`, `transfer`, `getInvoices`, `getBalance`, `getExchangeRates`, `getCurrencies`, `getStats`. Метода подписки/рекуррента нет; автопродление для бота реализуется своим напоминанием + новым инвойсом. (Для каналов/чатов есть отдельный механизм Subscriptions с 5%, но он не про бота.)
6. **Интеграция — сложность 1–2/5.** REST `https://pay.crypt.bot/api/<method>`, токен в заголовке `Crypto-Pay-API-Token`, тестнет `@CryptoTestnetBot` → `https://testnet-pay.crypt.bot/`. Вебхуки с HMAC-SHA256-подписью `crypto-pay-api-signature`; в API 1.5.2 (18.03.2026) ретраи расширены до «17 attempts over 3 days», при полном провале вебхуки автоматически отключаются. Есть библиотеки: Python `aiosend`, `aiocryptopay`; Node `crypto-bot-api`; PHP, Go, .NET ([help.cr.bot/en/articles/10279948](https://help.cr.bot/en/articles/10279948-crypto-pay-api)).
7. **Выплаты.** Баланс приложения выводится на внешний кошелёк (криптой), комиссия сети; лимиты: минимум/максимум перевода «roughly correspond to 1-25000 USD». Банковского/fiat-вывода нет.
8. **Риски.** (а) **Кастодиальность:** сервис не выдаёт seed-фразу, при блокировке аккаунта доступ к балансу зависит от оператора (вторичный источник: [ton-adoption.xyz](https://ton-adoption.xyz/blog/crypto-bot-gayd-platezhi-v-telegram-2026/)). (б) **Санкционные источники средств.** Официально не принимаются депозиты/выводы, связанные с подсанкционными сервисами; перечислены: **Garantex, Grinex, Bitpapa, Suex, Chatex, Cryptex, PM2BTC, HTX (бывш. Huobi), Rapira, ABCEX, Exnode, USDKG, EXMO, arXiv, Aifory Pro, Nobitex, Wallex, Bitpin, Ramzinex** ([help.cr.bot/en/articles/16034433](https://help.cr.bot/en/articles/16034433-does-crypto-bot-accept-transactions-from-any-crypto-service), 22.07.2026). Это критично: часть этих сервисов — как раз популярные у россиян способы купить крипту за рубли, и платёж из Garantex/Bitpapa/HTX приведёт к заморозке депозита. (в) Отказ в обслуживании по VPN-основанию официально не описан, но право на блокировку и конфискацию закреплено.
9. **Актуальность и гео.** `Unavailable regions in Crypto Bot` (обновлено «this week», т.е. около 2026-10-05): Гонконг, Бельгия, Куба, Франция, Иран, Сирия, Индия, Япония, КНДР, Малайзия, Сингапур, Китай, США, Багамы, Канада, Нидерланды, Великобритания, Ирландия, Бангладеш, Боливия, Абхазия, **Крым, Донецкая и Луганская области**, Узбекистан, Мальта. **Россия в списке отсутствует** — сервис для резидента РФ формально доступен; исключены только Крым и «Л/ДНР» ([help.cr.bot/en/articles/12460759](https://help.cr.bot/en/articles/12460759-unavailable-regions-in-crypto-bot)).

---

## 2. CryptoCloud (с 2026 — Trybit)

**Важно:** в мае–июне 2026 CryptoCloud провёл ребрендинг в **Trybit**; сайт `cryptocloud.plus` и `trybit.com` отдают один и тот же продукт, база знаний — `support.trybit.com`. Ребрендинг подтверждён официальным блогом ([cryptocloud.plus/blog/obnovlenie-cryptocloud-05-2026](https://cryptocloud.plus/blog/obnovlenie-cryptocloud-05-2026)) и прессой ([markets.businessinsider.com](https://markets.businessinsider.com/news/currencies/cryptocloud-rebrands-as-trybit-and-sets-sights-on-the-global-crypto-payments-market-1036281700)).

1. **VPN-политика — нет запрета.** В официальном списке запрещённых направлений: фишинг, сбор средств на военные нужды, финансовые пирамиды, darknet-маркеты, запрещённый видеоконтент, финансирование терроризма, отмывание денег ([support.trybit.com/ru/general-information/connection-requirements](https://support.trybit.com/ru/general-information/connection-requirements)). VPN/прокси не упомянуты; на сайте отдельно перечислены «Гэмблинг», «Платформы для взрослых» как обслуживаемые вертикали — то есть high-risk сервисы принимаются ([cryptocloud.plus/ru/pricing](https://cryptocloud.plus/ru/pricing)).
2. **Комиссии.** Базовый тариф — **1,9%** за обработку платежа, **0% за вывод**, без ежемесячных платежей, подключение бесплатно; индивидуально — **от 0,4%** для крупного оборота ([cryptocloud.plus/ru/pricing](https://cryptocloud.plus/ru/pricing)). Дополнительно: **трансферная комиссия Tron — фиксированно 1,4 USD**, Ethereum — динамически; для остальных сетей не взимается; **не взимается при оплате через Web3-кошелёк/WalletConnect** ([support.trybit.com/ru/general-information/fees](https://support.trybit.com/ru/general-information/fees)). При выводе с баланса дополнительно вычитается сетевая комиссия.
3. **KYC/AML мерчанта — не требуется, юрлицо не нужно.** Дословно: «Нет, верификация личности (KYC) не является обязательным требованием для работы с Trybit… для подключения достаточно быть физическим лицом, независимо от гражданства»; «Нет, Trybit работает по всему миру без ограничений по странам» ([support.trybit.com/ru/general-information/connection-requirements](https://support.trybit.com/ru/general-information/connection-requirements)). Требования к проекту: собственный домен и платный хостинг **либо работающий Telegram-бот**; наличие политики конфиденциальности, оферты, условий оплаты и возврата; проверка проекта до 24 часов. Это ключевой пункт: **для Telegram-бота домен не обязателен.**
4. **Монеты и автоконвертация.** Более 40 валютно-сетевых пар: BTC, LTC, ETH (ERC20/Base/Arbitrum/Optimism), TRX, TON, BNB, SOL, **USDT (TRC20, BEP20, TON, ERC20, Arbitrum, Optimism, SOL)**, USDC, DAI, PYUSD, USDD, XAUT и др. ([cryptocloud.plus/ru/pricing](https://cryptocloud.plus/ru/pricing)). Заявлена «Защита от волатильности — Автоматическая конвертация поступающих платежей в стейблкоины».
5. **Рекуррент — не подтверждено.** В базе знаний нет описания подписок/рекуррентных списаний; есть автовывод, массовые выплаты и API. Автопродление придётся делать на своей стороне.
6. **Интеграция — сложность 2/5.** Методы: API, Host2Host (White Label, без редиректа на сторонний домен), CMS-плагины, HTML-виджет ([support.trybit.com/integration/methods](https://support.trybit.com/integration/methods)); тип проекта «Telegram-бот» выбирается при создании и не меняется ([support.trybit.com/ru/getting-started/add-project](https://support.trybit.com/ru/getting-started/add-project)).
7. **Выплаты.** Дословно: «Trybit не удерживает средства — они доступны к выводу сразу после подтверждения платежа в сети, **без резервов и заморозок**»; минимальная сумма вывода — **от 10 USD**; только на сохранённые и подтверждённые адреса; обработка обычно до 5 минут, редко до часа ([support.trybit.com/ru/withdrawals/withdrawal-request](https://support.trybit.com/ru/withdrawals/withdrawal-request)). Есть правила автовывода по сумме/расписанию с подтверждением по 2FA ([support.trybit.com/ru/withdrawals/auto-withdrawal](https://support.trybit.com/ru/withdrawals/auto-withdrawal)).
8. **Риски.** (а) Заявлена «AML-проверка входящих платежей», холодное хранение и «вывод только на доверенные адреса — защита от заморозок» ([cryptocloud.plus/ru/pricing](https://cryptocloud.plus/ru/pricing)) — то есть входящий платёж с «грязного» адреса может быть остановлен. (б) Сервис оставляет право деактивировать аккаунт при нарушении правил или жалобах клиентов мерчанта; деактивация может быть временной ([support.trybit.com/ru/general-information/connection-requirements](https://support.trybit.com/ru/general-information/connection-requirements)). (в) Юрисдикция/регуляторный статус в открытых источниках не подтверждён; ребрендинг в 2026 — сигнал перестройки бизнеса, требует наблюдения.
9. **Актуальность.** Максимальная: официальная база знаний — GitBook, страницы обновляются, суммы и сети сверены на 2026-10-05.

---

## 3. Plisio

1. **VPN-политика — не запрещено (серая зона).** В Terms of Use нет ни слова VPN/proxy/anonymizer; запрещённое описано общими формулировками («Services is strictly prohibited» — про IP, «prohibited by law or court order»). Есть блок export controls: нельзя быть лицом под санкциями US Treasury/UN/EU и резидентом Кубы, Ирана, КНДР, Судана, Сирии ([plisio.net/docs/plisio-terms-of-use.pdf](https://plisio.net/docs/plisio-terms-of-use.pdf)). При этом на главной прямо перечислены обслуживаемые вертикали, включая **«High Risk»** и **«Adult»** ([plisio.net](https://plisio.net/)).
2. **Комиссии.** «No monthly, setup or hidden fees. We charge only one fee of **0.5%**» ([plisio.net](https://plisio.net/)). Массовые выплаты: «Pool together up to 1000 transactions saving 80% on fees… **No limits for payout amount**».
3. **KYC/AML мерчанта.** Маркетинг: «**No proof of identity needed**; we don't keep transaction details» ([plisio.net](https://plisio.net/)). Но AML-документ прямо описывает риск-ориентированную процедуру: «Plisio's AML/KYC procedure is supported by an automated risk prevention system», верификация через **SumSub**, «You will have 3 days to complete the verification procedure, and if you don't want to pass KYC…»; «reserves the right to apply the KYC/AML procedure to certain clients» ([plisio.net/docs/aml.pdf](https://plisio.net/docs/aml.pdf)). Юрлицо для подключения не требуется (регистрация «in 2 clicks», «Sign up for free»). ToS также оставляет право «freeze your account in case of a request from state regulatory authorities», но содержит и обратное обещание: «Plisio guarantees that your account will not be frozen if you do not violate the laws of your country».
4. **Монеты.** 15+ монет: BTC, ETH, LTC, ZEC, DOGE, DASH, **XMR (Monero)**, USDT, BCH, USDC, TUSD, SHIB, BTT, TRX, BNB, BUSD, ETC ([plisio.net](https://plisio.net/)). Автоконвертация: «convert crypto into 160+ world currencies»; отдельно заявлен приём монет напрямую в свой кошелёк ([partnerkin.com/services/plisio](https://partnerkin.com/services/plisio)).
5. **Рекуррент — не подтверждено.** В API-примерах только `createTransaction`, `getBalances`, `createMassWithdrawal`; продуктов подписок на сайте не заявлено ([plisio.net](https://plisio.net/), [plisio.net/documentation](https://plisio.net/documentation)).
6. **Интеграция — сложность 2/5.** REST API + вебхуки, официальные модули для OpenCart, VirtueMart, WHMCS, WooCommerce, Magento, PrestaShop, Ecwid, CS-Cart, XenForo и **Telegram** ([plisio.net](https://plisio.net/)).
7. **Выплаты.** Массовые выплаты до 1000 транзакций одним пулом, «No limits for payout amount», импорт из .xls ([plisio.net/mass-payouts](https://plisio.net/mass-payouts)). Минимумы вывода в открытых источниках — **не подтверждено**.
8. **Риски.** (а) Санкционные оговорки и право на заморозку по запросу регулятора. (б) Публичные обвинения в скаме на профильном форуме — пользовательская жалоба, не доказательство, но сигнал: [tarnkappe.info/forum/t/plisio-net-soll-angeblich-scam-sein/15816](https://tarnkappe.info/forum/t/plisio-net-soll-angeblich-scam-sein/15816). (в) Юрисдикция и лицензия — **не подтверждено**.
9. **Актуальность.** Официальные страницы живые (2026), копирайт «© 2026 Plisio, Inc.»; даты обновления тарифов и AML-документа не указаны — **не подтверждено**.

---

## 4. Cryptomus

1. **VPN-политика — не подтверждено.** Официальный ToS (`cryptomus.com/tos`) отдаёт Cloudflare-заглушку «Just a moment...» при машинном обращении, а зеркало на польской/японской локали в индексе поиска не содержит текста о VPN. Прямого запрета VPN найти не удалось, но и явного разрешения нет → **не подтверждено**.
2. **Комиссии.** «We only charge **2%** commission per transaction for new users, but the number can be even lower — contact us to get up to **0.4%** commission» ([cryptomus.com/fees/payment](https://cryptomus.com/fees/payment)). Автоконвертация: «The Auto-Convert feature allows you to convert all incoming payments to the currencies you prefer… It's completely free of charge!». Настраиваемый допуск недоплаты 0–5%, кастомные комиссии до 100% на монету, массовые выплаты, авто-вывод по расписанию, вывод с конвертацией. Переводы между кошельками внутри — без комиссии.
3. **KYC/AML мерчанта — фактически блокирующий пункт (2026).** 21–22 января 2026 у мерчантов и клиентов Cryptomus массово отключились личные кабинеты с требованием расширенного KYC; в письме поддержки: обязательная идентификация конечного клиента (KYC плательщиков), соблюдение KYT/Travel Rule с передачей ФИО, email, страны налогового резидентства, адреса, даты рождения и адреса кошелька, DAC8 с 01.01.2026. Ключевое: «**пройти дополнительную проверку KYC невозможно — стран СНГ и Европы в списке для KYC нет**»; KYC сняли на 7 дней, вывод остатков обещали в течение 20 дней ([pressaff.com](https://pressaff.com/tg-news/problemy-u-protsessinga-cryptomus-vchera-u-merchantov-i-klientov-cryptomus-massovo-sleteli/)). Это сообщество CPA, не официальный источник — но описывает конкретные последствия для РФ/СНГ-мерчантов.
4. **Монеты.** BTC, ETH, BNB, SOL, LTC, DOGE, **XMR**, BCH, USDC, SHIB, TRX, TON и др.; приём USDT и др. ([cryptomus.com/fees/payment](https://cryptomus.com/fees/payment), [cryptomus.com/gateway](https://cryptomus.com/gateway)).
5. **Рекуррент — не подтверждено.**
6. **Интеграция — сложность 2/5.** API (`doc.cryptomus.com`), плагины для CMS, приём «Via Telegram» ([cryptomus.com/fees/payment](https://cryptomus.com/fees/payment), [cryptomus.com/blog/how-to-accept-crypto-payments-via-telegram](https://cryptomus.com/blog/how-to-accept-crypto-payments-via-telegram)).
7. **Выплаты.** Авто-вывод по периоду/валюте/сети, вывод через API, конвертация при выводе, массовые выплаты. Сроки/минимумы — **не подтверждено**.
8. **Риски — высокие.** Массовые KYC-блокировки января 2026 с недоступностью баланса; CIS вне списка доступного KYC; отдельный FAQ «Причины, по которым транзакции могут быть заморожены» ([cryptomus.com/faq/reasons-why-transactions-can-be-frozen](https://cryptomus.com/faq/reasons-why-transactions-can-be-frozen)); FAQ про AML-комплаенс ([cryptomus.com/faq/what-is-aml-compliance](https://cryptomus.com/faq/what-is-aml-compliance)).
9. **Актуальность.** Ситуация менялась в январе 2026; фактическая доступность для мерчанта из РФ на 2026-10-05 — **не подтверждено** и должна проверяться лично до интеграции.

---

## 5. CoinGate

1. **VPN-политика — да, VPN-мерчанты обслуживаются.** На сайте есть отдельные категории каталога мерчантов: **VPNs** («Freedom & privacy online») и **Proxy** («Secure, seamless connections») ([coingate.com/stores/category/vpn](https://coingate.com/stores/category/vpn), [coingate.com/stores/category/proxy](https://coingate.com/stores/category/proxy)). Это сильнее любых деклараций: сервис публично перечисляет VPN-мерчантов. Общий T&C при этом содержит широкую оговорку: «You must not use the Services or the CoinGate System for any activity that violates law, creates compliance risk for us, or brea[ks]…» ([coingate.com/policy/general-terms-and-conditions](https://coingate.com/policy/general-terms-and-conditions)).
2. **Комиссии.** Тариф **Standard — 1% per transaction**, Core payment acceptance, 10+ криптовалют, приём из 180+ стран, «Standard currency exchange», «Standard settlements»; **Enterprise — Custom rates, value-based**, on-demand автоматические сеттлменты ([coingate.com/pricing](https://coingate.com/pricing)). Отдельный документ по выплатам: «SPECIAL TERMS OF CRYPTO PAYOUTS FOR MERCHANTS» ([coingate.com](https://coingate.com/app/uploads/2024/09/Crypto-Asset-Transfers-Payouts-_-Oct9.docx.pdf)). Минимальная комиссия/фикс — **не подтверждено**.
3. **KYC/AML мерчанта.** Merchant Terms of Services содержат раздел о customer due diligence: «We shall be entitled to the customer due diligence measures by means of an agent or a service provider» ([coingate.com Merchant ToS PDF](https://coingate.com/app/uploads/2024/02/Merchant-terms-of-Services_2024-02-06.pdf)). Требования к документам и допустимость физлица — **не подтверждено** (PDF отдаётся частично, полный текст получить не удалось).
4. **Монеты.** 10+ криптовалют в Standard, «All supported currencies» — отдельная страница; фиатные сеттлменты и конвертация в фиат заявлены ([coingate.com/pricing](https://coingate.com/pricing), [coingate.com/supported-currencies](https://coingate.com/supported-currencies)).
5. **Рекуррент.** Есть продукт **Billing** — «Simplify billing and payments» ([coingate.com/billing](https://coingate.com/billing)); наличие именно рекуррентных списаний по подписке — **не подтверждено**.
6. **Интеграция — сложность 2/5.** API (`developer.coingate.com`), sandbox (`sandbox.coingate.com`), плагины, Payment Channels (выделенный адрес для клиента), GitHub-репозитории ([coingate.com](https://coingate.com/)).
7. **Выплаты.** Крипто-выплаты (Crypto payouts), сеттлменты Standard/on-demand, Multi-currency account, «Digital asset custody» — хранение крипты и фиата в одном регулируемом счёте ([coingate.com](https://coingate.com/)). Сроки/минимумы — **не подтверждено**.
8. **Риски.** (а) Комплаенс-оговорка широкая, право отказать в обслуживании. (б) Формально требуется юридический субъект и прохождение due diligence — для схемы «родитель как самозанятый/физлицо» пригодность **не подтверждена**. (в) Санкционный комплаенс ЕС; обслуживание РФ-резидента — **не подтверждено**, требуется прямая проверка.
9. **Актуальность.** Страницы тарифов и каталога актуальны на 2026-10-05.

---

## 6. CoinPayments

1. **VPN-политика — нет запрета.** Раздел 7.1 «Prohibited Activities» перечисляет: unlawful activity и санкционные программы, unlawful pornography, unlawful gambling, fraudulent businesses, marijuana/tobacco/e-cigarettes, оружие, наркотики, продажу краденого и т.п. VPN/прокси/анонимайзеров в списке нет ([CoinPayments User Agreement PDF](https://www.coinpayments.net/downloads/CoinPayments_User_Agreement.pdf), раздел «Prohibited Activities»). Список при этом «illustrative, but not exhaustive».
2. **Комиссии.** Новая платформа: «ONE SIMPLE TRANSACTION FEE. **Starting at 3%**» для монет; «A **1%** fee is charged on incoming token payments instead»; «*Fee adjustment may apply to clients in **high-risk industries**». Режимы: To Balance 3%, ASAP Mode 3% + network fee, Nightly Mode 3% + network fee; «Payout modes which require conversions have an additional **0.1%** fee» ([coinpayments.net/help-fees](https://www.coinpayments.net/help-fees)). **Расхождение:** в том же документе выше по тексту упоминается «1% payment fee» — вероятно, остаток от legacy-тарифа; в TSV зафиксированы 3% как ставка новой платформы.
3. **KYC/AML мерчанта.** Заявлено: «Backed by ISO 27001 certification and robust **AML/KYC frameworks**»; «risk screening prior to settlement» ([coinpayments.net/supported-coins-fees](https://www.coinpayments.net/supported-coins-fees)). Конкретные документы, пороги и допустимость физлица — **не подтверждено**. Компания канадская (Ванкувер) — по вторичному источнику ([plisio.net](https://plisio.net/uk/crypto/litecoin-payment-gateway)).
4. **Монеты.** Крупнейшая поддержка монет среди классических шлюзов; фиатная конвертация «through trusted partners, receive digital assets, or convert crypto to fiat for bank payout where supported» ([coinpayments.net/supported-coins-fees](https://www.coinpayments.net/supported-coins-fees)). Точный список — на `supported-coins`.
5. **Рекуррент — не подтверждено.**
6. **Интеграция — сложность 3/5.** API (исторически SOAP/JSON), плагины, «Integration Overview» ([blog.coinpayments.net Integration Overview PDF](https://blog.coinpayments.net/wp-content/uploads/2019/05/CoinPayments-Integration-Overview.pdf)). Документация местами устарела (2019).
7. **Выплаты.** Батчинг выводов из нескольких callback-адресов: «Save up to 90% on withdrawal fees»; fiat-выплаты «where supported»; «Insured Funds for Merchants» — страхование кастодиальных остатков ([coinpayments.net/supported-coins-fees](https://www.coinpayments.net/supported-coins-fees)). Минимумы/сроки — **не подтверждено**.
8. **Риски.** (а) 3% — дорого при чеке 199–1590 ₽. (б) «Fee adjustment may apply to clients in high-risk industries» — VPN почти наверняка будет отнесён к high-risk, ставка может вырасти. (в) Явная оговорка о risk screening «prior to settlement» означает возможные удержания.
9. **Актуальность.** Страница тарифов содержит переключатель Legacy/New Platform и выглядит актуальной (2026); PDF-договор без даты обновления — **не подтверждено**.

---

## 7. Confirmo

1. **VPN-политика — не подтверждено** (официальный сайт и ToS не удалось получить полностью).
2. **Комиссии (вторичный источник).** «Transaction Fee: **0.8%** per transaction; Payout Fee: **0.5%** for on-demand cryptocurrency withdrawals; Settlement Withdrawals: Free (standard banking fees may apply)» ([bilixe.com/listing/confirmo-crypto-payment-gateway](https://bilixe.com/listing/confirmo-crypto-payment-gateway/)). Первичным источником не подтверждено.
3. **KYC/AML мерчанта.** По данным академического источника, для использования платёжного шлюза в роли мерчанта возможна регистрация **только фирмы**: «Pro užití platební brány, v roli obchodníka, je možná registrace pouze firmu» ([dspace.vsb.cz](https://dspace.vsb.cz/server/api/core/bitstreams/531b9413-882d-4216-b2d7-db3a74f62bc2/content)). Косвенно подтверждается наличием регистрационного дела в AMF (Франция): [AMF — Confirmo Limited](https://www.amf-france.org/sites/institutionnel/files/pdf/77623/en/Confirmo_Limited.pdf). Уверенность низкая.
4. **Монеты и сеттлмент.** BTC (+ Lightning), ETH, SOL, LTC, TRX, USDC, USDT; автоконвертация в фиат (EUR, USD, CZK) либо удержание в крипте; сеттлменты ежедневно/еженедельно/ежемесячно ([bilixe.com](https://bilixe.com/listing/confirmo-crypto-payment-gateway/)).
5. **Рекуррент — не подтверждено.**
6. **Интеграция — сложность 3/5.** Open-source API, WooCommerce-плагин, модули для CRM/биллинга ([bilixe.com](https://bilixe.com/listing/confirmo-crypto-payment-gateway/)); коннектор в Corefy ([corefy.com/connectors/confirmo](https://corefy.com/connectors/confirmo)).
7. **Выплаты.** Крипто-вывод 0,5%, банковский сеттлмент бесплатно; фиатные валюты EUR/USD/CZK — **RUB отсутствует**.
8. **Риски.** Отсутствие подтверждённой VPN-политики, требование юрлица, ориентация на ЕС/Чехию — для 15-летнего основателя с родителем-самозанятым практической ценности не имеет.
9. **Актуальность.** Обзоры 2026 года, но первичных данных мало. **Уверенность низкая.**

---

## 8. CoinsPaid

1. **VPN-политика — не подтверждено.** Официальный ToS `coinspaid.com/legal/tos/` блокируется Cloudflare при машинном обращении; страница тарифов `coinspaid.com/payments/pricing/` отдаёт 404 — цены и условия обсуждаются только через sales.
2. **Комиссии — не подтверждено** (публичного прайса нет).
3. **KYC/AML мерчанта.** Юрлицо: Coinspaid Solutions OÜ, registry code 17541786, ЕС ([coinspaid.com](https://coinspaid.com/)); компания выпускает комплаенс-гайды «HOW COINSPAID HELPS BUSINESSES STAY COMPLIANT» и «ACCEPTING CRYPTO: FULL CRYPTO COMPLIANCE CHECKLIST FOR BUSINESSES» ([coinspaid.com](https://coinspaid.com/wp-content/uploads/2025/11/CP_Guide_How-CoinsPaid-helps-businesses-stay-compliant.pdf)). Уровень комплаенса высокий → для физлица/самозанятого из РФ малопригоден.
4. **Монеты/автоконвертация — не подтверждено.**
5. **Рекуррент — не подтверждено.**
6. **Интеграция.** Есть плагин для WooCommerce ([wordpress.org/plugins/coinspaid](https://es-ec.wordpress.org/plugins/coinspaid/)) и пользовательский гайд для Adobe Commerce ([commercemarketplace.adobe.com](https://commercemarketplace.adobe.com/media/catalog/product/coinspaid_extensions-payment-1-0-3-ce/user_guides.pdf)); сложность — 3/5 (оценка по факту наличия API и плагинов).
7. **Выплаты — не подтверждено.**
8. **Риски.** (а) В 2023 году CoinsPaid была взломана на ~$37,3 млн ([smartcontractshacking.com](https://smartcontractshacking.com/hacks/coinspaid-hack-2023)); компания заявляла, что средства клиентов не пострадали ([coinspaidmedia.com](https://coinspaidmedia.com/es/news/coinspaid-hacked-no-customer-funds-affected/)). (б) Независимое расследование FinTelegram о структуре группы SoftSwiss/Dream Finance/CoinsPaid — репутационный сигнал ([fintelegram.com PDF](https://fintelegram.com/wp-content/uploads/2026/06/SoftSwiss-Compliance-Report-v2-Coinspaid-Dev-Update.pdf)). (в) География присутствия, заявленная на сайте: «European Union, USA, India, Malaysia, Australia, Argentina» — **России в списке нет** ([coinspaid.com](https://coinspaid.com/)).
9. **Актуальность.** Сайт 2026 года, но конкретика по тарифам/политике недоступна публично → **высокая неопределённость**.

---

## 9. ALFAcoins (СНГ-подобный шлюз, с 2013)

1. **VPN-политика — не подтверждено.** ToS получить не удалось; прямых запретов VPN в доступных материалах нет.
2. **Комиссии.** Официальный прайс: «Incoming payment (invoice) — **0.99%**»; «Withdrawal / Auto Withdrawal (Cryptocurrency) — **Network fee**»; «**Payout (API)** — Network fee + **0,99%**»; Refund 0%; Donation 0.99%. Сетевые комиссии фиксированы в валюте и обновляются: например, **TRC-20 Tether — 2,55664 USDT депозит / 4,5414 USDT вывод**, ERC-20 Tether — 0,3878 USDT, BEP-20 Tether — 0,005636 USDT. Подпись: «Last reviewed at: October 05, 2026» ([alfacoins.com/fees](https://alfacoins.com/fees)).
3. **KYC/AML мерчанта.** На главной прямо: «ALFAcoins is working with **all legal business entities**» ([alfacoins.com](https://alfacoins.com/)) — то есть требуется юридическое лицо; физлицо/самозанятый не подходит.
4. **Монеты.** Bitcoin, Bitcoin Cash, Litecoin, Ethereum, Dash, **BEP-20 Tether, SOL Tether, ERC-20 Tether**, Solana, XRP и др.; в прайсе также TRC-20 Tether, USDC, TRX, BNB, DAI, DOT, DOGE, XLM, ATOM ([alfacoins.com](https://alfacoins.com/), [alfacoins.com/fees](https://alfacoins.com/fees)). Автоконвертация: «Exchange (Cryptocurrency to Cryptocurrency) — To be introduced» — то есть **на дату проверки ещё не запущена**.
5. **Рекуррент — не подтверждено.**
6. **Интеграция — сложность 2/5.** Есть API, страница разработчиков, туториалы ([alfacoins.com](https://alfacoins.com/)).
7. **Выплаты.** Только криптой; комиссия сети (+0,99% для payout через API). Минимумы — **не подтверждено**.
8. **Риски.** Требование юрлица (блокирующее), отсутствие конвертации в фиат на дату проверки, TRC-20 вывод дорогой (~4,54 USDT за транзакцию — сопоставимо с чеком подписки).
9. **Актуальность.** Прайс помечен «Last reviewed at: October 05, 2026» — максимально свежий. Копирайт в футере местами «2013-2021» — сайт содержит устаревшие блоки.

---

## 10. NOWPayments

1. **VPN-политика — VPN не запрещён, но это неважно.** В разделе 14.1.5 «carry on any unlawful businesses and activities such as, for example: … prostitution, escorts; fraudulent business…; sale of narcotics…; pyramid schemes, high risk investments schemes…; any business that we believe poses elevated financial risk» VPN не упомянут ([NOWPayments FD ToS PDF](https://nowpayments.io/doc/fd-tos.pdf)). Упоминаний vpn/proxy/anonymizer в тексте договора нет (проверено поиском по извлечённому тексту).
2. **Комиссии.** Официальная страница прайсинга: «350+ Currencies, **1% Service fee**, 5 min Average tx time, 24/7 Support» ([nowpayments.io/pricing](https://nowpayments.io/pricing)). Есть отдельная программа «zero deposit fee USDT TRC20» ([nowpayments.io/zero-deposit-fee-usdttrc20](https://nowpayments.io/zero-deposit-fee-usdttrc20)). **Расхождение:** сторонний обзор указывает 0,5% ([partnerkin.com](https://partnerkin.com/en/b2b/review/nowpayments/)) — официальная страница даёт 1%.
3. **KYC/AML мерчанта — есть KYB.** «The third-party service provider reserves the right to request Know Your Business (KYB) verification from Merchant» ([nowpayments.io/doc/tos.pdf](https://nowpayments.io/doc/tos.pdf)); отдельная AML/KYC Policy ([nowpayments.io/doc/AML_KYC_Policy_NOWPayments.pdf](https://nowpayments.io/doc/AML_KYC_Policy_NOWPayments.pdf)).
4. **Монеты.** 350+ монет и токенов, включая USDT, BTC, ETH, XRP, XMR, TRX, USDC, BNB, DOT, BCH ([nowpayments.io/supported-coins](https://nowpayments.io/supported-coins)). Автоконвертация и fiat on-ramp/off-ramp заявлены продуктами ([nowpayments.io/fiat-on-ramp](https://nowpayments.io/fiat-on-ramp), [nowpayments.io/off-ramp](https://nowpayments.io/off-ramp)).
5. **Рекуррент — да, есть продукт.** «Subscriptions» — отдельный продукт ([nowpayments.io/crypto-subscriptions](https://nowpayments.io/crypto-subscriptions)). Детали (комиссия, механика списания) — **не подтверждено**.
6. **Интеграция — сложность 2/5.** API + IPN-вебхуки с подробной Postman-документацией, плагины, payment widget/button, white label ([nowpayments.io/api](https://nowpayments.io/api)).
7. **Выплаты.** Mass payouts, zero-fee mass payouts powered by ChangeNOW, custody, off-ramp payouts ([nowpayments.io/mass-payouts](https://nowpayments.io/mass-payouts)).
8. **Риски — блокирующие.** Раздел **15.1 PROHIBITED JURISDICTIONS** (FD Transfers LLC): «The Services are not rendered to residents or citizens of the European Union, the United Kingdom, the United States of America, the United Arab Emirates, **or to persons located in or resident of the Russian Federation**, or any jurisdiction where the use of cryptocurrency services is restricted or prohibited by applicable law» ([nowpayments.io/doc/fd-tos.pdf](https://nowpayments.io/doc/fd-tos.pdf), v1.4.2). Плюс п. 15.5: «You shall comply with this Section 12, even if FD Transfers LLC' methods to prevent use of the Services are not effective or can be bypassed». Это прямое и однозначное исключение РФ-резидента.
9. **Актуальность.** ToS v1.4.2 и прайс — 2026; страница прайсинга содержит копирайт «© NOWPayments – 2026».

---

## 11. BitPay

1. **VPN-политика — не подтверждено** (в разделе 4.4 «Prohibited Use and Business» VPN не упомянут; список «non-exhaustive», есть отдельный раздел 4.5 «Restricted Business», требующий предварительной авторизации — [bitpay.com/legal/terms-of-use](https://bitpay.com/legal/terms-of-use/)).
2. **Комиссии — не подтверждено** (публичного прайса в открытом доступе нет).
3. **KYC/AML мерчанта.** Жёсткое: «if an individual, are at least **18 years of age**… or, if a business or organization, have all necessary right, power, authority, and capacity to accept and bind such business or organization»; требование предоставлять и поддерживать «accurate, complete and satisfactory information»; BitPay «has the right to reject your Account registration, to later close your Account, or to restrict the provision of the Acceptance Services» ([bitpay.com/legal/terms-of-use](https://bitpay.com/legal/terms-of-use/)).
4. **Монеты/сеттлмент.** Классический BTC-центричный процессор (BTC, BCH, ETH, LTC, XRP, DOGE и стейблкоины), фиатные выплаты в банк; **RUB не поддерживается** — **не подтверждено официальной таблицей**, оценка по позиционированию на US-рынок.
5. **Рекуррент — не подтверждено.**
6. **Интеграция — сложность 3/5** (API, плагины, BitPay ID).
7. **Выплаты.** Банковские выплаты в US-банки; сроки/минимумы — **не подтверждено**.
8. **Риски.** США (OFAC, Dutch Sanctions Act в тексте), запрет на gambling «if you and your Shoppers or Donors are located» в ряде юрисдикций, обязательный возраст 18+ и корпоративная структура → **неприменим к 15-летнему основателю и к РФ-аудитории**.
9. **Актуальность.** Страница ToS живая (2026).

---

## 12. OpenNode

1. **VPN-политика — не запрещено, но с оговоркой.** В разделе 14.3 «Prohibited businesses» VPN не указан; в категории «High risk businesses» есть «**circumvention, jamming and interference devices**» — это про устройства обхода, а не про VPN-сервисы ([opennode.com/terms-and-conditions](https://opennode.com/terms-and-conditions/)). При этом на странице цен отдельно предлагается кастомный квоут для «**High Risk merchants**» ([opennode.com/pricing](https://opennode.com/pricing/)).
2. **Комиссии.** «Conversion is always free»; вывод BTC: scheduled — «Free, On-chain, Processed Weekly», on demand — «Instant, Free with Lightning, **1% fee on-chain**»; вывод в локальной валюте в банк — «Processed daily / Transfers next business day», «Transfers 1-2 business days» ([opennode.com/pricing](https://opennode.com/pricing/)). Ставка за приём платежа на странице в явном виде не указана — **не подтверждено**.
3. **KYC/AML мерчанта — обязательное.** «OpenNode is required to obtain, verify and record information that identifies each Business and Individual Client that opens an account on our platform, as part of a Customer Identification Program (CIP), in accordance with section 326 of the USA Patriot Act»; «OpenNode reserves the right to freeze Your Account until Your identity has been verified» ([opennode.com/terms-and-conditions](https://opennode.com/terms-and-conditions/)). Возраст: «intended for use by businesses and individuals **18 years of age and older**».
4. **Монеты.** Только Bitcoin + Lightning Network ([opennode.com/pricing](https://opennode.com/pricing/)). Автоконвертация BTC → USD/EUR/GBP/BRL/MXN/AUD/HKD/CAD в момент платежа по зафиксированному курсу ([opennode.com/pricing](https://opennode.com/pricing/)).
5. **Рекуррент.** Есть продукт «Billing» — «Billing & Invoicing» ([opennode.com/billing-invoicing](https://opennode.com/billing-invoicing/)); наличие рекуррента — **не подтверждено**.
6. **Интеграция — сложность 2/5.** REST API, hosted checkout, плагины, Lightning ([developers.opennode.com](https://developers.opennode.com/docs)).
7. **Выплаты.** BTC-кошелёк (еженедельно / on-demand) или банк в 8 фиатах — **RUB отсутствует**.
8. **Риски.** US-корпорация (Delaware), CIP/Patriot Act, арбитраж в Лос-Анджелесе, оговорка «Your account may be subject to freezing, forfeiture to, or seizure by a law enforcement agency» ([opennode.com/terms-and-conditions](https://opennode.com/terms-and-conditions/)). Не подходит для РФ.
9. **Актуальность.** ToS обновлён 29.08.2024; страница цен — 2026 (курс BTC обновляется).

---

## 13. Trocador и Trocador-подобные (no-KYC свап-агрегаторы)

Это **не платёжный шлюз для мерчанта**, а инструмент покупателя/владельца: агрегатор no-KYC обменников.

- Модель: агрегатор сравнивает курсы 20+ privacy-friendly партнёров (SimpleSwap, Exolix, ChangeNow, FixedFloat, StealthEX и др.) и маршрутизирует сделку к лучшему; **собственной комиссии Trocador не берёт** — получает часть fee партнёра; аккаунт и KYC не требуются; поддерживается 200+ монет, включая Monero/Zcash/Dash; есть «Payment Mode» (платишь одной монетой — получаешь другую), fiat-gateway через партнёров, AML-рейтинг каждого партнёра (A–D) и «Trocador Guarantee» ([mycrypto.dk/service/trocador](https://mycrypto.dk/service/trocador/), обновлено 22.04.2026).
- Минимумы зависят от партнёра, «typisk $10-$100»; партнёры с рейтингом «D» могут блокировать средства при провале KYC, и Trocador Guarantee это не покрывает (там же).
- Практическая ценность для «Kometa»: способ **купить крипту без KYC** или конвертировать полученное, но не способ приёма оплаты от клиента. Для клиента-обывателя это непонятный и рискованный маршрут.

---

## 14. Приём без посредника (своя проверка блокчейна)

Схема: у компании свой кошелёк(ы), бот выставляет адрес и сумму, после поступления транзакции сам проверяет блокчейн и активирует подписку. Комиссия сервису — 0, платим только сетевые сборы. Ниже — что реально по каждой сети, с подтверждёнными ограничениями.

### 14.1. USDT TRC20 на свой кошелёк + TronGrid API — **технически реально**

- **API есть.** Официальный эндпоинт TronGrid v1: `Get TRC-20 transaction info by account address` — `GET /v1/accounts/{address}/transactions/trc20` ([developers.tron.network/reference/get-trc20-transaction-info-by-account-address](https://developers.tron.network/reference/get-trc20-transaction-info-by-account-address)).
- **Ключ нужен.** «TronGrid uses API keys, daily quotas, and request frequency limits to protect service stability. Rate-limited requests usually return 429 or 403… **Production requests should include TRON-PRO-API-KEY**»; без ключа — «**Strict rate limiting or rejection**»; при «abnormally frequent polling» — «Temporary block or 403 / 429». **Конкретные цифры квот и QPS официально не публикуются:** «Specific quota and QPS may change depending on plan, network, endpoint type, and service policy. **Do not hard-code fixed limits into business logic**» ([developers.tron.network/reference/rate-limits](https://developers.tron.network/reference/rate-limits)). Вывод: бесплатный тариф существует и «как есть» работает для десятков платежей в день, но **точные лимиты — не подтверждено**, и polling-архитектуру нужно строить с backoff и без хардкода.
- **Сопоставление платежа.** TRC20 не имеет memo, поэтому надёжный способ — уникальная сумма (например, 199.037 ₽-эквивалент) или уникальный адрес на заказ. Подтверждения: TRON-блок ~3 сек, для мелких чеков достаточно 1–19 подтверждений; порог — решение разработчика (жёстких рекомендаций в документации нет → **не подтверждено**).
- **Риск заморозки USDT — главный.** Только Tether Ltd. может заморозить адрес, вызвав `addBlackList` у контракта USDT; «the USDT at that address can no longer be transferred out». На 26.07.2026 заморожено **9 597 адресов и $5,69 млрд**, из них на Tron — **$3,71 млрд в 6 901 адресе** (Tron доминирует ~85–97% активности). За 2025 год разморожено лишь **3,6%** адресов, медиана — 18,2 дня. Восстановление через Tether возможно для сумм **> $1 000** с комиссией **до 10%, минимум $1 000** — для чеков 199–1590 ₽ это экономически бессмысленно ([blocksec.com/blog/usdt-freeze-stablecoin-compliance-guide](https://blocksec.com/blog/usdt-freeze-stablecoin-compliance-guide), 26.07.2026). Проверить адрес заранее можно бесплатно: [blocksec.com/usdt-freeze-checker](https://blocksec.com/usdt-freeze-checker).
- **Практический вывод:** «немаркированность» входящего USDT вы не контролируете — платит покупатель, а «засветиться» может ваш адрес сбора. Лечится: отдельный адрес на каждый платёж + перечисление на «холодный» кошелёк + скрининг входящих адресов через AML-API. Обратите внимание: Crypto Pay отдельно декларирует неприём средств из подсанкционных сервисов (Garantex, Grinex, Bitpapa, HTX, EXMO и др.) — при своей схеме этой защиты нет, но и фильтра нет.

### 14.2. BTC через Blockstream Esplora / Blockchair по xpub — **частично реально**

- **Esplora — только адреса и scripthash.** В официальном API: `GET /address/:address`, `GET /scripthash/:hash`, `/address/:address/txs`, `/address/:address/utxo` — **эндпоинта по xpub/descriptor нет** ([github.com/Blockstream/esplora/blob/master/API.md](https://github.com/Blockstream/esplora/blob/master/API.md)). Значит: xpub хранится у вас, адреса выводите сами (BIP32/44/84), а в API стучитесь по каждому адресу. Публичный `blockstream.info/api` для продакшена не предназначен — документация предлагает self-hosting: «You can also self-host the Esplora API server… which provides better privacy and security». Ограничения публичного инстанса официально не опубликованы → **не подтверждено**.
- **Blockchair.** `https://blockchair.com/api/docs` отдаёт **HTTP 401** — документация закрыта (API-ключ обязателен), поэтому бесплатный лимит и поддержка xpub **не подтверждены**. Стороннее зеркало планов утверждает наличие free-tier с суточным лимитом ([api-evangelist/blockchair plans](https://raw.githubusercontent.com/api-evangelist/blockchair/refs/heads/main/plans/blockchair-plans-pricing.yml)) — вторичный источник, использовать как факт нельзя.
- **Риски BTC:** волатильность (нельзя автоконвертировать в рубль без посредника), управление UTXO, комиссия сети, и главное — **продажа BTC за рубли упирается в тот же P2P/обменник и 115-ФЗ**, что и USDT, плюс НДФЛ с дохода от продажи (крипта = имущество, НДФЛ 13–15% с 01.01.2025, 418-ФЗ — [habr.com/en/articles/989158](https://habr.com/en/articles/989158/)).
- **Практический вывод:** для чеков 199–1590 ₽ BTC on-chain экономически неудобен (комиссия сети может съесть до 10–30% чека). Реалистично только как опция «для тех, кто сам хочет», с ручной проверкой.

### 14.3. TON + TONAPI — **реально, самый дешёвый вариант**

- **Есть бесплатный тариф.** «TONAPI is already available for free, but with a default rate limit of **1 request per second (1 RPS)**»; для повышения лимитов без оплаты предлагается механизм `window.tonapi` внутри Tonkeeper ([docs.tonconsole.com/tonapi/dapp/free-limits](https://docs.tonconsole.com/tonapi/dapp/free-limits)). Ключ TONAPI всё равно нужен, «The key can be a free-tier key, but it must be valid» (там же).
- **Вебхуки есть.** TONAPI предоставляет Webhooks API — можно не опрашивать, а получать события ([docs.tonconsole.com/tonapi/webhooks-api](https://docs.tonconsole.com/tonapi/webhooks-api)).
- **Сопоставление платежа.** В TON у переводов есть comment (memo) — можно класть в комментарий номер заказа, что делает сверку тривиальной. USDT-TON (jetton) поддерживается; детали проверки jetton-переводов — в Cookbook ([docs.tonconsole.com/tonapi/cookbook/jetton-transfer](https://docs.tonconsole.com/tonapi/cookbook/jetton-transfer)).
- **Риск:** TON-адрес кошелька получателя не «замораживается» эмитентом (у TON нет аналога `addBlackList` Tether), но сам стейблкоин USDT-TON эмитируется Tether и заморозка jetton-адресов технически возможна — **для TON-jetton USDT публичных данных о массовых заморозках не найдено → не подтверждено**.

### 14.4. Общие риски схемы «без посредника»

1. **Юридический (главный).** С 01.09.2026 приём цифровой валюты как средства платежа за услуги в РФ запрещён (282-ФЗ/283-ФЗ). Схема «свой кошелёк» не создаёт исключения.
2. **Конвертация в рубли.** Любой путь (P2P на бирже, обменник с BestChange, наличные) — это либо банковский перевод, либо наличные. Для P2P: с лета 2025 поправки в **ст. 187 УК РФ** дают дропам до 2 лет и/или штраф 100–300 тыс. ₽, при этом «нет чётких юридических разграничений, поэтому потенциально под статью попадают большинство участников P2P-рынка (в частности те, кто принимает криптовалюту, а отправляет фиат)»; блокировки по **115-ФЗ** (карта может «отлететь» за перевод крупнее 300 000 ₽) и по **161-ФЗ** (единый реестр ЦБ, блокировка счетов во всех банках сразу) ([habr.com/en/articles/989158](https://habr.com/en/articles/989158/)).
3. **«Чёрный треугольник».** Покупатель крипты получает рубль напрямую со счёта жертвы мошенников → владелец счёта становится фигурантом уголовного дела. Реальный приговор: 2 и 2,5 года условно участникам схемы ([habr.com/en/articles/989158](https://habr.com/en/articles/989158/), [Forbes](https://www.forbes.ru/investicii/485643-kak-v-rossii-vynesli-pervyj-prigovor-za-p2p-torgovlu-na-kriptobirze)).
4. **Схема «покупатель платит СБП обменнику — компания получает USDT».** Юридически это (а) расчёт за услугу через третье лицо с крипто-звеном, (б) потенциально «незаконная банковская деятельность» (ст. 172 УК РФ) при систематическом характере, (в) прямой канал для «грязного» фиата на счёт обменника и далее на счёт получателя USDT. Признаки «процессинга» — сильное дробление платежей (100–200 переводов по 500–1000 ₽ вместо одного) — прямо описаны как маркер нелегального финансового трафика ([habr.com/en/articles/989158](https://habr.com/en/articles/989158/)). Для VPN-подписок за 199 ₽ это ровно тот паттерн, который выглядит как дробление.
5. **Возраст.** Основателю 15 лет: биржевой аккаунт с P2P открыть нельзя (18+ у всех крупных CEX), ответственность по 187/172 УК РФ при схеме «на имя ребёнка» ложится на владельца счёта/карты — то есть на родителя.

---

## 15. Обменники и P2P как способ конвертации для покупателя

- **BestChange** — это **мониторинг/агрегатор**, а не платёжный сервис и не сторона сделки: он сравнивает курсы обменников и перенаправляет пользователя. Обзоры 2026 года описывают его именно как инструмент выбора обменника ([buyhold.ru/obzory/bestchange](https://buyhold.ru/obzory/bestchange/), отзывы: [buyhold.ru/bestchange-otzyvy](https://buyhold.ru/bestchange-otzyvy/)). Собственной комиссии у мониторинга нет, платится наценка обменника; типичный разброс курсов — **не подтверждено** (в открытых источниках нет единой цифры).
- Обменники, размещённые на мониторингах, предъявляют требования к владельцам обменников (Premium Exchanger описывает требования мониторингов к обменникам: [premium.gitbook.io](https://premium.gitbook.io/main/osnovnye-nastroiki/faq/kakie-trebovaniya-predyavlyayutsya-k-obmennikam-ot-monitoringov)), но это требования к обменнику, а не гарантия безопасности для плательщика.
- Более безопасные альтернативы P2P по данным профильного разбора: обмен на **наличные** (не проходит через банковские счета → нет риска 115-ФЗ/161-ФЗ), вывод на **зарубежную карту**, и **онлайн-обменники через агрегаторы** с проверкой рейтинга, объёма отзывов и даты старта ([habr.com/en/articles/989158](https://habr.com/en/articles/989158/)).
- **Вывод для «Kometa»:** использовать обменники/P2P как способ конвертации **на стороне компании** — приемлемо только с юристом и с пониманием, что вся цепочка «фиат → крипта → фиат» фиксируется банками. Использовать их как способ **оплаты покупателем** («заплати СБП обменнику, получи USDT и отправь нам») — плохая идея: для обычного клиента это непонятно, а для компании — риск ст. 172 УК РФ и внимание банков к счетам обменника.

---

## 16. Кто явно принимает VPN, а кто банит

| Явно принимает VPN | Серая зона (нет запрета, но и разрешения нет) | Явно/фактически не подходит |
|---|---|---|
| **CoinGate** — VPN и Proxy вынесены в отдельные категории каталога мерчантов ([ссылка](https://coingate.com/stores/category/vpn)) | **Crypto Pay (CryptoBot)** — VPN нет в списке запрещённого, но есть право блокировки и конфискации ([политика](https://help.cr.bot/en/articles/12594929-crypto-pay-policy)) | **NOWPayments** — прямое исключение РФ-резидентов ([ToS 15.1](https://nowpayments.io/doc/fd-tos.pdf)) |
| **Plisio** — открыто обслуживает «High Risk» и «Adult» ([главная](https://plisio.net/)) | **CryptoCloud/Trybit** — VPN нет в списке запрещённого, обслуживаются гэмблинг и adult ([требования](https://support.trybit.com/ru/general-information/connection-requirements)) | **BitPay** — юрлицо + 18+, US-комплаенс ([ToS](https://bitpay.com/legal/terms-of-use/)) |
| **OpenNode** — принимает «High Risk merchants» по кастомному квоуту ([прайс](https://opennode.com/pricing/)) | **CoinPayments** — VPN нет в списке Prohibited Activities, но «fee adjustment may apply to clients in high-risk industries» ([fees](https://www.coinpayments.net/help-fees)) | **OpenNode** — US-корпорация, Patriot Act CIP, нет RUB |
| **ALFAcoins** — обслуживает «all legal business entities», high-risk не исключён ([сайт](https://alfacoins.com/)) | **Cryptomus** — политика не подтверждена, KYC-кризис января 2026 | **CoinsPaid** — нет публичного прайса, нет РФ в списке регионов, высокий комплаенс |
| | **Confirmo**, **CoinsPaid**, **ALFAcoins** — VPN-политика не подтверждена | **Cryptomus** — фактическая недоступность KYC для СНГ (январь 2026) |

**Отдельно:** случаев, когда именно VPN-мерчанта забанили «без возврата средств», в проверенных первичных источниках **не найдено** (не подтверждено). Зафиксированы два других механизма потери средств: (1) AML-заморозка входящего депозита (Crypto Pay — до отклонения депозита и «сжигания» адреса) и (2) полное отключение кабинета с требованием KYC, которое невозможно пройти (Cryptomus, январь 2026).

---

## Топ-3 в категории B

**1. Crypto Pay (CryptoBot) — оставить как есть, но с двумя оговорками.**
Плюсы: уже подключён; **счёт можно выставлять в рублях** (`fiat: RUB`) с оплатой в любой из 8 монет; автоконвертация `swap_to`; вебхуки с HMAC; тестнет; библиотеки под Python; Россия не в списке недоступных регионов. Минусы: **3% при обороте до $10k/30 дней** (многие считают, что бесплатно — это неверно), кастодиальность, заморозки депозитов из подсанкционных сервисов (Garantex/Grinex/Bitpapa/HTX/EXMO — то есть из самых популярных у россиян источников крипты), нет рекуррента в API.
Действия: (а) предупреждать пользователей не платить с Garantex/Bitpapa/HTX; (б) держать на балансе минимум, выводить часто; (в) считать экономику по 3%, а не по «0%».

**2. CryptoCloud → Trybit — лучший внешний шлюз под эту задачу.**
Плюсы: **1,9%** (от 0,4% индивидуально), **0% за вывод**, **KYC/KYB не требуется — достаточно физлица, без ограничений по странам**, проект типа «Telegram-бот» принимается без своего домена, вывод **без резервов и заморозок**, минимум $10, автовывод по правилам, 40+ пар, USDT в 7 сетях, автоконвертация в стейблкоины, Host2Host/White Label. Минусы: +$1,40 за платежи в Tron (при чеке 199 ₽ это ~0,6–0,7% сверху — то есть фактически ~2,5–2,6% на дешёвых тарифах); AML-скрининг входящих; молодой ребрендинг 2026 → юрисдикция и устойчивость не подтверждены.
Действия: подключать **только для тарифов от ~500 ₽** либо стимулировать оплату в TON/BEP20/ERC20/Web3, где трансферной комиссии нет.

**3. Plisio — самый дешёвый вариант (0,5%), но с проверкой.**
Плюсы: **0,5%** и никаких месячных/setup-сборов, «No proof of identity needed», принимает **Monero**, открыто работает с High Risk/Adult, массовые выплаты без лимита суммы, есть Telegram-модуль. Минусы: KYC через SumSub может быть запрошен риск-системой в любой момент (на верификацию 3 дня), санкционные оговорки и право заморозки по запросу регулятора, публичные жалобы на форуме, юрисдикция/лицензия не подтверждены.
Действия: подключить как второй канал, тестировать на малых суммах, не держать баланс.

**Почему не «своя проверка блокчейна» в топ-3.** Технически USDT TRC20 + TronGrid и TON + TONAPI реализуются (см. §14), комиссия 0%, но: юридический запрет 282-ФЗ/283-ФЗ, отсутствие скрининга входящих (риск заморозки адреса), и главное — конвертация в рубли упирается в P2P/обменники с рисками 115-ФЗ/161-ФЗ/187 УК РФ. Это разумный **резервный** канал и способ снизить зависимость от кастодиальных сервисов, а не основной.

---

## Кого вычеркиваем

| Сервис | Причина |
|---|---|
| **NOWPayments** | ToS 15.1 (FD Transfers LLC): услуги не оказываются «persons located in or resident of the Russian Federation», а также резидентам ЕС/Великобритании/США/ОАЭ. Обход прямо запрещён п. 15.5. |
| **BitPay** | Требует 18+ и юридическую структуру, US-комплаенс, ориентирован на US-рынок; для 15-летнего основателя и РФ-аудитории неприменим. |
| **OpenNode** | US-корпорация, обязательный CIP по Patriot Act, только BTC/Lightning, банковские выплаты в 8 фиатах **без RUB**, 18+. |
| **CoinsPaid** | Нет публичных тарифов и политики (404/Cloudflare), нет России среди заявленных регионов, высокий комплаенс-профиль, история взлома 2023 на $37,3 млн. Соотношение «неизвестность/риск» против «выгода» плохое. |
| **Cryptomus** | Январь 2026: массовые KYC-блокировки, обязательный KYC конечных плательщиков, Travel Rule/KYT, **стран СНГ и Европы в списке для KYC нет** → пройти проверку невозможно, при этом доступ к приёму платежей и выводу блокируется. |
| **ALFAcoins** | Работает только с «legal business entities» (юрлицо), конвертация в фиат «to be introduced», вывод TRC-20 USDT ~4,54 USDT за транзакцию — при чеке 199–1590 ₽ экономика не сходится. |
| **Confirmo** | По доступным источникам регистрация мерчанта — только фирма; фиатные сеттлменты EUR/USD/CZK без RUB; VPN-политика не подтверждена. Уверенность низкая, но для физлица смысла нет. |
| **CoinPayments** | 3% с входящего платежа (новая платформа) + оговорка о повышении ставки для high-risk индустрий; дорого при чеке 199–1590 ₽; документация частично устарела. Как единственный канал — нет; как «на всякий случай» — тоже, из-за цены. |
| **Приём «покупатель платит СБП обменнику»** | Прямой риск ст. 172 УК РФ (незаконная банковская деятельность), «процессинговый» паттерн дробления платежей, канал для грязного фиата. Для клиента — непонятно, для компании — опасно. |

**Оставляем под вопросом (нужна прямая проверка в поддержке перед интеграцией):** CoinGate (VPN-категория подтверждена, но требования к юрлицу и доступность для РФ-резидента — нет), CoinPayments (KYC-требования), Cryptomus (фактическая доступность в октябре 2026), CoinsPaid (тарифы).

---

## Что осталось неподтверждённым (честный список пробелов)

1. Точные квоты и QPS бесплатного TronGrid и бесплатного тарифа Blockchair — официально не публикуются / документация закрыта (401).
2. Наличие рекуррентных списаний у CryptoCloud/Trybit, Plisio, Cryptomus, CoinGate (для Billing), CoinPayments, Confirmo, ALFAcoins, CoinsPaid, OpenNode.
3. Требования KYC к физлицу-мерчанту у CoinGate, CoinPayments, CoinsPaid, Confirmo (первичными документами не подтверждено).
4. VPN-политика Cryptomus, CoinsPaid, Confirmo, ALFAcoins (ToS недоступны машинно).
5. Комиссия Crypto Pay за P2P (встречается «~0,4% с продавца» только во вторичном источнике).
6. Пороговые суммы, при которых Plisio запрашивает KYC через SumSub.
7. Возможность заморозки USDT-jetton в сети TON по инициативе Tether (публичных данных о массовых заморозках в TON не найдено).
8. Регуляторный статус/лицензия Plisio и CryptoCloud/Trybit.

---

## Источники (основные)

**Crypto Pay / CryptoBot:** [Fees and Limits Crypto Pay](https://help.cr.bot/en/articles/9820010-fees-and-limits-crypto-pay) · [Fees and limits Subscriptions](https://help.cr.bot/en/articles/9820192-fees-and-limits-subscriptions) · [Fees and limits (wallet)](https://help.cr.bot/en/articles/9717243-fees-and-limits) · [How do I withdraw coins](https://help.cr.bot/en/articles/9717238-how-do-i-withdraw-coins-to-another-wallet) · [Crypto Pay API](https://help.cr.bot/en/articles/10279948-crypto-pay-api) · [Crypto Pay Policy](https://help.cr.bot/en/articles/12594929-crypto-pay-policy) · [Unavailable regions](https://help.cr.bot/en/articles/12460759-unavailable-regions-in-crypto-bot) · [AML review](https://help.cr.bot/en/articles/12594783-why-was-my-deposit-locked-for-aml-review) · [Sanctioned services](https://help.cr.bot/en/articles/16034433-does-crypto-bot-accept-transactions-from-any-crypto-service) · [Обзор Crypto Bot 2026 (вторичный)](https://ton-adoption.xyz/blog/crypto-bot-gayd-platezhi-v-telegram-2026/)

**CryptoCloud / Trybit:** [Комиссии](https://cryptocloud.plus/ru/pricing) · [Комиссии (база знаний)](https://support.trybit.com/ru/general-information/fees) · [Требования и ограничения](https://support.trybit.com/ru/general-information/connection-requirements) · [Валюты, сети и лимиты](https://support.trybit.com/ru/general-information/currencies-networks) · [Вывод средств](https://support.trybit.com/ru/withdrawals/withdrawal-request) · [Автовывод](https://support.trybit.com/ru/withdrawals/auto-withdrawal) · [Методы интеграции](https://support.trybit.com/integration/methods) · [Добавление проекта](https://support.trybit.com/ru/getting-started/add-project) · [Ребрендинг](https://cryptocloud.plus/blog/obnovlenie-cryptocloud-05-2026) · [Business Insider](https://markets.businessinsider.com/news/currencies/cryptocloud-rebrands-as-trybit-and-sets-sights-on-the-global-crypto-payments-market-1036281700)

**Plisio:** [Главная/тарифы](https://plisio.net/) · [Terms of Use PDF](https://plisio.net/docs/plisio-terms-of-use.pdf) · [KYC/AML explained PDF](https://plisio.net/docs/aml.pdf) · [Обзор (вторичный)](https://partnerkin.com/services/plisio) · [Форум-жалоба](https://tarnkappe.info/forum/t/plisio-net-soll-angeblich-scam-sein/15816)

**Cryptomus:** [Тарифы](https://cryptomus.com/fees/payment) · [FAQ: заморозки](https://cryptomus.com/faq/reasons-why-transactions-can-be-frozen) · [FAQ: AML](https://cryptomus.com/faq/what-is-aml-compliance) · [KYC-кризис января 2026](https://pressaff.com/tg-news/problemy-u-protsessinga-cryptomus-vchera-u-merchantov-i-klientov-cryptomus-massovo-sleteli/)

**CoinGate:** [Тарифы](https://coingate.com/pricing) · [Каталог VPN](https://coingate.com/stores/category/vpn) · [Каталог Proxy](https://coingate.com/stores/category/proxy) · [General T&C](https://coingate.com/policy/general-terms-and-conditions) · [Merchant ToS PDF](https://coingate.com/app/uploads/2024/02/Merchant-terms-of-Services_2024-02-06.pdf) · [Billing](https://coingate.com/billing)

**CoinPayments:** [Fees](https://www.coinpayments.net/help-fees) · [Supported coins & features](https://www.coinpayments.net/supported-coins-fees) · [User Agreement PDF](https://www.coinpayments.net/downloads/CoinPayments_User_Agreement.pdf)

**Confirmo:** [Обзор (вторичный)](https://bilixe.com/listing/confirmo-crypto-payment-gateway/) · [AMF](https://www.amf-france.org/sites/institutionnel/files/pdf/77623/en/Confirmo_Limited.pdf) · [Академический источник о регистрации только фирмы](https://dspace.vsb.cz/server/api/core/bitstreams/531b9413-882d-4216-b2d7-db3a74f62bc2/content) · [Corefy connector](https://corefy.com/connectors/confirmo)

**CoinsPaid:** [Сайт](https://coinspaid.com/) · [Комплаенс-гайд PDF](https://coinspaid.com/wp-content/uploads/2025/11/CP_Guide_How-CoinsPaid-helps-businesses-stay-compliant.pdf) · [Взлом 2023](https://smartcontractshacking.com/hacks/coinspaid-hack-2023) · [Заявление компании](https://coinspaidmedia.com/es/news/coinspaid-hacked-no-customer-funds-affected/) · [FinTelegram PDF](https://fintelegram.com/wp-content/uploads/2026/06/SoftSwiss-Compliance-Report-v2-Coinspaid-Dev-Update.pdf)

**ALFAcoins:** [Сайт](https://alfacoins.com/) · [Fee schedule](https://alfacoins.com/fees) · [FAQ](https://alfacoins.com/faq)

**NOWPayments:** [Тарифы](https://nowpayments.io/pricing) · [FD ToS PDF (v1.4.2)](https://nowpayments.io/doc/fd-tos.pdf) · [AML/KYC Policy PDF](https://nowpayments.io/doc/AML_KYC_Policy_NOWPayments.pdf) · [Subscriptions](https://nowpayments.io/crypto-subscriptions) · [Zero deposit fee USDT TRC20](https://nowpayments.io/zero-deposit-fee-usdttrc20)

**BitPay:** [Terms of Use](https://bitpay.com/legal/terms-of-use/) · **OpenNode:** [Terms](https://opennode.com/terms-and-conditions/) · [Pricing](https://opennode.com/pricing/) · [Billing](https://opennode.com/billing-invoicing/)

**Trocador / BestChange / P2P:** [Обзор Trocador (вторичный)](https://mycrypto.dk/service/trocador/) · [BestChange 2026](https://buyhold.ru/obzory/bestchange/) · [Отзывы BestChange](https://buyhold.ru/bestchange-otzyvy/) · [P2P, 115-ФЗ, 161-ФЗ, 187 УК](https://habr.com/en/articles/989158/) · [Forbes о первом приговоре за P2P](https://www.forbes.ru/investicii/485643-kak-v-rossii-vynesli-pervyj-prigovor-za-p2p-torgovlu-na-kriptobirze) · [Требования мониторингов к обменникам](https://premium.gitbook.io/main/osnovnye-nastroiki/faq/kakie-trebovaniya-predyavlyayutsya-k-obmennikam-ot-monitoringov)

**Своя проверка блокчейна:** [TronGrid rate limits](https://developers.tron.network/reference/rate-limits) · [TronGrid TRC-20 по адресу](https://developers.tron.network/reference/get-trc20-transaction-info-by-account-address) · [USDT Freeze 2026 (BlockSec)](https://blocksec.com/blog/usdt-freeze-stablecoin-compliance-guide) · [USDT Freeze Checker](https://blocksec.com/usdt-freeze-checker) · [Esplora API](https://github.com/Blockstream/esplora/blob/master/API.md) · [Blockchair API docs (401)](https://blockchair.com/api/docs) · [TONAPI free limits](https://docs.tonconsole.com/tonapi/dapp/free-limits) · [TONAPI Webhooks](https://docs.tonconsole.com/tonapi/webhooks-api) · [TONAPI jetton transfer](https://docs.tonconsole.com/tonapi/cookbook/jetton-transfer)

**Право РФ:** [282-ФЗ и 283-ФЗ от 04.08.2026, запрет расчётов криптой с 01.09.2026](https://consultant-plus.ru/company/news/opublikovan-novyj-zakon-o-kriptovalyute-i-tsifrovyh-pravah/)
